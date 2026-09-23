from pathlib import Path

from procurement_bot.cli import _build_research_executor
from procurement_bot.config import Settings
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
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
