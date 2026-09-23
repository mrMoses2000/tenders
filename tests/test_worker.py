from __future__ import annotations

import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from procurement_bot.errors import PermanentProviderError
from procurement_bot.queue import ClaimedJob, stable_key
from procurement_bot.worker import JobWorker, _location_text, _validate_downloaded


def test_location_text_uses_exact_coordinates() -> None:
    raw = {"message": {"location": {"latitude": 43.25, "longitude": 76.95}}}
    assert _location_text(raw) == (
        "Пользователь указал геопозицию: latitude=43.25, longitude=76.95."
    )


def test_download_validation_rejects_fake_pdf(tmp_path) -> None:
    source = tmp_path / "request.pdf"
    source.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(PermanentProviderError, match="not a PDF"):
        _validate_downloaded(source, "document", "application/pdf")


def test_download_validation_accepts_minimal_docx_container(tmp_path) -> None:
    source = tmp_path / "request.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    _validate_downloaded(
        source,
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@pytest.mark.asyncio
async def test_worker_dispatches_prepare_research_job(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"case_id": str(uuid4()), "intake_version": 3}
    captured: list[dict[str, object]] = []

    async def fake_prepare(_self: JobWorker, value: dict[str, object]) -> None:
        captured.append(value)

    monkeypatch.setattr(JobWorker, "_prepare_research", fake_prepare)
    worker = object.__new__(JobWorker)
    job = ClaimedJob(
        id=uuid4(),
        kind="prepare_research",
        payload=payload,
        attempts=1,
        max_attempts=3,
        idempotency_key="prepare:1",
        locked_by="test",
        lease_expires_at=datetime.now(UTC),
    )

    await worker._dispatch(job)

    assert captured == [payload]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "method_name", "payload"),
    [
        ("start_research", "_start_research", {"approval_id": str(uuid4())}),
        (
            "execute_research",
            "_execute_research",
            {"research_run_id": str(uuid4())},
        ),
        (
            "process_supplier_reply",
            "_process_supplier_reply",
            {"conversation_id": str(uuid4()), "message_id": str(uuid4())},
        ),
        (
            "build_report",
            "_build_report",
            {"case_id": str(uuid4()), "research_run_id": str(uuid4())},
        ),
        (
            "prepare_supplier_outreach",
            "_prepare_supplier_outreach",
            {"case_id": str(uuid4())},
        ),
        (
            "enqueue_whatsapp_send",
            "_enqueue_whatsapp_send",
            {"approval_id": str(uuid4())},
        ),
        (
            "resolve_locality",
            "_resolve_locality",
            {
                "case_id": str(uuid4()),
                "intake_version": 3,
                "owner_user_id": str(uuid4()),
            },
        ),
    ],
)
async def test_worker_dispatches_research_control_jobs(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    method_name: str,
    payload: dict[str, str],
) -> None:
    captured: list[dict[str, object]] = []

    async def fake_handler(_self: JobWorker, value: dict[str, object]) -> None:
        captured.append(value)

    monkeypatch.setattr(JobWorker, method_name, fake_handler)
    worker = object.__new__(JobWorker)
    job = ClaimedJob(
        id=uuid4(),
        kind=kind,
        payload=payload,
        attempts=1,
        max_attempts=3,
        idempotency_key=f"{kind}:1",
        locked_by="test",
        lease_expires_at=datetime.now(UTC),
    )

    await worker._dispatch(job)

    assert captured == [payload]


@pytest.mark.asyncio
async def test_execute_research_fails_closed_without_live_adapter() -> None:
    worker = object.__new__(JobWorker)
    worker.research_executor = None

    with pytest.raises(PermanentProviderError, match="not configured"):
        await worker._execute_research({"research_run_id": str(uuid4())})


@pytest.mark.asyncio
async def test_terminal_research_schedules_idempotent_report_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    case_id = uuid4()
    jobs: list[dict[str, object]] = []

    class Connection:
        def transaction(self):
            @asynccontextmanager
            async def manager():
                yield

            return manager()

    class Pool:
        def acquire(self):
            @asynccontextmanager
            async def manager():
                yield Connection()

            return manager()

    class Executor:
        async def execute(self, _run_id: object) -> object:
            return SimpleNamespace(status="partial", case_id=case_id)

    async def enqueue(_connection: object, **kwargs: object) -> object:
        jobs.append(kwargs)
        return uuid4()

    monkeypatch.setattr("procurement_bot.worker.enqueue_job", enqueue)
    worker = object.__new__(JobWorker)
    worker.pool = Pool()  # type: ignore[assignment]
    worker.research_executor = Executor()

    await worker._execute_research({"research_run_id": str(run_id)})

    assert jobs == [
        {
            "kind": "build_report",
            "payload": {"case_id": str(case_id), "research_run_id": str(run_id)},
            "idempotency_key": stable_key("build-report", case_id, run_id),
        }
    ]
