from __future__ import annotations

import json
from pathlib import Path

import pytest

from procurement_bot.providers.chrome_lifecycle import (
    ChromeLifecycle,
    ChromeLifecycleCounters,
)


def _plugin(tmp_path: Path) -> Path:
    root = tmp_path / "chrome-plugin"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "chrome-is-running.js").write_text("// fake check\n", encoding="utf-8")
    (scripts / "open-chrome-window.js").write_text("// fake launch\n", encoding="utf-8")
    return root


def _node_stub(tmp_path: Path, *, running: bool) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "node"
    executable.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  *chrome-is-running.js) printf '%s\\n' '{json.dumps({'running': running})}'; "
        f"[ '{str(running).lower()}' = 'true' ] || exit 1 ;;\n"
        "  *open-chrome-window.js) printf '%s\\n' '{\"dryRun\":false}' ;;\n"
        "  *) exit 7 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_plugin_root_must_be_absolute_and_contain_supported_scripts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ChromeLifecycle(Path("relative/plugin"))
    with pytest.raises(ValueError, match="scripts are missing"):
        ChromeLifecycle(tmp_path)


@pytest.mark.asyncio
async def test_preflight_launches_only_when_supported_check_says_chrome_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(tmp_path)
    node = _node_stub(tmp_path, running=False)
    monkeypatch.setenv("PATH", str(node.parent))
    counters = ChromeLifecycleCounters(cleanup_requested=1)

    await ChromeLifecycle(root).ensure_running(counters)

    assert counters.cleanup_requested == 1
    assert counters.recovery_attempted == 1
    assert counters.launcher_started == 1


@pytest.mark.asyncio
async def test_existing_chrome_is_not_relaunched_on_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(tmp_path)
    node = _node_stub(tmp_path, running=True)
    monkeypatch.setenv("PATH", str(node.parent))
    counters = ChromeLifecycleCounters()

    await ChromeLifecycle(root).ensure_running(counters)

    assert counters.recovery_attempted == 0
    assert counters.launcher_started == 0


@pytest.mark.asyncio
async def test_recovery_is_bounded_and_uses_launcher_only_for_closed_chrome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(tmp_path)
    node = _node_stub(tmp_path, running=False)
    monkeypatch.setenv("PATH", str(node.parent))
    counters = ChromeLifecycleCounters()

    await ChromeLifecycle(root).recover_after_disconnect(counters)

    assert counters.recovery_attempted == 1
    assert counters.launcher_started == 1
