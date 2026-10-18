from pathlib import Path

import pytest

from procurement_bot import cli
from procurement_bot.cli import _build_research_executor
from procurement_bot.config import Settings
from procurement_bot.errors import ProviderAuthenticationDeferred
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.configured_agy_browser import ConfiguredAgyBrowserExecutor
from procurement_bot.providers.staged_research import StagedResearchProvider, prepare_discovery
from procurement_bot.research import ResearchSource, build_research_plan
from procurement_bot.research_execution import ResearchRunExecutor


def _olx_discovery_call():
    plan = build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Тестовый товар", quantity=1, unit="шт")],
        ),
        include_olx=True,
    )
    branch = next(item for item in plan.source_plans if item.source == ResearchSource.OLX)
    return prepare_discovery(branch)


def test_live_research_wiring_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert _build_research_executor(settings, object()) is None


@pytest.mark.asyncio
async def test_live_research_wiring_uses_dedicated_exact_mcp_profile(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=token,
        BROWSER_RESEARCH_ENABLED=True,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object())

    assert isinstance(executor, ResearchRunExecutor)
    transport = executor.browser.executor
    assert isinstance(transport, ConfiguredAgyBrowserExecutor)
    assert transport._transport is None
    configured = await transport._get_transport()
    assert isinstance(configured, AgyBrowserMcpExecutor)
    assert configured.home_dir == settings.agy_browser_home
    assert set(configured.expected_mcp_server) == {"command", "args"}


@pytest.mark.asyncio
async def test_staged_research_uses_long_source_captcha_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            captured["instance"] = self

        async def execute(self, _call):
            return {}

    monkeypatch.setattr(cli, "AgyBrowserMcpExecutor", FakeTransport)
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=token,
        BROWSER_RESEARCH_ENABLED=True,
        PARALLEL_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_ENABLED=True,
        BROWSER_SOURCE_COOLDOWN_SECONDS=1200,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object(), profile_extractor=object())

    assert isinstance(executor, ParallelResearchRunExecutor)
    agy = executor.browser.primary
    assert isinstance(agy, ConfiguredAgyBrowserExecutor)
    assert agy.captcha_cooldown_seconds == 1200
    await agy.execute(_olx_discovery_call())
    assert captured["captcha_cooldown_seconds"] == 1200
    assert isinstance(executor.browser, StagedResearchProvider)
    assert executor.browser.primary is agy
    assert captured["instance"] is not None
    assert executor.browser.fallback is None


@pytest.mark.asyncio
async def test_legacy_research_keeps_short_source_captcha_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def execute(self, _call):
            return {}

    monkeypatch.setattr(cli, "AgyBrowserMcpExecutor", FakeTransport)
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=token,
        BROWSER_RESEARCH_ENABLED=True,
        PARALLEL_RESEARCH_ENABLED=True,
        BROWSER_SOURCE_COOLDOWN_SECONDS=1200,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object())

    assert isinstance(executor, ParallelResearchRunExecutor)
    transport = executor.browser.executor
    assert isinstance(transport, ConfiguredAgyBrowserExecutor)
    assert transport.captcha_cooldown_seconds == 60
    await transport.execute(_olx_discovery_call())
    assert captured["captcha_cooldown_seconds"] == 60


def test_staged_codex_primary_constructs_codex_without_legacy_fallback_flags(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    codex_executable = tmp_path / "codex"
    codex_executable.write_text("codex", encoding="utf-8")
    codex_executable.chmod(0o700)
    captured: dict[str, object] = {}

    class FakeAgyTransport:
        def __init__(self, **kwargs):
            captured["agy_kwargs"] = kwargs
            captured["agy_instance"] = self

    class FakeCodexTransport:
        def __init__(self, **kwargs):
            captured["codex_kwargs"] = kwargs
            captured["codex_instance"] = self

    monkeypatch.setattr(cli, "AgyBrowserMcpExecutor", FakeAgyTransport)
    monkeypatch.setattr(cli, "CodexChromeExecutor", FakeCodexTransport)
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=token,
        BROWSER_RESEARCH_ENABLED=True,
        PARALLEL_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_PRIMARY="codex",
        CODEX_2GIS_EXECUTABLE=codex_executable,
        CODEX_CHROME_NODE_EXECUTABLE=Path("/opt/hermes/node/bin/node"),
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object(), profile_extractor=object())

    assert isinstance(executor, ParallelResearchRunExecutor)
    assert isinstance(executor.browser, StagedResearchProvider)
    assert executor.browser.primary is captured["codex_instance"]
    assert executor.browser.fallback is None
    agy = executor.browser.legacy.executor
    assert isinstance(agy, ConfiguredAgyBrowserExecutor)
    assert agy._transport is None
    assert captured["codex_kwargs"]["resource_budget"] is agy._transport_kwargs[
        "resource_budget"
    ]
    assert captured["codex_kwargs"]["chrome_node_executable"] == Path(
        "/opt/hermes/node/bin/node"
    )


@pytest.mark.parametrize("executable_state", ["missing", "not_executable"])
def test_staged_codex_primary_fails_when_executable_is_invalid(
    tmp_path: Path, monkeypatch, executable_state: str
) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    codex_executable = tmp_path / "codex"
    if executable_state == "not_executable":
        codex_executable.write_text("codex", encoding="utf-8")
        codex_executable.chmod(0o600)
    else:
        codex_executable = tmp_path / "missing-codex"

    class FakeAgyTransport:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(cli, "AgyBrowserMcpExecutor", FakeAgyTransport)
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=token,
        BROWSER_RESEARCH_ENABLED=True,
        PARALLEL_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_PRIMARY="codex",
        CODEX_2GIS_EXECUTABLE=codex_executable,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    with pytest.raises(ValueError, match="Codex Chrome executable was not found"):
        _build_research_executor(settings, object(), profile_extractor=object())


@pytest.mark.asyncio
async def test_agy_browser_auth_defers_then_lazily_builds_isolated_transport(
    tmp_path: Path, monkeypatch
) -> None:
    missing_token = tmp_path / "not-yet-authenticated"
    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def execute(self, call):
            return {"request_sha256": call.request_sha256}

    monkeypatch.setattr(cli, "AgyBrowserMcpExecutor", FakeTransport)
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=missing_token,
        BROWSER_RESEARCH_ENABLED=True,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object())
    transport = executor.browser.executor
    assert isinstance(transport, ConfiguredAgyBrowserExecutor)
    assert not settings.agy_browser_home.exists()
    call = _olx_discovery_call()
    with pytest.raises(ProviderAuthenticationDeferred):
        await transport.execute(call)
    assert captured == {}
    assert not settings.agy_browser_home.exists()

    missing_token.write_text("restored auth", encoding="utf-8")
    result = await transport.execute(call)

    assert result == {"request_sha256": call.request_sha256}
    assert captured["executable"] == Path("/bin/true").resolve()
    assert captured["home_dir"] == settings.agy_browser_home
    assert captured["mcp_server_name"] == "playwright"
    assert captured["expected_mcp_server"] == {
        "command": "/usr/bin/npx",
        "args": ["@playwright/mcp@1.2.3"],
    }
    assert (settings.agy_browser_home / ".gemini/config/mcp_config.json").is_file()
    assert (settings.agy_browser_home / ".gemini/antigravity-cli/settings.json").is_file()


def test_codex_primary_can_start_without_agy_auth(tmp_path: Path, monkeypatch) -> None:
    codex_executable = tmp_path / "codex"
    codex_executable.write_text("codex", encoding="utf-8")
    codex_executable.chmod(0o700)
    monkeypatch.setattr(cli, "CodexChromeExecutor", lambda **_kwargs: object())
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=tmp_path / "missing-token",
        BROWSER_RESEARCH_ENABLED=True,
        PARALLEL_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_ENABLED=True,
        STAGED_RESEARCH_PRIMARY="codex",
        CODEX_2GIS_EXECUTABLE=codex_executable,
        AGY_BROWSER_HOME=tmp_path / "browser-home",
        AGY_BROWSER_MCP_SERVER_NAME="playwright",
        AGY_BROWSER_MCP_SERVER_JSON=(
            '{"command":"/usr/bin/npx","args":["@playwright/mcp@1.2.3"]}'
        ),
    )

    executor = _build_research_executor(settings, object())

    assert isinstance(executor, ParallelResearchRunExecutor)
    assert isinstance(executor.browser, StagedResearchProvider)
    assert executor.browser.primary is not None
    assert isinstance(executor.browser.legacy.executor, ConfiguredAgyBrowserExecutor)
    assert not settings.agy_browser_home.exists()


def test_invalid_browser_mcp_configuration_still_fails_at_startup(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        AGY_EXECUTABLE="/bin/true",
        AGY_AUTH_TOKEN_SOURCE=tmp_path / "missing-token",
        BROWSER_RESEARCH_ENABLED=True,
        AGY_BROWSER_MCP_SERVER_JSON="not-json",
    )

    with pytest.raises(ValueError, match="AGY_BROWSER_MCP_SERVER_JSON is invalid JSON"):
        _build_research_executor(settings, object())
