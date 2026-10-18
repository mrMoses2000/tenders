from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from procurement_bot.config import Settings
from procurement_bot.errors import ProviderAuthenticationDeferred, RetryableProviderError
from procurement_bot.providers.agy import AgyCliStatusError
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    BrowserOperation,
    PreparedBrowserCall,
)
from procurement_bot.providers.configured_agy_browser import ConfiguredAgyBrowserExecutor

MCP_SERVER = {"command": "/usr/bin/node", "args": ["/usr/local/lib/fake-mcp.js"]}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        agy_auth_token_source=tmp_path / "primary-token",
        agy_home=tmp_path / "parser-home",
        agy_browser_home=tmp_path / "browser-home",
        browser_research_enabled=True,
        agy_browser_mcp_server_name="playwright",
        agy_browser_mcp_server_json=(
            '{"command":"/usr/bin/node","args":["/usr/local/lib/fake-mcp.js"]}'
        ),
    )


class _FakeTransport:
    def __init__(self, **kwargs: Any) -> None:
        self.home_dir = kwargs["home_dir"]
        self.calls = 0
        self.error_code: str | None = None

    async def execute(self, _call: object) -> dict[str, str]:
        self.calls += 1
        if self.error_code:
            raise AgyCliStatusError(self.error_code)
        return {"result": "ok"}


def _executor(settings: Settings, transports: list[_FakeTransport]):
    def factory(**kwargs):
        transport = _FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    return ConfiguredAgyBrowserExecutor(
        settings,
        executable=Path("/usr/bin/agy"),
        mcp_server_name="playwright",
        expected_mcp_server=MCP_SERVER,
        transport_factory=factory,
        captcha_cooldown_seconds=60,
    )


@pytest.mark.asyncio
async def test_browser_transport_refreshes_when_primary_token_rotates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token-a")
    transports: list[_FakeTransport] = []
    executor = _executor(settings, transports)

    await executor.execute(object())  # type: ignore[arg-type]
    old_version = settings.agy_auth_token_source.stat()
    settings.agy_auth_token_source.write_bytes(b"token-b")
    os.utime(
        settings.agy_auth_token_source,
        ns=(old_version.st_atime_ns, old_version.st_mtime_ns + 1_000_000_000),
    )

    await executor.execute(object())  # type: ignore[arg-type]
    assert len(transports) == 2
    assert transports[0].calls == 1
    assert transports[1].calls == 1
    token = settings.agy_browser_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    assert token.read_bytes() == b"token-b"


@pytest.mark.asyncio
async def test_browser_keeps_cache_when_fallback_has_same_token_bytes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"same-token")
    parser_fallback = (
        settings.agy_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    )
    parser_fallback.parent.mkdir(parents=True)
    parser_fallback.write_bytes(b"same-token")
    transports: list[_FakeTransport] = []
    executor = _executor(settings, transports)
    await executor.execute(object())  # type: ignore[arg-type]

    settings.agy_auth_token_source.unlink()
    await executor.execute(object())  # type: ignore[arg-type]
    assert len(transports) == 1
    assert transports[0].calls == 2


@pytest.mark.asyncio
async def test_deleted_cached_browser_token_defers_without_using_old_transport(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token")
    transports: list[_FakeTransport] = []
    executor = _executor(settings, transports)
    await executor.execute(object())  # type: ignore[arg-type]
    settings.agy_auth_token_source.unlink()
    (settings.agy_browser_home / ".gemini/antigravity-cli/antigravity-oauth-token").unlink()

    with pytest.raises(ProviderAuthenticationDeferred) as caught:
        await executor.execute(object())  # type: ignore[arg-type]
    assert caught.value.reason == "provider_auth_missing"
    assert len(transports) == 1
    assert transports[0].calls == 1


@pytest.mark.asyncio
async def test_concurrent_browser_rotation_rebuilds_transport_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token-a")
    transports: list[_FakeTransport] = []
    executor = _executor(settings, transports)
    await executor.execute(object())  # type: ignore[arg-type]
    settings.agy_auth_token_source.write_bytes(b"token-b")

    await asyncio.gather(*(executor.execute(object()) for _ in range(8)))  # type: ignore[arg-type]
    assert len(transports) == 2
    assert transports[1].calls == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_code", "deferred"),
    [("cli_auth_failed", True), ("cli_usage_limit", False), ("cli_account_ineligible", False)],
)
async def test_browser_converts_only_typed_auth_denial_without_local_retry(
    tmp_path: Path, diagnostic_code: str, deferred: bool
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token")
    transports: list[_FakeTransport] = []
    executor = _executor(settings, transports)
    transport = await executor._get_transport()
    transport.error_code = diagnostic_code

    if deferred:
        with pytest.raises(ProviderAuthenticationDeferred) as caught:
            await executor.execute(object())  # type: ignore[arg-type]
        assert caught.value.reason == "provider_auth_failed"
    else:
        with pytest.raises(RetryableProviderError) as caught:
            await executor.execute(object())  # type: ignore[arg-type]
        assert isinstance(caught.value, AgyCliStatusError)
        assert caught.value.diagnostic_code == diagnostic_code
    assert len(transports) == 1
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_real_browser_cli_auth_envelope_defers_through_configured_wrapper(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token")
    marker = tmp_path / "invocation-count"
    script = tmp_path / "fake-agy"
    script.write_text(
        "#!/usr/bin/python3\n"
        "from pathlib import Path\n"
        f"p=Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "print('{\"status\":\"ERROR\",\"message\":\"authentication failed; PRIVATE\"}')\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    executor = ConfiguredAgyBrowserExecutor(
        settings,
        executable=script,
        mcp_server_name="playwright",
        expected_mcp_server=MCP_SERVER,
        captcha_cooldown_seconds=60,
    )
    request = "{}"
    call = PreparedBrowserCall(
        operation=BrowserOperation.RESOLVE_LOCALITY,
        spec_sha256=BROWSER_SPEC_SHA256,
        request_sha256=hashlib.sha256(request.encode()).hexdigest(),
        trusted_instruction="read-only test",
        untrusted_request_json=request,
        output_schema={"type": "object"},
    )

    with pytest.raises(ProviderAuthenticationDeferred) as caught:
        await executor.execute(call)

    assert caught.value.reason == "provider_auth_failed"
    assert "PRIVATE" not in str(caught.value)
    assert marker.read_text() == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "expected_auth_failure"),
    [
        (
            '{"status":"ERROR","message":"authentication failed; PRIVATE"}',
            True,
        ),
        (
            '{"status":"SUCCESS","structured_output":{"result_json":"{}"}}',
            False,
        ),
        ("not json", False),
        ("", False),
    ],
)
async def test_nonzero_browser_exit_classifies_only_structured_error_envelopes(
    tmp_path: Path, stdout: str, expected_auth_failure: bool
) -> None:
    settings = _settings(tmp_path)
    settings.agy_auth_token_source.write_bytes(b"token")
    marker = tmp_path / "invocation-count"
    script = tmp_path / "fake-agy"
    script.write_text(
        "#!/usr/bin/python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"p=Path({str(marker)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        f"sys.stdout.write({stdout!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    executor = ConfiguredAgyBrowserExecutor(
        settings,
        executable=script,
        mcp_server_name="playwright",
        expected_mcp_server=MCP_SERVER,
        captcha_cooldown_seconds=60,
    )
    request = "{}"
    call = PreparedBrowserCall(
        operation=BrowserOperation.RESOLVE_LOCALITY,
        spec_sha256=BROWSER_SPEC_SHA256,
        request_sha256=hashlib.sha256(request.encode()).hexdigest(),
        trusted_instruction="read-only test",
        untrusted_request_json=request,
        output_schema={"type": "object"},
    )

    if expected_auth_failure:
        with pytest.raises(ProviderAuthenticationDeferred) as caught:
            await executor.execute(call)
        assert caught.value.reason == "provider_auth_failed"
        assert "PRIVATE" not in str(caught.value)
    else:
        with pytest.raises(RetryableProviderError) as caught:
            await executor.execute(call)
        assert not isinstance(caught.value, AgyCliStatusError)
        assert str(caught.value) == "AGY browser CLI exited with a nonzero status"
        assert "PRIVATE" not in str(caught.value)
    assert marker.read_text() == "1"
