from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_autonomous_pipeline import pipeline_db, seed_chat  # noqa: F401, F811

from procurement_bot.approvals import (
    approve_request,
    request_approval,
    require_and_consume_approval,
)
from procurement_bot.errors import ProviderAuthenticationDeferred
from procurement_bot.health import provider_auth_snapshot, readiness_snapshot
from procurement_bot.mini_app import authenticate, readiness
from procurement_bot.pipeline_reviews import load_reviews, reconcile_reviews, record_review
from procurement_bot.queue import ClaimedJob
from procurement_bot.transport_recovery import reconcile_history_acceptance
from procurement_bot.whatsapp_inbound import AuthenticatedWhatsAppInbound, accept_whatsapp_inbound
from procurement_bot.worker import JobWorker


@pytest.mark.postgres
async def test_reviews_deduplicate_owner_alerts_and_preserve_unknown_until_exact_history(
    pipeline_db,  # noqa: F811
):
    pool, case, owner, run, _ = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    payload = {"case_id": str(case), "channel": "whatsapp", "recipient": "+77000000004",
               "text": "Есть ли товар?", "conversation_id": str(chat),
               "logical_action_id": "review-test", "is_followup": False}
    async with pool.acquire() as c, c.transaction():
        approval = await request_approval(c, case_id=case, action_type="send_whatsapp",
                                          requested_by=owner, payload=payload)
        await approve_request(c, approval_id=approval, approved_by=owner)
        await require_and_consume_approval(c, case_id=case, action_type="send_whatsapp",
                                          payload=payload)
    event = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,status,idempotency_key,approval_id,"
        "provider_request_id,last_error_code) VALUES('whatsapp.send_text',$1,'retry',$2,$3,"
        "'review-reserved','provider_acceptance_unknown') RETURNING id",
        payload, str(uuid4()), approval,
    )
    async with pool.acquire() as c, c.transaction():
        assert (await reconcile_reviews(c, case))["counts"] == {"unknown_send": 1}
        await reconcile_reviews(c, case)
        await record_review(c, case_id=case, kind="unknown_send", subject_id=uuid4(),
                            conversation_id=chat, reason_code="provider_acceptance_unknown")
    assert await pool.fetchval(
        "SELECT count(*) FROM outbox_events WHERE event_type='telegram.send_message'"
    ) == 1
    proof = {"id": "true_77000000004@c.us_review-reserved", "fromMe": True,
             "body": payload["text"]}
    for incorrect in ({**proof, "fromMe": False}, {**proof, "body": "wrong"},
                      {**proof, "id": "true_77000000014@c.us_review-reserved"}):
        assert not await reconcile_history_acceptance(
            pool, session_name="test", conversation_id=chat,
            external_chat_id="77000000004@c.us", message=incorrect,
        )
    assert await pool.fetchval("SELECT status FROM outbox_events WHERE id=$1", event) == "retry"
    assert await reconcile_history_acceptance(
        pool, session_name="test", conversation_id=chat,
        external_chat_id="77000000004@c.us", message=proof,
    ) == 1
    assert await pool.fetchval("SELECT status FROM outbox_events WHERE id=$1", event) == "sent"
    assert await pool.fetchval(
        "SELECT count(*) FROM supplier_conversation_messages WHERE outbox_event_id=$1", event,
    ) == 1
    async with pool.acquire() as c, c.transaction():
        remaining = await reconcile_reviews(c, case)
    assert remaining["counts"] == {"unknown_send": 1}  # unrelated item must stay unknown
    assert not await reconcile_history_acceptance(
        pool, session_name="test", conversation_id=chat,
        external_chat_id="77000000004@c.us", message=proof,
    )


@pytest.mark.postgres
async def test_supplier_terminal_failure_notifies_case_owner_and_is_persisted(
    pipeline_db,  # noqa: F811
):
    pool, case, _owner, run, _ = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    async with pool.acquire() as c, c.transaction():
        await accept_whatsapp_inbound(c, AuthenticatedWhatsAppInbound(
            provider_event_id="terminal-review-inbound", session_name="test",
            sender_phone="+77000000004", provider_message_id="reply-1",
            received_at=datetime.now(UTC), text="Есть",
        ), worker_id="test")
    message = await pool.fetchval(
        "SELECT id FROM supplier_conversation_messages WHERE conversation_id=$1 "
        "AND external_message_id='reply-1'", chat,
    )
    job = ClaimedJob(id=uuid4(), kind="process_supplier_reply",
                     payload={"conversation_id": str(chat), "message_id": str(message)},
                     attempts=5, max_attempts=5, idempotency_key=str(uuid4()),
                     locked_by="test", lease_expires_at=datetime.now(UTC)+timedelta(minutes=1))
    worker = object.__new__(JobWorker)
    worker.pool = pool
    await worker._record_terminal_failure(job, "PermanentProviderError")
    await worker._record_terminal_failure(job, "PermanentProviderError")
    async with pool.acquire() as c:
        reviews = await load_reviews(c, case)
    assert reviews["counts"] == {"reply_processing_failed": 1}
    alert = await pool.fetchval(
        "SELECT payload FROM outbox_events WHERE event_type='telegram.send_message'"
    )
    assert alert["chat_id"] == 12345
    assert "Ответ поставщика сохранён" in alert["text"]
    assert await pool.fetchval(
        "SELECT text_content FROM supplier_conversation_messages WHERE id=$1", message,
    ) == "Есть"
    # A later episode sends a fresh alert after all earlier items were resolved.
    await pool.execute("UPDATE pipeline_reviews SET state='resolved',resolved_at=now()")
    await pool.execute(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,last_error_code) "
        "VALUES('process_supplier_reply',$1,'dead',$2,'reply_job_dead')",
        job.payload, str(uuid4()),
    )
    async with pool.acquire() as c, c.transaction():
        await reconcile_reviews(c, case)
    assert await pool.fetchval(
        "SELECT count(*) FROM outbox_events WHERE event_type='telegram.send_message'"
    ) == 2


@pytest.mark.postgres
async def test_dead_reply_with_exact_projection_proof_resolves_and_is_not_recreated(
    pipeline_db,  # noqa: F811
):
    pool, case, _owner, run, _ = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    async with pool.acquire() as connection, connection.transaction():
        await accept_whatsapp_inbound(connection, AuthenticatedWhatsAppInbound(
            provider_event_id="reply-review-late-commit", session_name="test",
            sender_phone="+77000000004", provider_message_id="late-commit-message",
            received_at=datetime.now(UTC), text="Есть в наличии",
        ), worker_id="review-proof-test")
    message = await pool.fetchval(
        "SELECT id FROM supplier_conversation_messages WHERE conversation_id=$1 "
        "AND external_message_id='late-commit-message'", chat,
    )
    job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,5,5,'lease_lost') "
        "RETURNING id",
        {"conversation_id": str(chat), "message_id": str(message)}, str(uuid4()),
    )
    event_data = {"case_id": str(case), "conversation_id": str(chat),
                  "message_id": str(message), "classification": "available"}
    await pool.execute(
        "INSERT INTO workflow_events(case_id,event_type,actor_type,idempotency_key,data) "
        "VALUES($1,'supplier_reply_processed','worker',$2,$3)",
        case, str(uuid4()), event_data,
    )
    async with pool.acquire() as connection, connection.transaction():
        await record_review(connection, case_id=case, kind="reply_processing_failed",
                            subject_id=job, conversation_id=chat, reason_code="lease_lost")
        resolved = await reconcile_reviews(connection, case)
    assert resolved["counts"] == {}
    assert await pool.fetchval(
        "SELECT state FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2", case, job,
    ) == "resolved"

    # Existing resolved reviews stay resolved; dead-job backfill must also avoid
    # creating a review when canonical projection proof predates reconciliation.
    async with pool.acquire() as connection, connection.transaction():
        repeated = await reconcile_reviews(connection, case)
    assert repeated["counts"] == {}
    assert await pool.fetchval(
        "SELECT count(*) FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2 "
        "AND state='open'", case, job,
    ) == 0

    async with pool.acquire() as connection, connection.transaction():
        await accept_whatsapp_inbound(connection, AuthenticatedWhatsAppInbound(
            provider_event_id="reply-review-late-commit-no-review", session_name="test",
            sender_phone="+77000000004", provider_message_id="second-late-commit",
            received_at=datetime.now(UTC), text="Есть",
        ), worker_id="review-proof-test")
    second_message = await pool.fetchval(
        "SELECT id FROM supplier_conversation_messages WHERE conversation_id=$1 "
        "AND external_message_id='second-late-commit'", chat,
    )
    second_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,5,5,'lease_lost') "
        "RETURNING id",
        {"conversation_id": str(chat), "message_id": str(second_message)}, str(uuid4()),
    )
    await pool.execute(
        "INSERT INTO workflow_events(case_id,event_type,actor_type,idempotency_key,data) "
        "VALUES($1,'supplier_reply_processed','worker',$2,$3)",
        case, str(uuid4()), {**event_data, "message_id": str(second_message)},
    )
    async with pool.acquire() as connection, connection.transaction():
        assert (await reconcile_reviews(connection, case))["counts"] == {}
    assert await pool.fetchval(
        "SELECT count(*) FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2",
        case, second_job,
    ) == 0


@pytest.mark.postgres
async def test_dead_reply_without_case_scoped_projection_proof_stays_open(
    pipeline_db,  # noqa: F811
):
    pool, case, _owner, run, _ = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    async with pool.acquire() as connection, connection.transaction():
        await accept_whatsapp_inbound(connection, AuthenticatedWhatsAppInbound(
            provider_event_id="reply-review-cross-case", session_name="test",
            sender_phone="+77000000004", provider_message_id="unprocessed-message",
            received_at=datetime.now(UTC), text="Не обработано",
        ), worker_id="review-proof-test")
    message = await pool.fetchval(
        "SELECT id FROM supplier_conversation_messages WHERE conversation_id=$1 "
        "AND external_message_id='unprocessed-message'", chat,
    )
    job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,5,5,'failed') "
        "RETURNING id",
        {"conversation_id": str(chat), "message_id": str(message)}, str(uuid4()),
    )
    succeeded_without_proof = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts) "
        "VALUES('process_supplier_reply',$1,'succeeded',$2,1,5) RETURNING id",
        {"conversation_id": str(chat), "message_id": str(message)}, str(uuid4()),
    )
    missing_message_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts) "
        "VALUES('process_supplier_reply',$1,'dead',$2,5,5) RETURNING id",
        {"conversation_id": str(chat), "message_id": str(uuid4())}, str(uuid4()),
    )
    wrong_kind_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts) "
        "VALUES('process_supplier_media',$1,'dead',$2,5,5) RETURNING id",
        {"conversation_id": str(chat), "message_id": str(message)}, str(uuid4()),
    )
    other_case = await pool.fetchval(
        "INSERT INTO procurement_cases(owner_user_id,title,city,status) "
        "SELECT owner_user_id,'Other reply case','Алматы','researching' "
        "FROM procurement_cases WHERE id=$1 RETURNING id", case,
    )
    await pool.execute(
        "INSERT INTO workflow_events(case_id,event_type,actor_type,idempotency_key,data) "
        "VALUES($1,'supplier_reply_processed','worker',$2,$3)",
        other_case, str(uuid4()), {"case_id": str(other_case), "conversation_id": str(chat),
                                  "message_id": str(message)},
    )
    async with pool.acquire() as connection, connection.transaction():
        await record_review(connection, case_id=case, kind="reply_processing_failed",
                            subject_id=succeeded_without_proof, conversation_id=chat,
                            reason_code="missing_canonical_event")
        first = await reconcile_reviews(connection, case)
        second = await reconcile_reviews(connection, case)
    assert first["counts"] == {"reply_processing_failed": 1}
    assert second["counts"] == {"reply_processing_failed": 1}
    assert await pool.fetchval(
        "SELECT count(*) FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2 "
        "AND state='open'", case, job,
    ) == 1
    assert await pool.fetchval(
        "SELECT state FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2",
        case, succeeded_without_proof,
    ) == "resolved"
    for excluded_job in (missing_message_job, wrong_kind_job):
        assert await pool.fetchval(
            "SELECT count(*) FROM pipeline_reviews WHERE case_id=$1 AND subject_id=$2",
            case, excluded_job,
        ) == 0


@pytest.mark.postgres
async def test_review_context_is_case_scoped_and_tolerates_missing_sources(
    pipeline_db,  # noqa: F811
):
    pool, case, _owner, run, _ = pipeline_db
    chat, supplier, *_ = await seed_chat(pool, case, run, 1)
    inbound_ids = []
    reply_texts = ("Цена 2500 тг", "<script>не запускать</script>" + "т" * 1200)
    for suffix, text in zip(("blocked", "failed"), reply_texts, strict=True):
        async with pool.acquire() as connection, connection.transaction():
            await accept_whatsapp_inbound(connection, AuthenticatedWhatsAppInbound(
                provider_event_id="review-context-" + suffix, session_name="test",
                sender_phone="+77000000004", provider_message_id="reply-" + suffix,
                received_at=datetime.now(UTC), text=text,
            ), worker_id="review-context-test")
        inbound_ids.append(await pool.fetchval(
            "SELECT id FROM supplier_conversation_messages WHERE conversation_id=$1 "
            "AND external_message_id=$2", chat, "reply-" + suffix,
        ))

    outbound_payload = {"case_id": str(case), "channel": "whatsapp",
                        "recipient": "+77000000004", "text": "Запрос цены" + "т" * 1200,
                        "conversation_id": str(chat), "logical_action_id": "review-context",
                        "is_followup": False}
    async with pool.acquire() as connection, connection.transaction():
        approval = await request_approval(
            connection, case_id=case, action_type="send_whatsapp",
            requested_by=_owner, payload=outbound_payload,
        )
        await approve_request(connection, approval_id=approval, approved_by=_owner)
        await require_and_consume_approval(
            connection, case_id=case, action_type="send_whatsapp", payload=outbound_payload,
        )
    unknown_event = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,status,idempotency_key,"
        "provider_request_id,last_error_code,approval_id) "
        "VALUES('whatsapp.send_text',$1,'retry',$2,'provider-request-context',"
        "'provider_acceptance_unknown',$3) RETURNING id",
        outbound_payload, str(uuid4()), approval,
    )
    dead_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,4,5,'reply_failed') "
        "RETURNING id",
        {"conversation_id": str(chat), "message_id": str(inbound_ids[1])}, str(uuid4()),
    )

    other_case = await pool.fetchval(
        "INSERT INTO procurement_cases(owner_user_id,title,city,status) "
        "SELECT owner_user_id,'Other case','Алматы','researching' "
        "FROM procurement_cases WHERE id=$1 RETURNING id", case,
    )
    contact_id = await pool.fetchval(
        "SELECT supplier_contact_id FROM supplier_conversations WHERE id=$1", chat,
    )
    foreign_chat = await pool.fetchval(
        "INSERT INTO supplier_conversations(case_id,supplier_id,supplier_contact_id,"
        "session_name,external_chat_id,state,idempotency_key) VALUES($1,$2,$3,'test',"
        "'77000000004@foreign.c.us','waiting_price',$4) RETURNING id",
        other_case, supplier, contact_id, str(uuid4()),
    )
    foreign_event = await pool.fetchval(
        "INSERT INTO outbox_events(event_type,payload,status,idempotency_key) "
        "VALUES('telegram.send_message',$1,'pending',$2) RETURNING id",
        {"case_id": str(other_case), "conversation_id": str(chat),
         "text": "Чужой контекст не показывать"}, str(uuid4()),
    )
    missing_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,5,5,'source_missing') "
        "RETURNING id",
        {"conversation_id": str(chat), "message_id": str(uuid4())}, str(uuid4()),
    )
    foreign_job = await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,attempts,max_attempts,"
        "last_error_code) VALUES('process_supplier_reply',$1,'dead',$2,5,5,'foreign_job') "
        "RETURNING id",
        {"case_id": str(other_case), "conversation_id": str(chat),
         "message_id": str(inbound_ids[1])}, str(uuid4()),
    )

    async with pool.acquire() as connection, connection.transaction():
        await record_review(connection, case_id=case, kind="unknown_send",
                            subject_id=unknown_event, conversation_id=chat,
                            reason_code="provider_acceptance_unknown")
        await record_review(connection, case_id=case, kind="blocked_reply",
                            subject_id=inbound_ids[0], conversation_id=chat,
                            reason_code="provider_acceptance_unknown")
        await record_review(connection, case_id=case, kind="reply_processing_failed",
                            subject_id=dead_job, conversation_id=chat,
                            reason_code="reply_failed")
        missing_event = uuid4()
        await record_review(connection, case_id=case, kind="unknown_send",
                            subject_id=missing_event, conversation_id=chat,
                            reason_code="source_missing")
        await record_review(connection, case_id=case, kind="blocked_reply",
                            subject_id=uuid4(), conversation_id=None,
                            reason_code="conversation_missing")
        await record_review(connection, case_id=case, kind="blocked_reply",
                            subject_id=uuid4(), conversation_id=chat,
                            reason_code="message_missing")
        await record_review(connection, case_id=case, kind="blocked_reply",
                            subject_id=uuid4(), conversation_id=foreign_chat,
                            reason_code="cross_case_conversation")
        await record_review(connection, case_id=case, kind="unknown_send",
                            subject_id=foreign_event, conversation_id=chat,
                            reason_code="cross_case_outbox")
        await record_review(connection, case_id=case, kind="reply_processing_failed",
                            subject_id=missing_job, conversation_id=chat,
                            reason_code="message_missing")
        await record_review(connection, case_id=case, kind="reply_processing_failed",
                            subject_id=foreign_job, conversation_id=chat,
                            reason_code="cross_case_job")
        reviews = await load_reviews(connection, case)

    by_subject = {row["subject_id"]: row for row in reviews["items"]}
    unknown = by_subject[unknown_event]
    assert unknown["supplier_name"] == "test"
    assert unknown["supplier_contact"] == "+77000000004"
    assert unknown["conversation_state"] == "waiting_availability"
    assert unknown["external_chat_id"] == "77000000004@c.us"
    assert unknown["outbound_status"] == "retry"
    assert unknown["provider_request_id"] == "provider-request-context"
    assert unknown["outbound_error_code"] == "provider_acceptance_unknown"
    assert unknown["outbound_text"] == outbound_payload["text"][:1000]
    assert unknown["outbound_text_truncated"] is True

    blocked = by_subject[inbound_ids[0]]
    assert blocked["inbound_text"] == "Цена 2500 тг"
    assert blocked["inbound_received_at"] is not None

    failed = by_subject[dead_job]
    assert failed["inbound_text"] == reply_texts[1][:1000]
    assert failed["inbound_text_truncated"] is True
    assert failed["reply_job_status"] == "dead"
    assert failed["reply_job_attempts"] == 4
    assert failed["reply_job_max_attempts"] == 5
    assert failed["reply_job_error_code"] == "reply_failed"

    missing = by_subject[missing_event]
    assert missing["supplier_name"] == "test"
    assert missing["outbound_status"] is None
    assert missing["outbound_text"] is None
    foreign_outbox = by_subject[foreign_event]
    assert foreign_outbox["outbound_status"] is None
    assert foreign_outbox["outbound_text"] is None
    missing_job_context = by_subject[missing_job]
    assert missing_job_context["reply_job_status"] == "dead"
    assert missing_job_context["inbound_text"] is None
    foreign_job_context = by_subject[foreign_job]
    assert foreign_job_context["reply_job_status"] is None
    assert foreign_job_context["reply_job_error_code"] is None
    assert foreign_job_context["inbound_text"] is None
    missing_message = next(row for row in reviews["items"]
                           if row["reason_code"] == "message_missing")
    assert missing_message["supplier_name"] == "test"
    assert missing_message["inbound_text"] is None
    null_chat = next(row for row in reviews["items"]
                     if row["reason_code"] == "conversation_missing")
    assert null_chat["supplier_name"] is None
    cross_case = next(row for row in reviews["items"]
                      if row["reason_code"] == "cross_case_conversation")
    assert cross_case["supplier_name"] is None
    assert cross_case["external_chat_id"] is None


@pytest.mark.postgres
async def test_readiness_ignores_historic_dead_jobs_but_detects_overdue_execution(
    pipeline_db,  # noqa: F811
):
    pool, case, _owner, _run, _ = pipeline_db
    await pool.execute("UPDATE jobs SET status='dead'")
    async with pool.acquire() as c:
        assert (await readiness_snapshot(c))["execution_ready"]
    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=now()-interval '2 hours' WHERE case_id=$1",
        case,
    )
    async with pool.acquire() as c:
        assert not (await readiness_snapshot(c))["execution_ready"]


@pytest.mark.postgres
async def test_readiness_api_is_public_and_distinguishes_resource_or_execution_failure(
    pipeline_db, monkeypatch, tmp_path,  # noqa: F811
):
    pool, case, *_ = pipeline_db
    request = SimpleNamespace(path="/api/ready", app={"pool": pool})
    assert (await authenticate(request, readiness)).status == 200
    monkeypatch.setattr("procurement_bot.mini_app.STATIC", tmp_path)
    failed = await authenticate(request, readiness)
    assert failed.status == 503
    assert "case" not in failed.text and "telegram" not in failed.text
    await pool.execute(
        "UPDATE case_pipeline_runtime SET next_tick_at=now()-interval '2 hours' WHERE case_id=$1",
        case,
    )
    assert (await readiness(request)).status == 503


async def test_readiness_api_returns_unavailable_without_database_details():
    class UnavailablePool:
        def acquire(self, **_kwargs):
            raise RuntimeError("secret-dsn-and-host")

    response = await readiness(SimpleNamespace(app={"pool": UnavailablePool()}))
    assert response.status == 503
    assert response.text == '{"status": "unavailable"}'


@pytest.mark.postgres
@pytest.mark.parametrize("lease_expired,legacy_accepted,expected", [
    (False, False, 0), (True, False, 1), (True, True, 0),
])
async def test_review_scan_distinguishes_active_leases_and_legacy_acceptance(
    pipeline_db, lease_expired, legacy_accepted, expected,  # noqa: F811
):
    pool, case, owner, run, _ = pipeline_db
    chat, *_ = await seed_chat(pool, case, run, 1)
    payload = {"case_id": str(case), "channel": "whatsapp", "recipient": "+77000000004",
               "text": "Есть товар?", "conversation_id": str(chat),
               "logical_action_id": "lease-review-test", "is_followup": False}
    async with pool.acquire() as c:
        approval = await request_approval(c, case_id=case, action_type="send_whatsapp",
                                          requested_by=owner, payload=payload)
    await pool.execute(
        "INSERT INTO outbox_events(event_type,payload,status,idempotency_key,"
        "provider_request_id,external_id,locked_at,locked_by,lease_expires_at,approval_id) "
        "VALUES('whatsapp.send_text',$1,'sending',$2,'inflight-id',$3,now(),'worker',$4,$5)",
        payload, str(uuid4()),
        "legacy-provider-id" if legacy_accepted else None,
        datetime.now(UTC) + timedelta(minutes=-1 if lease_expired else 1),
        approval,
    )
    async with pool.acquire() as c, c.transaction():
        reviews = await reconcile_reviews(c, case)
    assert reviews["counts"].get("unknown_send", 0) == expected
    assert await pool.fetchval(
        "SELECT count(*) FROM outbox_events WHERE event_type='telegram.send_message'"
    ) == expected


@pytest.mark.postgres
async def test_auth_missing_preserves_real_job_and_checkpoint_attempts(
    pipeline_db, monkeypatch,  # noqa: F811
):
    pool, _case, _owner, run, executor = pipeline_db

    class AuthMissingBrowser:
        async def search_suppliers(self, _plan):
            raise ProviderAuthenticationDeferred()

    executor.browser = AuthMissingBrowser()

    async def dispatch(_worker, job):
        await executor.execute_task(job.payload, job_id=job.id, worker_id=job.locked_by)

    monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
    worker = object.__new__(JobWorker)
    worker.pool, worker.worker_id, worker.lease_seconds = pool, "auth-wait-test", 300
    for _attempt in range(16):
        assert await worker.run_once()
        task = await pool.fetchrow(
            "SELECT cp.id,cp.current_job_id FROM research_branch_checkpoints cp "
            "JOIN jobs j ON j.id=cp.current_job_id WHERE cp.research_run_id=$1 "
            "AND j.last_error_code='provider_auth_missing' ORDER BY cp.id LIMIT 1", run,
        )
        if task:
            break
    assert task is not None
    row = await pool.fetchrow("SELECT * FROM jobs WHERE id=$1", task["current_job_id"])
    checkpoint = await pool.fetchrow(
        "SELECT * FROM research_branch_checkpoints WHERE id=$1", task["id"],
    )
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["last_error_code"] == "provider_auth_missing"
    assert row["available_at"] > datetime.now(UTC)+timedelta(minutes=29)
    assert checkpoint["status"] == "queued" and checkpoint["batch_attempts"] == 0
    assert checkpoint["owner_worker"] is None and checkpoint["lease_until"] is None
    assert checkpoint["last_error_code"] == "provider_auth_missing"
    assert await pool.fetchval("SELECT count(*) FROM research_provider_waits") == 0


def test_auth_readiness_never_claims_account_eligibility_or_exposes_paths():
    class MissingSettings:
        def _available_agy_auth_token(self):
            raise ValueError("private-auth-path")

    assert provider_auth_snapshot(MissingSettings()) == {
        "available": False, "reason": "provider_auth_missing",
    }

    class PresentSettings:
        def _available_agy_auth_token(self):
            return "private-auth-path"

    assert provider_auth_snapshot(PresentSettings()) == {
        "available": True, "reason": "credentials_present_not_account_verified",
    }
