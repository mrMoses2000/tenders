import asyncio
import json
from pathlib import Path

import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.browser import (
    PreparedBrowserCall,
    StructuredBrowserProvider,
)
from procurement_bot.research import LocalityResearchTask

MCP_SERVER = {
    "command": "/usr/bin/npx",
    "args": ["-y", "@browsermcp/mcp@1.2.3"],
}


def _safe_settings(server_name: str = "browsermcp") -> dict:
    return {
        "toolPermission": "request-review",
        "enableTerminalSandbox": True,
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "allow": [f"mcp({server_name}/*)"],
            "deny": [
                "command(*)",
                "unsandboxed(*)",
                "read_file(*)",
                "write_file(*)",
                "read_url(*)",
                "execute_url(*)",
            ],
            "ask": [],
        },
    }


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/python3\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_profile(home: Path, *, config: dict | None = None) -> None:
    profile = home / ".gemini" / "antigravity-cli"
    config_dir = home / ".gemini" / "config"
    profile.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (profile / "antigravity-oauth-token").write_text("dedicated-token", encoding="utf-8")
    (profile / "settings.json").write_text(
        json.dumps(_safe_settings()),
        encoding="utf-8",
    )
    (config_dir / "mcp_config.json").write_text(
        json.dumps(config or {"mcpServers": {"browsermcp": MCP_SERVER}}),
        encoding="utf-8",
    )


def _call() -> PreparedBrowserCall:
    return StructuredBrowserProvider(StubExecutor()).prepare_locality(
        LocalityResearchTask(city="Алматы", phrase="первая Алматы")
    )


class StubExecutor:
    async def execute(self, call):  # pragma: no cover - only satisfies the preparation helper
        raise AssertionError(call)


def _executor(tmp_path: Path, executable: Path, **overrides) -> AgyBrowserMcpExecutor:
    home = tmp_path / "dedicated-home"
    if not home.exists():
        _write_profile(home)
    options = {
        "executable": executable,
        "home_dir": home,
        "mcp_server_name": "browsermcp",
        "expected_mcp_server": MCP_SERVER,
        "timeout_seconds": 5,
    }
    options.update(overrides)
    return AgyBrowserMcpExecutor(**options)


def test_constructor_requires_explicit_absolute_paths(tmp_path):
    with pytest.raises(ValueError, match="executable path must be absolute"):
        AgyBrowserMcpExecutor(
            executable=Path("agy"),
            home_dir=tmp_path,
            mcp_server_name="browsermcp",
            expected_mcp_server=MCP_SERVER,
        )
    with pytest.raises(ValueError, match="HOME path must be absolute"):
        AgyBrowserMcpExecutor(
            executable=tmp_path / "agy",
            home_dir=Path("profile"),
            mcp_server_name="browsermcp",
            expected_mcp_server=MCP_SERVER,
        )


def test_constructor_rejects_mcp_environment_block(tmp_path):
    with pytest.raises(ValueError, match="environment block"):
        AgyBrowserMcpExecutor(
            executable=tmp_path / "agy",
            home_dir=tmp_path,
            mcp_server_name="browsermcp",
            expected_mcp_server={"command": "/bin/true", "env": {"TOKEN": "secret"}},
        )


@pytest.mark.asyncio
async def test_execute_uses_ephemeral_home_and_does_not_inherit_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROCUREMENT_MUST_NOT_LEAK", "top-secret")
    script = _write_executable(
        tmp_path / "fake-agy",
        """
import json, os, pathlib, sys
prompt = sys.stdin.read()
home = pathlib.Path(os.environ["HOME"])
config = json.loads((home / ".gemini/config/mcp_config.json").read_text())
safe = (
    "PROCUREMENT_MUST_NOT_LEAK" not in os.environ
    and home.name == "home"
    and list(config["mcpServers"]) == ["browsermcp"]
    and "UNTRUSTED_BROWSER_REQUEST_JSON" in prompt
)
result = {"safe": safe, "argv": sys.argv[1:]}
print(json.dumps({"status": "SUCCESS", "structured_output": {"result_json": json.dumps(result)}}))
""",
    )
    executor = _executor(tmp_path, script)

    result = await executor.execute(_call())

    assert result["safe"] is True
    assert "--sandbox" in result["argv"]
    assert "--disable-slash-commands" in result["argv"]
    assert not (tmp_path / "dedicated-home" / ".cache").exists()


@pytest.mark.asyncio
async def test_execute_refuses_missing_profile_before_starting_process(tmp_path):
    marker = tmp_path / "started"
    script = _write_executable(
        tmp_path / "fake-agy",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
    )
    home = tmp_path / "missing-home"
    executor = AgyBrowserMcpExecutor(
        executable=script,
        home_dir=home,
        mcp_server_name="browsermcp",
        expected_mcp_server=MCP_SERVER,
    )

    with pytest.raises(PermanentProviderError, match="missing"):
        await executor.execute(_call())
    assert not marker.exists()


@pytest.mark.asyncio
async def test_execute_refuses_extra_or_drifted_mcp_server(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    home = tmp_path / "dedicated-home"
    _write_profile(
        home,
        config={
            "mcpServers": {
                "browsermcp": MCP_SERVER,
                "unexpected": {"command": "/bin/true", "args": []},
            }
        },
    )
    executor = _executor(tmp_path, script)

    with pytest.raises(PermanentProviderError, match="allowlist"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_settings_that_define_another_mcp_path(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    settings = tmp_path / "dedicated-home/.gemini/antigravity-cli/settings.json"
    settings.write_text('{"nested":{"mcp_servers":{}}}', encoding="utf-8")

    with pytest.raises(PermanentProviderError, match="settings.json"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_global_auto_approval_policy(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    settings = tmp_path / "dedicated-home/.gemini/antigravity-cli/settings.json"
    settings.write_text('{"toolPermission":"always-proceed"}', encoding="utf-8")

    with pytest.raises(PermanentProviderError, match="fail-closed"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_non_json_stdout_without_markdown_fallback(tmp_path):
    script = _write_executable(
        tmp_path / "fake-agy",
        'print(\'```json\\n{"status":"SUCCESS"}\\n```\')\n',
    )
    executor = _executor(tmp_path, script)

    with pytest.raises(RetryableProviderError, match="one JSON value"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_validates_call_hash_before_starting_process(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    call = _call().model_copy(update={"request_sha256": "0" * 64})

    with pytest.raises(PermanentProviderError, match="request hash"):
        await executor.execute(call)


@pytest.mark.asyncio
async def test_concurrency_is_bounded_by_semaphore(tmp_path, monkeypatch):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script, concurrency=2)
    active = 0
    maximum = 0

    async def invoke(prompt):
        nonlocal active, maximum
        assert prompt
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {}

    monkeypatch.setattr(executor, "_invoke", invoke)
    await asyncio.gather(*(executor.execute(_call()) for _ in range(8)))

    assert maximum == 2


@pytest.mark.asyncio
async def test_timeout_terminates_process_group(tmp_path):
    script = _write_executable(
        tmp_path / "fake-agy",
        "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n",
    )
    executor = _executor(tmp_path, script, timeout_seconds=1)

    with pytest.raises(RetryableProviderError, match="timeout"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_accepts_only_prepared_browser_call(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)

    with pytest.raises(TypeError, match="PreparedBrowserCall"):
        await executor.execute({})  # type: ignore[arg-type]
