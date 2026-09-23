from __future__ import annotations

from pathlib import Path

import pytest

from procurement_bot.report_artifacts import (
    ReportArtifactConflict,
    resolve_report_path,
    store_report_html,
)


def test_report_html_is_private_content_addressed_and_idempotent(tmp_path: Path) -> None:
    first = store_report_html(tmp_path, "<!doctype html><title>Отчёт</title>")
    second = store_report_html(tmp_path, "<!doctype html><title>Отчёт</title>")

    assert first == second
    storage_path, digest, byte_size = first
    assert storage_path == f"reports/{digest[:2]}/{digest}.html"
    target = resolve_report_path(tmp_path, storage_path)
    assert target.stat().st_size == byte_size
    assert target.stat().st_mode & 0o777 == 0o600


def test_report_storage_rejects_empty_and_tampered_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        store_report_html(tmp_path, "  ")

    storage_path, _, _ = store_report_html(tmp_path, "<h1>safe</h1>")
    target = resolve_report_path(tmp_path, storage_path)
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(ReportArtifactConflict, match="other bytes"):
        store_report_html(tmp_path, "<h1>safe</h1>")


def test_report_path_cannot_escape_private_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        resolve_report_path(tmp_path, "../secret.html")
