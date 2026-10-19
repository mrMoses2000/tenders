from __future__ import annotations

import asyncio
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.pipeline_runtime import ProviderQuotaDeferred
from procurement_bot.providers.agy import AgyCliStatusError
from procurement_bot.queue import ClaimedJob, stable_key
from procurement_bot.semantic_supplier_reply import (
    SemanticReplyProposalOutput,
    SemanticReplyTarget,
    _make_snapshot,
)
from procurement_bot.supplier_dialogue import WhatsAppSendDeferred
from procurement_bot.worker import JobWorker, _location_text, _validate_downloaded


@pytest.mark.asyncio
async def test_outreach_sleeps_without_consuming_retries_when_waha_unavailable():
    class Pool:
        async def fetchval(self, *_args):
            return True

    class Waha:
        async def get_session_status(self):
            return "FAILED"

    worker = object.__new__(JobWorker)
    worker.pool = Pool()
    worker.waha_client = Waha()
    worker.whatsapp_enabled = True
    now = datetime.now(UTC)
    with pytest.raises(WhatsAppSendDeferred) as caught:
        await worker._prepare_supplier_outreach({"case_id": str(uuid4())})
    assert caught.value.reason == "waha_session_unavailable"
    assert 1799 <= (caught.value.available_at - now).total_seconds() <= 1801


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

    async def fake_handler(_self: JobWorker, value: dict[str, object], **_kwargs) -> None:
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
async def test_semantic_reply_extractor_runs_after_snapshot_connection_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        async def fetchval(self, *_args):
            return False

        def transaction(self):
            @asynccontextmanager
            async def manager():
                yield

            return manager()

    class Pool:
        def __init__(self) -> None:
            self.active_connections = 0
            self.connection = Connection()

        def acquire(self):
            @asynccontextmanager
            async def manager():
                self.active_connections += 1
                try:
                    yield self.connection
                finally:
                    self.active_connections -= 1

            return manager()

    pool = Pool()
    target = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=uuid4(),
        request_item_id=uuid4(),
        line_number=1,
        item_name="Марля",
        quantity=None,
        unit="рулон",
        specification_text="Медицинская марля",
    )
    snapshot = _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text="Цена 12 500 тг",
        targets=(target,),
    )
    extractor_calls: list[object] = []
    extractor_output = {
        "context_sha256": snapshot.context_sha256,
        "classification": "price_received",
        "classification_quote": "Цена 12 500 тг",
        "target_offer_id": str(target.offer_id),
        "target_item_id": str(target.request_item_id),
        "facts": [
            {
                "offer_id": str(target.offer_id),
                "item_id": str(target.request_item_id),
                "field": "price",
                "value": "12500",
                "evidence_quote": "Цена 12 500 тг",
            },
            {
                "offer_id": str(target.offer_id),
                "item_id": str(target.request_item_id),
                "field": "currency",
                "value": "KZT",
                "evidence_quote": "Цена 12 500 тг",
            },
        ],
        "reason": "Supplier quoted a price.",
    }

    class Extractor:
        async def extract(self, text, output_model, **kwargs):
            assert pool.active_connections == 0
            assert snapshot.message_text in text
            extractor_calls.append(kwargs)
            return output_model.model_validate(extractor_output)

    async def load_snapshot(*_args, **_kwargs):
        return snapshot

    projected: list[dict[str, object]] = []

    async def process(_connection, **kwargs):
        projected.append(kwargs)
        return SimpleNamespace(newly_processed=False)

    monkeypatch.setattr("procurement_bot.worker.load_semantic_reply_snapshot", load_snapshot)
    monkeypatch.setattr("procurement_bot.worker.process_supplier_reply", process)
    worker = object.__new__(JobWorker)
    worker.pool = pool
    worker.intake = SimpleNamespace(extractor=Extractor())
    worker.semantic_supplier_reply_enabled = True

    await worker._process_supplier_reply(
        {"conversation_id": str(snapshot.conversation_id), "message_id": str(snapshot.message_id)}
    )

    assert len(extractor_calls) == 1
    assert len(projected) == 1
    assert projected[0]["semantic_snapshot"] == snapshot
    assert projected[0]["semantic_proposal"].target_offer_id == target.offer_id
    assert projected[0]["semantic_proposal_error"] is None


@pytest.mark.asyncio
async def test_semantic_schema_failure_commits_ambiguity_without_regex_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        async def fetchval(self, *_args):
            return False

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

    target = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=uuid4(),
        request_item_id=uuid4(),
        line_number=1,
        item_name="Марля",
        quantity=None,
        unit="рулон",
        specification_text="Медицинская марля",
    )
    snapshot = _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text="Цена 12 500 тг",
        targets=(target,),
    )

    class InvalidExtractor:
        async def extract(self, *_args, **_kwargs):
            return SemanticReplyProposalOutput.model_validate(
                {
                    "context_sha256": snapshot.context_sha256,
                    "classification": "price_received",
                    "classification_quote": "invented quote",
                    "target_offer_id": str(target.offer_id),
                    "target_item_id": str(target.request_item_id),
                    "facts": [],
                    "reason": "Unsupported",
                }
            )

    async def load_snapshot(*_args, **_kwargs):
        return snapshot

    projected: list[dict[str, object]] = []

    async def process(_connection, **kwargs):
        projected.append(kwargs)
        return SimpleNamespace(newly_processed=False)

    monkeypatch.setattr("procurement_bot.worker.load_semantic_reply_snapshot", load_snapshot)
    monkeypatch.setattr("procurement_bot.worker.process_supplier_reply", process)
    worker = object.__new__(JobWorker)
    worker.pool = Pool()
    worker.intake = SimpleNamespace(extractor=InvalidExtractor())
    worker.semantic_supplier_reply_enabled = True

    await worker._process_supplier_reply(
        {"conversation_id": str(snapshot.conversation_id), "message_id": str(snapshot.message_id)}
    )

    assert projected[0]["semantic_snapshot"] == snapshot
    assert projected[0]["semantic_proposal"] is None
    assert "could not be validated" in projected[0]["semantic_proposal_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_code", "deferred"),
    [
        ("cli_usage_limit", True),
        ("cli_rate_limit", True),
        ("cli_auth_failed", False),
    ],
)
async def test_semantic_cli_quota_is_deferred_before_reply_projection(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_code: str,
    deferred: bool,
) -> None:
    class Connection:
        async def fetchval(self, *_args):
            return False

        def transaction(self):
            @asynccontextmanager
            async def manager():
                yield

            return manager()

    class Pool:
        def __init__(self) -> None:
            self.active_connections = 0
            self.connection = Connection()

        def acquire(self):
            @asynccontextmanager
            async def manager():
                self.active_connections += 1
                try:
                    yield self.connection
                finally:
                    self.active_connections -= 1

            return manager()

    pool = Pool()
    target = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=uuid4(),
        request_item_id=uuid4(),
        line_number=1,
        item_name="Марля",
        quantity=None,
        unit="рулон",
        specification_text="Медицинская марля",
    )
    snapshot = _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text="Буду после 15:00",
        targets=(target,),
    )
    status_error = AgyCliStatusError(diagnostic_code)

    class LimitedExtractor:
        async def extract(self, *_args, **_kwargs):
            assert pool.active_connections == 0
            raise status_error

    async def load_snapshot(*_args, **_kwargs):
        return snapshot

    projected: list[dict[str, object]] = []

    async def process(_connection, **kwargs):
        projected.append(kwargs)
        return SimpleNamespace(newly_processed=False)

    monkeypatch.setattr("procurement_bot.worker.load_semantic_reply_snapshot", load_snapshot)
    monkeypatch.setattr("procurement_bot.worker.process_supplier_reply", process)
    worker = object.__new__(JobWorker)
    worker.pool = pool
    worker.intake = SimpleNamespace(extractor=LimitedExtractor())
    worker.semantic_supplier_reply_enabled = True

    payload = {
        "conversation_id": str(snapshot.conversation_id),
        "message_id": str(snapshot.message_id),
    }
    before = datetime.now(UTC)
    if deferred:
        with pytest.raises(ProviderQuotaDeferred) as caught:
            await worker._process_supplier_reply(payload)
        assert 1799 <= (caught.value.available_at - before).total_seconds() <= 1801
    else:
        with pytest.raises(AgyCliStatusError) as caught:
            await worker._process_supplier_reply(payload)
        assert caught.value is status_error

    assert projected == []


@pytest.mark.asyncio
async def test_execute_research_fails_closed_without_live_adapter() -> None:
    worker = object.__new__(JobWorker)
    worker.research_executor = None

    with pytest.raises(PermanentProviderError, match="not configured"):
        await worker._execute_research({"research_run_id": str(uuid4())})


@pytest.mark.asyncio
async def test_lost_heartbeat_cancels_dispatch_and_retries_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={},
        attempts=1,
        max_attempts=3,
        idempotency_key="lease-loss",
        locked_by="worker",
        lease_expires_at=datetime.now(UTC),
    )
    cancelled = asyncio.Event()
    failed: list[tuple[object, bool]] = []

    async def claim(*_args, **_kwargs):
        return job

    async def dispatch(_self, _job):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def heartbeat(_self, _job):
        await asyncio.sleep(0)
        raise RetryableProviderError("job lease ownership was lost")

    async def fail(_pool, claimed, _error, *, retryable):
        assert claimed is job
        failed.append((claimed, retryable))
        return False

    monkeypatch.setattr("procurement_bot.worker.claim_job", claim)
    monkeypatch.setattr("procurement_bot.worker.fail_job", fail)
    monkeypatch.setattr("procurement_bot.worker.complete_job", lambda *_args: None)
    monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
    monkeypatch.setattr(JobWorker, "_heartbeat", heartbeat)
    worker = object.__new__(JobWorker)
    worker.pool = object()
    worker.worker_id = "worker"
    worker.lease_seconds = 60

    assert await worker.run_once()
    assert cancelled.is_set()
    assert failed == [(job, True)]


@pytest.mark.asyncio
async def test_parallel_heartbeat_cancels_after_pause_fences_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={"task_id": str(uuid4())},
        attempts=1,
        max_attempts=2,
        idempotency_key="paused-owner",
        locked_by="worker",
        lease_expires_at=datetime.now(UTC),
    )

    async def immediate_sleep(_seconds):
        return None

    async def renewed(*_args, **_kwargs):
        return True

    class Pool:
        async def fetchval(self, sql, *args):
            assert "grp.status='researching'" in sql
            assert args == (UUID(job.payload["task_id"]), job.id, "worker")
            return False

    monkeypatch.setattr("procurement_bot.worker.asyncio.sleep", immediate_sleep)
    monkeypatch.setattr("procurement_bot.worker.renew_job_lease", renewed)
    worker = object.__new__(JobWorker)
    worker.pool = Pool()
    worker.worker_id = "worker"
    worker.lease_seconds = 30

    with pytest.raises(RetryableProviderError, match="ownership was lost"):
        await worker._heartbeat(job)


@pytest.mark.asyncio
async def test_worker_cancellation_drains_dispatch_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={},
        attempts=1,
        max_attempts=3,
        idempotency_key="cancel",
        locked_by="worker",
        lease_expires_at=datetime.now(UTC),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def claim(*_args, **_kwargs):
        return job

    async def dispatch(_self, _job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def heartbeat(_self, _job):
        await asyncio.Event().wait()

    monkeypatch.setattr("procurement_bot.worker.claim_job", claim)
    monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
    monkeypatch.setattr(JobWorker, "_heartbeat", heartbeat)
    worker = object.__new__(JobWorker)
    worker.pool = object()
    worker.worker_id = "worker"
    worker.lease_seconds = 60

    running = asyncio.create_task(worker.run_once())
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_terminal_parallel_failure_reconciles_after_transaction_closes() -> None:
    events: list[str] = []

    class Connection:
        def transaction(self):
            @asynccontextmanager
            async def manager():
                events.append("transaction-open")
                try:
                    yield
                finally:
                    events.append("transaction-closed")

            return manager()

        async def fetchval(self, *_args):
            return None

    class Pool:
        def acquire(self):
            @asynccontextmanager
            async def manager():
                yield Connection()

            return manager()

    class Executor:
        async def reconcile(self, run_id):
            assert run_id == expected_run_id
            events.append("reconciled")

    expected_run_id = uuid4()
    worker = object.__new__(JobWorker)
    worker.pool = Pool()
    worker.research_executor = Executor()
    job = ClaimedJob(
        id=uuid4(),
        kind="parallel_research_task",
        payload={"research_run_id": str(expected_run_id)},
        attempts=2,
        max_attempts=2,
        idempotency_key="dead-parallel",
        locked_by="worker",
        lease_expires_at=datetime.now(UTC),
    )

    await worker._record_terminal_failure(job, "PermanentProviderError")

    assert events == ["transaction-open", "transaction-closed", "reconciled"]


@pytest.mark.asyncio
async def test_terminal_research_schedules_idempotent_report_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    case_id = uuid4()
    jobs: list[dict[str, object]] = []

    class Connection:
        async def fetchval(self, *_args):
            return "completion-1"

        async def fetchrow(self, *_args):
            # No opted-in owner research runtime is attached to this fake run.
            return None

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
            "kind": "refresh_report",
            "payload": {
                "case_id": str(case_id),
                "trigger_id": f"research-terminal:{run_id}:completion-1",
            },
            "idempotency_key": stable_key("terminal-report-refresh", run_id, "completion-1"),
        }
    ]


@pytest.mark.asyncio
async def test_whatsapp_policy_wait_defers_without_failure_or_completion(monkeypatch):
    from datetime import timedelta

    from procurement_bot.supplier_dialogue import WhatsAppSendDeferred

    available_at = datetime.now(UTC) + timedelta(hours=3)
    job = ClaimedJob(
        id=uuid4(),
        kind="enqueue_whatsapp_send",
        payload={},
        attempts=1,
        max_attempts=3,
        idempotency_key="quiet-wait",
        locked_by="worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    deferred = []

    async def claim(*_args, **_kwargs):
        return job

    async def dispatch(_self, _job):
        raise WhatsAppSendDeferred(available_at=available_at, reason="quiet_hours")

    async def heartbeat(_self, _job):
        await asyncio.Event().wait()

    async def defer(_pool, claimed, **kwargs):
        deferred.append((claimed, kwargs))
        return True

    async def forbidden(*_args, **_kwargs):
        pytest.fail("policy wait must not fail or complete the job")

    monkeypatch.setattr("procurement_bot.worker.claim_job", claim)
    monkeypatch.setattr("procurement_bot.worker.defer_job", defer)
    monkeypatch.setattr("procurement_bot.worker.fail_job", forbidden)
    monkeypatch.setattr("procurement_bot.worker.complete_job", forbidden)
    monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
    monkeypatch.setattr(JobWorker, "_heartbeat", heartbeat)
    worker = object.__new__(JobWorker)
    worker.pool = object()
    worker.worker_id = "worker"
    worker.lease_seconds = 300
    assert await worker.run_once() is True
    assert deferred == [(job, {"available_at": available_at, "reason": "quiet_hours"})]
