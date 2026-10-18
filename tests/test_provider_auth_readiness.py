from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_autonomous_pipeline import pipeline_db  # noqa: F401, F811

from procurement_bot.config import Settings
from procurement_bot.health import health_snapshot, readiness_snapshot
from procurement_bot.mini_app import authenticate, readiness


def _settings_with_token_file(path):
    return Settings(
        _env_file=None,
        agy_auth_token_source=path,
        agy_home=path.parent / "agy-home",
        agy_browser_home=path.parent / "browser-home",
    )


async def _add_job(pool, *, status: str, error_code: str) -> object:
    return await pool.fetchval(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,last_error_code) "
        "VALUES('test_provider_auth_wait','{}'::jsonb,$1,$2,$3) RETURNING id",
        status,
        str(uuid4()),
        error_code,
    )


def _public_ready_request(pool, settings):
    return SimpleNamespace(
        path="/api/ready",
        app={"pool": pool, "settings": settings},
    )


@pytest.mark.postgres
async def test_auth_waits_block_public_readiness_until_credentials_and_jobs_recover(
    pipeline_db, tmp_path,  # noqa: F811
):
    pool, *_ = pipeline_db
    token_path = tmp_path / "antigravity-oauth-token"
    settings = _settings_with_token_file(token_path)
    missing_job = await _add_job(
        pool, status="pending", error_code="provider_auth_missing"
    )
    failed_job = await _add_job(
        pool, status="retry", error_code="provider_auth_failed"
    )
    request = _public_ready_request(pool, settings)

    missing_response = await authenticate(request, readiness)
    missing_body = json.loads(missing_response.text)
    assert missing_response.status == 503
    assert missing_body == {
        "status": "degraded",
        "database": "ok",
        "resources": True,
        "provider_credentials": False,
        "provider_authentication_blocked": True,
    }

    token_path.write_text("test-only-token", encoding="utf-8")
    waiting_response = await authenticate(request, readiness)
    waiting_body = json.loads(waiting_response.text)
    assert waiting_response.status == 503
    assert waiting_body["provider_credentials"] is True
    assert waiting_body["provider_authentication_blocked"] is True
    assert str(missing_job) not in waiting_response.text
    assert str(failed_job) not in waiting_response.text
    assert str(tmp_path) not in waiting_response.text
    assert all(
        word not in waiting_response.text.casefold()
        for word in ("owner", "contact", "telegram", "prompt", "case_id", "job_id")
    )

    await pool.execute(
        "UPDATE jobs SET status='succeeded',completed_at=now(),last_error_code='' "
        "WHERE id=ANY($1::uuid[])",
        [missing_job, failed_job],
    )
    recovered_response = await authenticate(request, readiness)
    recovered_body = json.loads(recovered_response.text)
    assert recovered_response.status == 200
    assert recovered_body["status"] == "ready"
    assert recovered_body["provider_credentials"] is True
    assert recovered_body["provider_authentication_blocked"] is False


@pytest.mark.postgres
async def test_historical_auth_failures_and_quota_waits_do_not_block_readiness(
    pipeline_db, tmp_path,  # noqa: F811
):
    pool, *_ = pipeline_db
    token_path = tmp_path / "antigravity-oauth-token"
    token_path.write_text("test-only-token", encoding="utf-8")
    settings = _settings_with_token_file(token_path)
    await _add_job(pool, status="dead", error_code="provider_auth_missing")
    await _add_job(pool, status="dead", error_code="provider_auth_failed")
    await _add_job(pool, status="retry", error_code="provider_quota_sleep")

    async with pool.acquire() as connection:
        snapshot = await readiness_snapshot(connection)
    assert snapshot["provider_auth_waiting_jobs"] == 0
    assert snapshot["provider_authentication_blocked"] is False
    assert snapshot["execution_ready"] is True

    response = await authenticate(
        _public_ready_request(pool, settings), readiness
    )
    assert response.status == 200
    assert json.loads(response.text)["provider_authentication_blocked"] is False


@pytest.mark.postgres
async def test_health_degrades_for_auth_waiting_jobs_even_when_token_exists(
    pipeline_db, tmp_path,  # noqa: F811
):
    pool, *_ = pipeline_db
    # Normalize only this disposable fixture so the health transition is isolated.
    await pool.execute(
        "UPDATE jobs SET status='succeeded',completed_at=COALESCE(completed_at,now()),"
        "locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,last_error_code=''"
    )
    await pool.execute(
        "UPDATE outbox_events SET status='sent',sent_at=COALESCE(sent_at,now()),"
        "locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,last_error_code=''"
    )
    token_path = tmp_path / "antigravity-oauth-token"
    token_path.write_text("test-only-token", encoding="utf-8")
    settings = _settings_with_token_file(token_path)

    baseline = await health_snapshot(pool, settings=settings)
    assert baseline["status"] == "ok"

    await _add_job(pool, status="pending", error_code="provider_auth_failed")
    degraded = await health_snapshot(pool, settings=settings)
    assert degraded["status"] == "degraded"
    assert degraded["readiness"]["provider_auth_waiting_jobs"] == 1
    assert degraded["readiness"]["provider_authentication_blocked"] is True
