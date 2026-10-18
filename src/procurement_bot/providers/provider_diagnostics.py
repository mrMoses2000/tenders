from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_UNDERLYING_ERROR_CODES = frozenset({
    "browser_scroll_timeout",
    "browser_tool_timeout",
    "permission_denied",
    "usage_limit",
})


class ProviderAttemptDiagnostics:
    """Write small, allowlisted provider attempt receipts outside temp workspaces."""

    def __init__(
        self,
        root: Path | None,
        *,
        operation: str,
        source: str,
        item_index: int | None,
        model: str,
        request_sha256: str | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else None
        self.started_at = datetime.now(UTC)
        self.attempt_id = uuid4().hex
        self._stage_started = time.monotonic()
        self.stage_elapsed_seconds: dict[str, float] = {}
        self.operation = operation[:80]
        self.source = source[:40]
        self.item_index = (
            item_index if isinstance(item_index, int) and not isinstance(item_index, bool)
            and item_index >= 0 else None
        )
        self.model = model[:100]
        self.request_sha256 = (
            request_sha256 if isinstance(request_sha256, str)
            and len(request_sha256) == 64
            and all(character in "0123456789abcdef" for character in request_sha256)
            else None
        )
        self.last_stage = "started"
        self.browser_calls = 0
        self.pages_examined = 0
        self.cleanup_requested = 0
        self.recovery_attempted = 0
        self.launcher_started = 0
        self.underlying_error_code: str | None = None

    def stage(self, value: str) -> None:
        now = time.monotonic()
        self.stage_elapsed_seconds[self.last_stage] = (
            self.stage_elapsed_seconds.get(self.last_stage, 0) + now - self._stage_started
        )
        self._stage_started = now
        self.last_stage = value[:80]
        self._write(status="running")

    def observe(
        self,
        *,
        browser_calls: int = 0,
        pages_examined: int = 0,
        cleanup_requested: int = 0,
        recovery_attempted: int = 0,
        launcher_started: int = 0,
    ) -> None:
        self.browser_calls = max(self.browser_calls, max(0, browser_calls))
        self.pages_examined = max(self.pages_examined, max(0, pages_examined))
        self.cleanup_requested = max(self.cleanup_requested, max(0, cleanup_requested))
        self.recovery_attempted = max(self.recovery_attempted, max(0, recovery_attempted))
        self.launcher_started = max(self.launcher_started, max(0, launcher_started))

    def finish(
        self,
        *,
        status: str,
        error: str | None = None,
        underlying_error_code: str | None = None,
    ) -> None:
        now = time.monotonic()
        self.stage_elapsed_seconds[self.last_stage] = (
            self.stage_elapsed_seconds.get(self.last_stage, 0) + now - self._stage_started
        )
        self._stage_started = now
        self.underlying_error_code = (
            underlying_error_code
            if isinstance(underlying_error_code, str)
            and underlying_error_code in _UNDERLYING_ERROR_CODES
            else None
        )
        self._write(status=status, error=error, finished=True)

    def _write(self, *, status: str, error: str | None = None, finished: bool = False) -> None:
        if self.root is None:
            return
        finished_at = datetime.now(UTC)
        receipt: dict[str, Any] = {
            "operation": self.operation,
            "source": self.source,
            "item_index": self.item_index,
            "model": self.model,
            "request_sha256": self.request_sha256,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat() if finished else None,
            "updated_at": finished_at.isoformat(),
            "stage_elapsed_seconds": {key: round(value, 3)
                                      for key, value in self.stage_elapsed_seconds.items()},
            "status": status[:40],
            "error": error[:80] if error else None,
            "underlying_error_code": self.underlying_error_code,
            "last_stage": self.last_stage,
            "browser_call_count": self.browser_calls,
            "page_count": self.pages_examined,
            "tab_cleanup_requested_count": self.cleanup_requested,
            "browser_recovery_attempt_count": self.recovery_attempted,
            "browser_launcher_start_count": self.launcher_started,
        }
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root,
                prefix=".attempt-", suffix=".tmp", delete=False,
            ) as output:
                temporary_name = output.name
                os.chmod(temporary_name, 0o600)
                json.dump(receipt, output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
            target = self.root / (
                f"{self.started_at:%Y%m%dT%H%M%S}-{self.attempt_id}.json"
            )
            os.replace(temporary_name, target)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)


def request_diagnostic_context(request_json: str) -> tuple[str, int | None]:
    """Read only non-sensitive routing fields from a prepared call request."""

    try:
        request = json.loads(request_json)
    except (TypeError, json.JSONDecodeError):
        return "unknown", None
    if not isinstance(request, dict):
        return "unknown", None
    source = request.get("source", "2gis")
    item_index = request.get("item_index")
    safe_source = (
        source if isinstance(source, str)
        and source in {"2gis", "olx", "web", "own_database"}
        else "unknown"
    )
    safe_index = (
        item_index if isinstance(item_index, int) and not isinstance(item_index, bool)
        and item_index >= 0 else None
    )
    return safe_source, safe_index


def safe_error_code(exc: BaseException) -> str:
    """Map exception details to bounded codes; never retain arbitrary messages/logs."""

    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    diagnostic_code = getattr(exc, "diagnostic_code", None)
    if isinstance(diagnostic_code, str) and diagnostic_code in {
        "cli_usage_limit", "cli_rate_limit", "cli_auth_failed", "cli_context_limit",
        "cli_timeout", "cli_status_error", "cli_permission_denied", "mcp_disconnected",
        "cli_exit_nonzero", "cli_account_ineligible",
        "resource_wait_timeout",
    }:
        return diagnostic_code

    access_issue = getattr(exc, "access_issue", None)
    if isinstance(access_issue, str) and access_issue in {
        "captcha", "access_denied", "login_wall", "mcp_error",
        "browser_start_failed", "no_results", "unknown",
    }:
        return f"access_{access_issue}"
    known_messages = {
        "AGY browser CLI timeout": "cli_timeout",
        "AGY browser CLI transport failed": "transport_failed",
        "Codex Chrome fallback timed out": "cli_timeout",
        "Codex Chrome fallback could not start": "browser_start_failed",
        "configured AGY executable could not start": "browser_start_failed",
    }
    return known_messages.get(str(exc), type(exc).__name__[:80])


def result_access_issue(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    issue = result.get("access_issue")
    return issue if isinstance(issue, str) and issue in {
        "captcha", "access_denied", "login_wall", "mcp_error",
        "browser_start_failed", "no_results", "unknown",
    } else None
