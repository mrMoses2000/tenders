from pathlib import Path

import pytest

from procurement_bot import cli
from procurement_bot.cli import _build_research_executor
from procurement_bot.config import Settings
from procurement_bot.parallel_research import ParallelResearchRunExecutor
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.staged_research import StagedResearchProvider
from procurement_bot.research_execution import ResearchRunExecutor


def test_live_research_wiring_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert _build_research_executor(settings, object()) is None


def test_live_research_wiring_uses_dedicated_exact_mcp_profile(tmp_path: Path) -> None:
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
    assert isinstance(transport, AgyBrowserMcpExecutor)
    assert transport.home_dir == settings.agy_browser_home
    assert set(transport.expected_mcp_server) == {"command", "args"}


def test_staged_research_uses_long_source_captcha_cooldown(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            captured["instance"] = self

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
    assert captured["captcha_cooldown_seconds"] == 1200
    assert isinstance(executor.browser, StagedResearchProvider)
    assert executor.browser.primary is captured["instance"]
    assert executor.browser.fallback is None


def test_legacy_research_keeps_short_source_captcha_cooldown(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "token"
    token.write_text("auth", encoding="utf-8")
    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

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
    assert executor.browser.legacy.executor is captured["agy_instance"]
    assert captured["codex_kwargs"]["resource_budget"] is captured["agy_kwargs"][
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
