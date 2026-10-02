from __future__ import annotations

import hashlib
import json
import shlex
import stat
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from procurement_bot.errors import RetryableProviderError
from procurement_bot.intake import SearchScope
from procurement_bot.providers.browser import StaticBrowserExecutor, StructuredBrowserProvider
from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.research import (
    LocalityConstraint,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
)


def _call():
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["поставщик"],
        rendered_query="поставщик Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=1,
        empty_page_limit=1,
    )
    return StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)


def _staged_call(stage: str):
    call = _call()
    request = json.loads(call.untrusted_request_json)
    request["execution_stage"] = stage
    request_json = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return call.model_copy(update={
        "untrusted_request_json": request_json,
        "request_sha256": hashlib.sha256(request_json.encode()).hexdigest(),
    })


def test_explicit_chrome_node_executable_is_passed_to_lifecycle(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o700)
    codex = _write_codex_stub(tmp_path, access_issue=None)

    executor = CodexChromeExecutor(
        executable=codex,
        chrome_node_executable=node,
    )

    assert executor._chrome_lifecycle.node_executable == node


def test_explicit_chrome_node_executable_uses_lifecycle_validation(tmp_path: Path) -> None:
    codex = _write_codex_stub(tmp_path, access_issue=None)
    node = tmp_path / "not-executable"
    node.write_text("node", encoding="utf-8")

    with pytest.raises(ValueError, match="Node executable must be absolute and executable"):
        CodexChromeExecutor(executable=codex, chrome_node_executable=node)


def _write_codex_stub(
    tmp_path: Path,
    *,
    access_issue: str | None,
    failed_tool_text: str | None = None,
    result_object: dict | None = None,
) -> Path:
    if result_object is None:
        result_object = {
            "candidates": [],
            "exhausted": False,
            "pages_examined": 0,
            "access_issue": access_issue,
        }
    envelope = json.dumps({"result_json": json.dumps(result_object)})
    lines = ["#!/bin/sh", "cat >/dev/null"]
    if failed_tool_text is not None:
        event = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "status": "failed",
                "result": {"content": [{"type": "text", "text": failed_tool_text}]},
            },
        })
        lines.append(f"printf '%s\\n' {shlex.quote(event)} >&2")
    lines.extend([
        "while [ \"$#\" -gt 0 ]; do",
        "  if [ \"$1\" = -o ]; then",
        "    shift",
        f"    printf '%s\\n' {shlex.quote(envelope)} > \"$1\"",
        "    exit 0",
        "  fi",
        "  shift",
        "done",
        "exit 1",
    ])
    executable = tmp_path / "codex-stub"
    executable.write_text("\n".join(lines) + "\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


@pytest.mark.asyncio
async def test_structured_access_failure_retains_private_trace_and_safe_cause(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "diagnostics"
    executable = _write_codex_stub(
        tmp_path,
        access_issue="mcp_error",
        failed_tool_text="Permission denied by browser policy",
    )
    executor = CodexChromeExecutor(
        executable=executable,
        diagnostics_root=diagnostics,
        retain_failure_trace=True,
    )

    result = await executor.execute(_call())

    assert result["access_issue"] == "mcp_error"
    trace_dir = diagnostics / "private-failure-traces"
    traces = list(trace_dir.glob("*.log"))
    assert len(traces) == 1
    assert traces[0].read_text(encoding="utf-8").find("Permission denied") >= 0
    assert stat.S_IMODE(trace_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(traces[0].stat().st_mode) == 0o600

    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    finished = [receipt for receipt in receipts if receipt["finished_at"] is not None]
    assert len(finished) == 2  # outer execute and _invoke receipts
    assert all(receipt["status"] == "access_issue" for receipt in finished)
    assert all(receipt["error"] == "access_mcp_error" for receipt in finished)
    assert all(receipt["underlying_error_code"] == "permission_denied" for receipt in finished)
    assert all("Permission denied by browser policy" not in json.dumps(receipt)
               for receipt in finished)


@pytest.mark.asyncio
async def test_success_does_not_retain_private_trace(tmp_path: Path) -> None:
    diagnostics = tmp_path / "diagnostics"
    executable = _write_codex_stub(tmp_path, access_issue=None)
    executor = CodexChromeExecutor(
        executable=executable,
        diagnostics_root=diagnostics,
        retain_failure_trace=True,
    )

    result = await executor.execute(_call())

    assert result["access_issue"] is None
    assert not (diagnostics / "private-failure-traces").exists()
    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    assert any(receipt["status"] == "succeeded" for receipt in receipts)


@pytest.mark.asyncio
async def test_structured_access_failure_trace_retention_is_disabled_by_default(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "diagnostics"
    executable = _write_codex_stub(
        tmp_path,
        access_issue="captcha",
        failed_tool_text="Timed out waiting for CDP command Input.click",
    )
    executor = CodexChromeExecutor(executable=executable, diagnostics_root=diagnostics)

    result = await executor.execute(_call())

    assert result["access_issue"] == "captcha"
    assert not (diagnostics / "private-failure-traces").exists()
    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    finished = [receipt for receipt in receipts if receipt["finished_at"] is not None]
    assert len(finished) == 2
    assert all(receipt["underlying_error_code"] == "browser_tool_timeout"
               for receipt in finished)


@pytest.mark.asyncio
async def test_explicit_mcp_disconnect_retries_same_call_once_under_plugin_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "chrome-plugin"
    scripts = plugin_root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "chrome-is-running.js").write_text("// fake check\n", encoding="utf-8")
    (scripts / "open-chrome-window.js").write_text("// fake launch\n", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"running\":true}'\n", encoding="utf-8"
    )
    node.chmod(0o700)

    marker = tmp_path / "attempt-count"
    prompt_a = tmp_path / "prompt-a"
    prompt_b = tmp_path / "prompt-b"
    executable = tmp_path / "codex-stub"
    result_envelope = json.dumps({
        "result_json": json.dumps({
            "candidates": [], "exhausted": False, "pages_examined": 1,
            "access_issue": "mcp_error",
        })
    })
    disconnect_event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call", "status": "failed",
            "result": {"content": [{"type": "text", "text": "MCP server disconnected"}]},
        },
    })
    executable.write_text(
        "#!/bin/sh\n"
        f"count=0; [ ! -f {shlex.quote(str(marker))} ] || count=$(cat {shlex.quote(str(marker))})\n"
        "count=$((count + 1))\n"
        f"printf '%s' \"$count\" > {shlex.quote(str(marker))}\n"
        f"if [ \"$count\" -eq 1 ]; then cat > {shlex.quote(str(prompt_a))}; "
        "echo 'MCP server disconnected' >&2; exit 1; fi\n"
        f"cat > {shlex.quote(str(prompt_b))}\n"
        f"printf '%s\\n' {shlex.quote(disconnect_event)} >&2\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        f"  if [ \"$1\" = -o ]; then shift; printf '%s\\n' "
        f"{shlex.quote(result_envelope)} > \"$1\"; break; fi\n"
        "  shift\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    call = _call()
    executor = CodexChromeExecutor(
        executable=executable,
        chrome_plugin_root=plugin_root,
        diagnostics_root=tmp_path / "diagnostics",
    )
    result = await executor.execute(call)

    assert result["request_sha256"] == call.request_sha256
    assert result["access_issue"] == "mcp_error"
    assert marker.read_text() == "2"
    assert prompt_a.read_text() == prompt_b.read_text()
    receipts = [json.loads(path.read_text()) for path in (tmp_path / "diagnostics").glob("*.json")]
    assert any(
        receipt["browser_recovery_attempt_count"] == 1
        and receipt["browser_launcher_start_count"] == 0
        and receipt["tab_cleanup_requested_count"] == 1
        for receipt in receipts
    )


@pytest.mark.asyncio
async def test_timeout_sends_sigint_and_reaps_cli_before_forced_termination(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "turn-interrupted"
    executable = tmp_path / "codex-waits-for-signal"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import signal, time\n"
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "def stop(*_args):\n"
        "    marker.write_text('sigint-cleanup', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    call = _call()
    executor = CodexChromeExecutor(
        executable=executable,
        timeout_seconds=30,
        batch_timeout_seconds=1,
    )

    with pytest.raises(RetryableProviderError, match="timed out"):
        await executor.execute(call)

    assert marker.read_text(encoding="utf-8") == "sigint-cleanup"


def _valid_staged_result() -> dict:
    return {
        "candidates": [{
            "source": "2gis",
            "external_id": "firm-123",
            "name": "Поставщик",
            "evidence": [{
                "evidence_type": "business_listing",
                "source": "2gis",
                "source_url": "https://2gis.kz/almaty/firm/123",
                "observed_at": "2026-10-01T10:00:00Z",
                "raw_snapshot_sha256": "a" * 64,
                "claims": ["business_identity"],
            }],
        }],
        "exhausted": False,
        "pages_examined": 1,
        "access_issue": None,
    }


@pytest.mark.asyncio
async def test_malformed_staged_result_retains_opt_in_trace_and_safe_diagnostic(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "diagnostics"
    malformed = {
        "candidates": ["invalid candidate"],
        "exhausted": False,
        "pages_examined": 1,
        "access_issue": None,
    }
    executor = CodexChromeExecutor(
        executable=_write_codex_stub(
            tmp_path, access_issue=None, result_object=malformed,
        ),
        diagnostics_root=diagnostics,
        retain_failure_trace=True,
    )

    with pytest.raises(ValidationError):
        await executor.execute(_staged_call("read_card"))

    traces = list((diagnostics / "private-failure-traces").glob("*.log"))
    assert len(traces) == 1
    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    finished = [receipt for receipt in receipts if receipt["finished_at"] is not None]
    assert len(finished) == 2
    assert all(receipt["error"] == "ValidationError" for receipt in finished)
    assert all("invalid candidate" not in json.dumps(receipt) for receipt in finished)


@pytest.mark.asyncio
async def test_valid_staged_result_does_not_retain_trace(tmp_path: Path) -> None:
    diagnostics = tmp_path / "diagnostics"
    executor = CodexChromeExecutor(
        executable=_write_codex_stub(
            tmp_path, access_issue=None, result_object=_valid_staged_result(),
        ),
        diagnostics_root=diagnostics,
        retain_failure_trace=True,
    )

    result = await executor.execute(_staged_call("read_contacts"))

    assert result["candidates"][0]["external_id"] == "firm-123"
    assert not (diagnostics / "private-failure-traces").exists()


@pytest.mark.asyncio
async def test_malformed_staged_result_retention_remains_disabled_by_default(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "diagnostics"
    executor = CodexChromeExecutor(
        executable=_write_codex_stub(
            tmp_path,
            access_issue=None,
            result_object={
                "candidates": ["invalid candidate"],
                "exhausted": False,
                "pages_examined": 1,
                "access_issue": None,
            },
        ),
        diagnostics_root=diagnostics,
    )

    with pytest.raises(ValidationError):
        await executor.execute(_staged_call("read_contacts"))

    assert not (diagnostics / "private-failure-traces").exists()
