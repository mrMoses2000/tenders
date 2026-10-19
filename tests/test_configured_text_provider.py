"""Native construction and application boundaries, with synthetic provider responses."""

import json
from pathlib import Path

import pytest
from test_autonomous_pipeline import pipeline_db  # noqa: F401, F811
from test_search_profiles import _Connection, _profile
from test_semantic_supplier_reply import _proposal, _snapshot

from procurement_bot import cli
from procurement_bot.codex_text_extractor import CliResult, CodexTextError, OptInCodexTextExtractor
from procurement_bot.config import Settings
from procurement_bot.configured_extractor import (
    ConfiguredAgyAudioTranscriber,
    ConfiguredAgyExtractor,
    ConfiguredStructuredExtractor,
)
from procurement_bot.errors import ProviderAuthenticationDeferred
from procurement_bot.health import provider_auth_snapshot
from procurement_bot.intake import IntakeService, ParsedProcurementRequest, ProcurementRequestPatch
from procurement_bot.search_profiles import load_search_item_snapshot, propose_search_profile
from procurement_bot.semantic_supplier_reply import propose_semantic_reply


def settings(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        agy_home=tmp_path / "agy",
        agy_browser_home=tmp_path / "browser",
        agy_auth_token_source=tmp_path / "missing-agy",
        codex_text_home=tmp_path,
        codex_text_codex_home=tmp_path / "codex",
        **kwargs,
    )


def runner_for(result, *, wrong_transport=None, wrong_result=None):
    async def run(call):
        payload = json.loads(call.prompt.decode().split("\n", 1)[1])
        bindings = {name: payload[name] for name in ("spec_sha256", "context_sha256")}
        output = result(payload) if callable(result) else result
        if wrong_result:
            output[wrong_result] = "f" * 64
        value = {**bindings, "result": output}
        if wrong_transport:
            value[wrong_transport] = "f" * 64
        call.output_path.write_text(json.dumps(value))
        return CliResult(0)

    return run


@pytest.mark.asyncio
async def test_default_agy_missing_auth_never_falls_back_to_codex(tmp_path, monkeypatch):
    selected = ConfiguredStructuredExtractor(settings(tmp_path))
    assert selected.provider == "agy"
    assert isinstance(selected._extractor, ConfiguredAgyExtractor)

    async def forbidden(*args, **kwargs):
        pytest.fail("missing AGY auth must not call Codex")

    monkeypatch.setattr(OptInCodexTextExtractor, "extract", forbidden)
    with pytest.raises(ProviderAuthenticationDeferred, match="provider_auth_missing"):
        await selected.extract("fixture", ParsedProcurementRequest)


@pytest.mark.parametrize("provider", ["auto", "fallback", "CODEX", ""])
def test_provider_enum_is_explicit(tmp_path, provider):
    with pytest.raises(ValueError):
        settings(tmp_path, structured_text_provider=provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [None, "spec_sha256", "context_sha256"])
async def test_native_intake_uses_selected_provider_and_rejects_inner_hash_drift(tmp_path, wrong):
    configured = ConfiguredStructuredExtractor(settings(tmp_path, structured_text_provider="codex"))
    assert isinstance(configured._extractor, OptInCodexTextExtractor)
    request = ParsedProcurementRequest(city="Тараз").model_dump(mode="json")
    configured._extractor._runner = runner_for(
        lambda p: {
            "spec_sha256": p["spec_sha256"],
            "context_sha256": p["context_sha256"],
            "request": request,
        },
        wrong_result=wrong,
    )
    service = IntakeService(configured)
    if wrong:
        with pytest.raises(CodexTextError, match="result_hash_mismatch"):
            await service.process("Synthetic intake fixture")
    else:
        result = await service.process("Synthetic intake fixture")
        assert isinstance(result.patch, ProcurementRequestPatch)
        assert result.request.city == "Тараз"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [None, "spec_sha256", "context_sha256"])
async def test_native_research_profile_preserves_contract_and_binds_transport(tmp_path, wrong):
    configured = ConfiguredStructuredExtractor(settings(tmp_path, structured_text_provider="codex"))
    configured._extractor._runner = runner_for(
        _profile().model_dump(mode="json"), wrong_transport=wrong
    )
    connection = _Connection()
    snapshot = await load_search_item_snapshot(connection, connection.run_id, connection.item_id)
    if wrong:
        with pytest.raises(CodexTextError, match="invalid_schema_result"):
            await propose_search_profile(configured, snapshot)
    else:
        assert await propose_search_profile(configured, snapshot) == _profile()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [None, "context_sha256"])
async def test_native_semantic_reply_rejects_inner_context_drift(tmp_path, wrong):
    snapshot = _snapshot()
    configured = ConfiguredStructuredExtractor(settings(tmp_path, structured_text_provider="codex"))
    configured._extractor._runner = runner_for(_proposal(snapshot), wrong_result=wrong)
    if wrong:
        with pytest.raises(CodexTextError, match="result_hash_mismatch"):
            await propose_semantic_reply(configured, snapshot)
    else:
        result = await propose_semantic_reply(configured, snapshot)
        assert result.context_sha256 == snapshot.context_sha256


def test_readiness_tracks_selected_text_and_required_agy_audio(tmp_path):
    from sys import executable

    selected = settings(
        tmp_path,
        structured_text_provider="codex",
        voice_provider="disabled",
        codex_text_executable=Path(executable),
        codex_text_node_executable=Path(executable),
    )
    assert not provider_auth_snapshot(selected)["available"]
    selected.codex_text_codex_home.mkdir()
    (selected.codex_text_codex_home / "auth.json").write_text("synthetic credential sentinel")
    assert provider_auth_snapshot(selected) == {
        "available": True,
        "reason": "credentials_present_not_account_verified",
    }
    assert not provider_auth_snapshot(selected.model_copy(update={"voice_provider": "agy"}))[
        "available"
    ]
    assert not provider_auth_snapshot(selected.model_copy(update={"supplier_media_enabled": True}))[
        "available"
    ]
    assert not provider_auth_snapshot(
        selected.model_copy(update={"browser_research_enabled": True})
    )["available"]
    assert not provider_auth_snapshot(
        selected.model_copy(update={"structured_text_provider": "agy"})
    )["available"]


@pytest.mark.parametrize("provider", ["agy", "codex"])
def test_worker_wires_same_selected_text_into_native_consumers_and_keeps_agy_voice(
    tmp_path, monkeypatch, provider
):
    selected = settings(
        tmp_path,
        structured_text_provider=provider,
        worker_concurrency=2,
        media_root=tmp_path / "media",
        semantic_supplier_reply_enabled=True,
    )
    captured, research = [], []

    class FakeWorker:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def run_forever(self):
            pass

    class Outbox:
        def __init__(self, **kwargs):
            pass

        async def run_forever(self):
            pass

    class Tasks:
        def create_task(self, coroutine):
            coroutine.close()

    def build(_settings, _pool, *, profile_extractor):
        research.append(profile_extractor)

    monkeypatch.setattr(cli, "JobWorker", FakeWorker)
    monkeypatch.setattr(cli, "TelegramOutboxWorker", Outbox)
    monkeypatch.setattr(cli, "_build_research_executor", build)
    cli._start_worker_tasks(Tasks(), selected, object(), object())
    assert len(captured) == 2
    assert research == [captured[0]["intake"].extractor]
    assert captured[1]["intake"].extractor is research[0]
    assert research[0].provider == provider
    assert isinstance(captured[0]["transcriber"], ConfiguredAgyAudioTranscriber)
    assert not selected.agy_home.exists()


@pytest.mark.asyncio
async def test_missing_agy_voice_auth_never_calls_text_provider(tmp_path):
    voice = ConfiguredAgyAudioTranscriber(settings(tmp_path, structured_text_provider="codex"))
    with pytest.raises(ProviderAuthenticationDeferred):
        await voice.transcribe(tmp_path / "synthetic-missing-audio")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_selected_codex_native_readiness_keeps_auth_job_guards(
    pipeline_db,  # noqa: F811
    tmp_path,
):
    from sys import executable
    from types import SimpleNamespace
    from uuid import uuid4

    from procurement_bot.mini_app import readiness

    pool, *_ = pipeline_db
    selected = settings(
        tmp_path,
        structured_text_provider="codex",
        voice_provider="disabled",
        codex_text_executable=Path(executable),
        codex_text_node_executable=Path(executable),
    )
    selected.codex_text_codex_home.mkdir()
    (selected.codex_text_codex_home / "auth.json").write_text("synthetic sentinel")
    request = SimpleNamespace(app={"pool": pool, "settings": selected})
    assert (await readiness(request)).status == 200
    await pool.execute(
        "INSERT INTO jobs(kind,payload,status,idempotency_key,last_error_code) "
        "VALUES('synthetic_text','{}'::jsonb,'retry',$1,'provider_auth_failed')",
        str(uuid4()),
    )
    response = await readiness(request)
    assert response.status == 503
    assert json.loads(response.text)["provider_authentication_blocked"]


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [False, True])
async def test_selected_codex_profile_persisted_only_after_validated_proposal(
    pipeline_db,  # noqa: F811
    tmp_path,
    wrong,
):
    from procurement_bot.search_profiles import persist_search_profile

    pool, case, _, run_id, _ = pipeline_db
    item_id = await pool.fetchval(
        "SELECT id FROM request_items WHERE case_id=$1 ORDER BY line_number LIMIT 1",
        case,
    )
    selected = ConfiguredStructuredExtractor(settings(tmp_path, structured_text_provider="codex"))
    selected._extractor._runner = runner_for(
        _profile().model_dump(mode="json"),
        wrong_transport="spec_sha256" if wrong else None,
    )
    async with pool.acquire() as connection:
        snapshot = await load_search_item_snapshot(connection, run_id, item_id)
    if wrong:
        with pytest.raises(CodexTextError, match="invalid_schema_result"):
            await propose_search_profile(selected, snapshot)
        assert await pool.fetchval("SELECT count(*) FROM research_search_profiles") == 0
    else:
        profile = await propose_search_profile(selected, snapshot)
        async with pool.acquire() as connection:
            async with connection.transaction():
                saved = await persist_search_profile(connection, snapshot, profile)
        assert saved.created
        assert await pool.fetchval("SELECT count(*) FROM research_search_profiles") == 1
    assert await pool.fetchval("SELECT count(*) FROM request_items WHERE case_id=$1", case) == 4
