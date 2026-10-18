from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest

from procurement_bot.intake import ParsedProcurementRequest
from procurement_bot.operator_case_import import (
    CaseImportConflict,
    apply_operator_import,
    canonical_request_hash,
    item_content_sha256,
    item_projection_sha256,
    source_manifest_sha256,
    validate_item_projection,
    validate_operator_request,
    validate_requirement_projection,
)


def request() -> ParsedProcurementRequest:
    return ParsedProcurementRequest.model_validate(
        {
            "source_type": "document",
            "city": "Тараз",
            "search_scope": "citywide",
            "items": [
                {
                    "name": "Кабель силовой",
                    "quantity": "4",
                    "unit": "шт",
                    "tender_unit_price_private": None,
                    "source_reference": "таблица:строка 1",
                    "ambiguous_fields": ["сечение: проверить по фото"],
                    "requirements": [
                        {
                            "name": "Техническая характеристика",
                            "expected_value": "сечение 4x10 мм²",
                            "unit": None,
                            "source_reference": "таблица:строка 1",
                            "ambiguous": True,
                        }
                    ],
                }
            ],
        }
    )


def item_rows():
    return [
        {
            "id": "stable-item-id-1",
            "line_number": 1,
            "name": "Кабель силовой",
            "specification_text": "сечение 4x10 мм²",
            "quantity": Decimal("4"),
            "unit": "шт",
            "status": "draft",
            "analogs_allowed": None,
        }
    ]


def requirement_rows():
    return [
        {
            "request_item_id": "stable-item-id-1",
            "requirement_key": "Техническая характеристика",
            "operator": "equals",
            "expected_value": {"value": "сечение 4x10 мм²", "unit": None},
            "is_hard": True,
            "source_pointer": "таблица:строка 1",
        }
    ]


def test_validated_request_keeps_item_conflicts_and_unknown_finance_searchable():
    normalized = request()
    digest = canonical_request_hash(normalized)

    assert validate_operator_request(normalized, expected_request_sha256=digest) == digest
    assert normalized.items[0].tender_unit_price_private is None
    assert normalized.items[0].ambiguous_fields == ["сечение: проверить по фото"]
    assert normalized.items[0].requirements[0].ambiguous is True


def test_request_hash_and_geography_are_strict():
    normalized = request()
    with pytest.raises(CaseImportConflict, match="hash"):
        validate_operator_request(normalized, expected_request_sha256="0" * 64)
    with pytest.raises(CaseImportConflict, match="city"):
        validate_operator_request(
            normalized.model_copy(update={"city": "Алматы"}),
            expected_request_sha256=canonical_request_hash(
                normalized.model_copy(update={"city": "Алматы"})
            ),
        )


def test_unknown_currency_does_not_allow_an_unqualified_private_price():
    normalized = request().model_copy(
        update={
            "items": [
                request().items[0].model_copy(update={"tender_unit_price_private": Decimal("10")})
            ]
        }
    )
    with pytest.raises(CaseImportConflict, match="currency"):
        validate_operator_request(
            normalized,
            expected_request_sha256=canonical_request_hash(normalized),
        )


def test_projection_matches_without_replacing_stable_item_ids():
    normalized = request()
    rows = item_rows()
    validate_item_projection(normalized, rows)
    assert rows[0]["id"] == "stable-item-id-1"

    changed = [dict(rows[0], specification_text="сечение 4x16 мм²")]
    with pytest.raises(CaseImportConflict, match="specification_text"):
        validate_item_projection(normalized, changed)


def test_existing_requirement_evidence_must_match_reviewed_request():
    normalized = request()
    rows = item_rows()
    validate_requirement_projection(normalized, requirement_rows(), rows)

    changed = [dict(requirement_rows()[0], source_pointer="строка 2")]
    with pytest.raises(CaseImportConflict, match="requirement facts"):
        validate_requirement_projection(normalized, changed, rows)

    changed_operator = [dict(requirement_rows()[0], operator="minimum")]
    with pytest.raises(CaseImportConflict, match="requirement facts"):
        validate_requirement_projection(normalized, changed_operator, rows)


def test_requirement_facts_cannot_be_swapped_between_item_rows():
    normalized = request().model_copy(
        update={
            "items": [
                request().items[0],
                request()
                .items[0]
                .model_copy(
                    update={
                        "name": "Автоматический выключатель",
                        "source_reference": "таблица:строка 2",
                        "requirements": [
                            request()
                            .items[0]
                            .requirements[0]
                            .model_copy(
                                update={
                                    "name": "Техническая характеристика",
                                    "expected_value": "номинальный ток 25 А",
                                    "unit": None,
                                    "source_reference": "таблица:строка 2",
                                }
                            )
                        ],
                    }
                ),
            ]
        }
    )
    items = [
        item_rows()[0],
        dict(
            item_rows()[0],
            id="stable-item-id-2",
            line_number=2,
            name="Автоматический выключатель",
            specification_text="номинальный ток 25 А",
        ),
    ]
    swapped = [
        {
            **requirement_rows()[0],
            "request_item_id": "stable-item-id-2",
        },
        {
            **requirement_rows()[0],
            "request_item_id": "stable-item-id-1",
            "requirement_key": "Техническая характеристика",
            "operator": "equals",
            "expected_value": {"value": "номинальный ток 25 А", "unit": None},
            "source_pointer": "таблица:строка 2",
        },
    ]
    with pytest.raises(CaseImportConflict, match="requirement facts"):
        validate_requirement_projection(normalized, swapped, items)


def test_projection_count_mismatch_blocks_without_partial_import():
    with pytest.raises(CaseImportConflict, match="item count"):
        validate_item_projection(request(), [])


def test_projection_fingerprint_covers_exact_raw_spec_and_identity():
    rows = item_rows()
    first = item_projection_sha256(rows)
    assert first != item_projection_sha256([dict(rows[0], specification_text="35 см")])
    assert first != item_projection_sha256([dict(rows[0], id="different-id")])


def test_content_fingerprint_ignores_lifecycle_status_but_covers_content_and_identity():
    rows = item_rows()
    first = item_content_sha256(rows)
    assert first == item_content_sha256([dict(rows[0], status="ready")])
    assert first != item_content_sha256([dict(rows[0], name="changed")])
    assert first != item_content_sha256([dict(rows[0], id="different-id")])


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.snapshot = deepcopy(self.connection.state)

    async def __aexit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.connection.state = self.snapshot


class _Connection:
    def __init__(self, owner_id):
        self.owner_id = owner_id
        self.state = {"events": {}, "versions": [], "sql": []}
        self.items = item_rows()
        self.requirements = requirement_rows()
        self.case = {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "owner_user_id": owner_id,
            "source_kind": "manual",
            "status": "draft",
            "title": "Existing title",
            "city": "",
            "active": True,
        }

    def transaction(self):
        return _Transaction(self)

    async def execute(self, sql, *args):
        self.state["sql"].append(sql)
        if "UPDATE case_parse_versions SET status='superseded'" in sql:
            for version in self.state["versions"]:
                if version["status"] == "applied":
                    version["status"] = "superseded"
        elif "UPDATE procurement_cases SET city" in sql:
            self.case.update(city=args[1], status="ready")
        elif "UPDATE request_items SET status='ready'" in sql:
            pass
        elif "INSERT INTO workflow_events" in sql:
            self.state["events"][args[2]] = {"data": args[3]}

    async def fetchrow(self, sql, *args):
        if "FROM procurement_cases c JOIN users" in sql:
            return self.case
        if "FROM case_parse_versions" in sql:
            for version in self.state["versions"]:
                if version["status"] == "applied":
                    return version
            return None
        if "FROM workflow_events WHERE idempotency_key" in sql:
            return self.state["events"].get(args[0])
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM request_items" in sql:
            return self.items
        if "FROM item_requirements" in sql:
            return self.requirements
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "COALESCE(max(version),0)" in sql:
            return max((row["version"] for row in self.state["versions"]), default=0)
        if "INSERT INTO case_parse_versions" in sql:
            version = {
                "id": "parse-version-1",
                "version": args[1],
                "normalized_payload": args[2],
                "status": "applied",
            }
            self.state["versions"].append(version)
            return version["id"]
        if "INSERT INTO item_requirements" in sql:
            requirement_id = f"new-req-{len(self.requirements) + 1}"
            self.requirements.append(
                {
                    "request_item_id": str(args[0]),
                    "requirement_key": args[1],
                    "operator": "equals",
                    "expected_value": args[2],
                    "is_hard": args[3],
                    "source_pointer": args[4],
                    "id": requirement_id,
                }
            )
            return requirement_id
        raise AssertionError(sql)


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _artifact(digest="a" * 64):
    return [{"reference": "reviewed-source.docx", "sha256": digest}]


def _source_hash(artifacts):
    return source_manifest_sha256(artifacts)


@pytest.mark.asyncio
async def test_apply_appends_provenance_and_same_retry_is_idempotent():
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    digest = canonical_request_hash(normalized)
    connection = _Connection(owner_id)
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": digest,
        "expected_projection_sha256": item_projection_sha256(item_rows()),
        "source_sha256": _source_hash(_artifact()),
        "source_artifacts": _artifact(),
        "source_reference": "reviewed-source-artifact",
    }

    first = await apply_operator_import(_Pool(connection), **kwargs)
    second = await apply_operator_import(_Pool(connection), **kwargs)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert connection.case["status"] == "ready"
    assert connection.case["title"] == "Existing title"
    assert [row["id"] for row in connection.items] == ["stable-item-id-1"]
    assert len(connection.state["versions"]) == 1
    assert connection.state["events"]
    assert not any("DELETE FROM request_items" in sql for sql in connection.state["sql"])
    assert not any("request_item_financials" in sql for sql in connection.state["sql"])


@pytest.mark.asyncio
async def test_changed_source_hash_cannot_replay_applied_import():
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    digest = canonical_request_hash(normalized)
    connection = _Connection(owner_id)
    pool = _Pool(connection)
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": digest,
        "expected_projection_sha256": item_projection_sha256(item_rows()),
        "source_sha256": _source_hash(_artifact()),
        "source_artifacts": _artifact(),
        "source_reference": "reviewed-source-artifact",
    }
    await apply_operator_import(pool, **kwargs)

    changed_artifact = _artifact("b" * 64)
    with pytest.raises(CaseImportConflict, match="provenance"):
        await apply_operator_import(
            pool,
            **{
                **kwargs,
                "source_sha256": _source_hash(changed_artifact),
                "source_artifacts": changed_artifact,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"name": "edited name"}, {"id": "replacement-id"}])
async def test_idempotent_retry_rejects_item_content_or_identity_drift(changed):
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    connection = _Connection(owner_id)
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": canonical_request_hash(normalized),
        "expected_projection_sha256": item_projection_sha256(item_rows()),
        "source_sha256": _source_hash(_artifact()),
        "source_artifacts": _artifact(),
        "source_reference": "reviewed-source-artifact",
    }
    pool = _Pool(connection)
    await apply_operator_import(pool, **kwargs)
    connection.items[0].update(changed)

    with pytest.raises(CaseImportConflict, match="drifted"):
        await apply_operator_import(pool, **kwargs)


@pytest.mark.asyncio
async def test_idempotent_retry_allows_lifecycle_status_change():
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    connection = _Connection(owner_id)
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": canonical_request_hash(normalized),
        "expected_projection_sha256": item_projection_sha256(item_rows()),
        "source_sha256": _source_hash(_artifact()),
        "source_artifacts": _artifact(),
        "source_reference": "reviewed-source-artifact",
    }
    pool = _Pool(connection)
    await apply_operator_import(pool, **kwargs)
    connection.items[0]["status"] = "ready"

    result = await apply_operator_import(pool, **kwargs)

    assert result["idempotent"] is True


@pytest.mark.asyncio
async def test_apply_adds_missing_hard_requirement_without_replacing_items():
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    digest = canonical_request_hash(normalized)
    connection = _Connection(owner_id)
    connection.items[0]["id"] = "f919354e-3e5e-43f3-aaee-a0502b19ac0e"
    connection.requirements = []
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": digest,
        "expected_projection_sha256": item_projection_sha256(connection.items),
        "source_sha256": _source_hash(_artifact("c" * 64)),
        "source_artifacts": _artifact("c" * 64),
        "source_reference": "reviewed-source-artifact",
    }

    result = await apply_operator_import(_Pool(connection), **kwargs)

    assert result["idempotent"] is False
    assert len(connection.requirements) == 1
    assert connection.requirements[0]["request_item_id"] == connection.items[0]["id"]
    assert connection.requirements[0]["requirement_key"] == "Техническая характеристика"
    assert connection.requirements[0]["expected_value"] == {
        "value": "сечение 4x10 мм²",
        "unit": None,
    }
    assert not any("DELETE FROM request_items" in sql for sql in connection.state["sql"])


@pytest.mark.asyncio
async def test_projection_conflict_rolls_back_without_any_database_mutation():
    owner_id = UUID("22222222-2222-4222-8222-222222222222")
    case_id = UUID("11111111-1111-4111-8111-111111111111")
    normalized = request()
    connection = _Connection(owner_id)
    connection.items[0]["specification_text"] = "different source specification"
    kwargs = {
        "case_id": case_id,
        "owner_id": owner_id,
        "request": normalized,
        "expected_request_sha256": canonical_request_hash(normalized),
        "expected_projection_sha256": item_projection_sha256(item_rows()),
        "source_sha256": _source_hash(_artifact("d" * 64)),
        "source_artifacts": _artifact("d" * 64),
        "source_reference": "reviewed-source-artifact",
    }
    before = deepcopy(connection.state)

    with pytest.raises(CaseImportConflict, match="specification_text"):
        await apply_operator_import(_Pool(connection), **kwargs)

    assert connection.state == before


@pytest.mark.asyncio
async def test_cli_source_hash_mismatch_stops_before_database_access(tmp_path, monkeypatch):
    import hashlib
    import json
    from argparse import Namespace

    from scripts import reconcile_uranenergo_case as cli

    normalized = request()
    source_file = tmp_path / "reviewed-source.docx"
    source_file.write_bytes(b"source bytes")
    artifact = [{"reference": source_file.name, "sha256": "0" * 64}]
    envelope_path = tmp_path / "request.json"
    envelope_raw = json.dumps(
        {"source_artifacts": artifact, "request": normalized.model_dump(mode="json")},
        ensure_ascii=False,
    ).encode()
    envelope_path.write_bytes(envelope_raw)

    async def forbidden_db(*args, **kwargs):
        raise AssertionError("database must not be accessed after source hash mismatch")

    monkeypatch.setattr(cli, "create_pool", forbidden_db)
    args = Namespace(
        normalized_request_file=envelope_path,
        expected_request_file_sha256=hashlib.sha256(envelope_raw).hexdigest(),
        source_file=[source_file],
        expected_source_sha256=["0" * 64],
        case_id=UUID("11111111-1111-4111-8111-111111111111"),
        owner_id=UUID("22222222-2222-4222-8222-222222222222"),
        expected_request_sha256=canonical_request_hash(normalized),
        expected_projection_sha256="a" * 64,
        apply=False,
    )

    with pytest.raises(ValueError, match="source artifact hash mismatch"):
        await cli._run(args)
