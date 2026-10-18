"""Explicit, append-only import of an operator-reviewed case request."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.intake import ParsedProcurementRequest
from procurement_bot.queue import stable_key


class CaseImportConflict(ValueError):
    """The existing case no longer matches the reviewed import preconditions."""


def item_projection_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash the full current projection, including raw spec text and stable IDs."""

    projection = [
        {
            "id": str(row["id"]),
            "line_number": int(row["line_number"]),
            "name": row["name"],
            "specification_text": row["specification_text"],
            "quantity": None if row["quantity"] is None else str(row["quantity"]),
            "unit": row["unit"],
            "status": row["status"],
            "analogs_allowed": row["analogs_allowed"],
        }
        for row in rows
    ]
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def item_content_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash stable item identity and reviewed content, excluding lifecycle status."""

    projection = [
        {
            "id": str(row["id"]),
            "line_number": int(row["line_number"]),
            "name": row["name"],
            "specification_text": row["specification_text"],
            "quantity": None if row["quantity"] is None else str(row["quantity"]),
            "unit": row["unit"],
            "analogs_allowed": row["analogs_allowed"],
        }
        for row in rows
    ]
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_request_hash(request: ParsedProcurementRequest) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_manifest_sha256(source_artifacts: Sequence[Mapping[str, str]]) -> str:
    normalized = []
    for artifact in source_artifacts:
        if set(artifact) != {"reference", "sha256"}:
            raise ValueError("source artifact must contain exactly reference and sha256")
        reference = artifact["reference"]
        digest = artifact["sha256"]
        if not reference or "/" in reference or "\\" in reference:
            raise ValueError("source reference must be a stable artifact name, not a path")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("source artifact SHA-256 must be lowercase hexadecimal")
        normalized.append({"reference": reference, "sha256": digest})
    if not normalized or len({a["reference"] for a in normalized}) != len(normalized):
        raise ValueError("source artifact manifest must be non-empty with unique references")
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_requirement_projection_hash(
    request: ParsedProcurementRequest,
    item_rows: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    projection = []
    for item, row in zip(request.items, item_rows, strict=True):
        for requirement in item.requirements:
            if not requirement.source_reference:
                raise CaseImportConflict("every hard specification needs a source reference")
            projection.append(
                {
                    "request_item_id": str(row["id"]),
                    "requirement_key": requirement.name,
                    "operator": "equals",
                    "expected_value": {
                        "value": requirement.expected_value,
                        "unit": requirement.unit,
                    },
                    "is_hard": requirement.hard_requirement,
                    "source_pointer": requirement.source_reference,
                }
            )
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), projection


def validate_operator_request(
    request: ParsedProcurementRequest,
    *,
    expected_request_sha256: str,
    required_city: str = "Тараз",
) -> str:
    """Check explicit geographic/search facts and keep unknown finances unknown."""

    if request.city != required_city:
        raise CaseImportConflict(f"request city must be exactly {required_city!r}")
    if request.search_scope is None or request.search_scope.value != "citywide":
        raise CaseImportConflict("request must explicitly use citywide search scope")
    if not request.items:
        raise CaseImportConflict("request contains no items")
    for line, item in enumerate(request.items, start=1):
        if not item.source_reference:
            raise CaseImportConflict(f"line {line} has no source reference")
        if not item.name or item.quantity is None:
            raise CaseImportConflict(f"line {line} lacks a searchable name or quantity")
        if not item.unit and not item.requirements:
            raise CaseImportConflict(f"line {line} lacks a unit and searchable specification")
        _operator_source_specification(item)
        if item.tender_unit_price_private is not None:
            raise CaseImportConflict(
                f"line {line} has a private price without an explicitly reviewed currency"
            )
    digest = canonical_request_hash(request)
    if digest != expected_request_sha256:
        raise CaseImportConflict("normalized request hash differs from reviewed hash")
    return digest


def validate_item_projection(
    request: ParsedProcurementRequest,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Require reviewed rows to match the extant item IDs and requirement facts."""

    if len(rows) != len(request.items):
        raise CaseImportConflict(
            f"item count differs: reviewed={len(request.items)} existing={len(rows)}"
        )
    for line, (item, row) in enumerate(zip(request.items, rows, strict=True), start=1):
        raw_spec = _operator_source_specification(item)
        comparisons = {
            "line_number": (row["line_number"], line),
            "name": (row["name"], item.name),
            "specification_text": (row["specification_text"], raw_spec),
            "quantity": (row["quantity"], item.quantity),
            "unit": (row["unit"], item.unit or ""),
        }
        for field, (existing, reviewed) in comparisons.items():
            if field == "quantity" and existing is not None and reviewed is not None:
                if str(existing) == str(reviewed):
                    continue
            if existing != reviewed:
                raise CaseImportConflict(
                    f"line {line} {field} differs from the existing case projection"
                )
        if not row["id"]:
            raise CaseImportConflict(f"line {line} has no stable item identity")


def _operator_source_specification(item: Any) -> str:
    """Bind the complete raw DB spec to one exact, source-backed hard fact."""

    if len(item.requirements) != 1:
        raise CaseImportConflict(
            "each operator-import line must contain one exact source specification field"
        )
    requirement = item.requirements[0]
    if (
        requirement.name != "Техническая характеристика"
        or requirement.expected_value is None
        or requirement.unit is not None
        or not requirement.hard_requirement
    ):
        raise CaseImportConflict(
            "operator-import specification must be one hard 'Техническая характеристика' field"
        )
    return requirement.expected_value


def validate_requirement_projection(
    request: ParsedProcurementRequest,
    rows: Sequence[Mapping[str, Any]],
    item_rows: Sequence[Mapping[str, Any]],
) -> None:
    if len(item_rows) != len(request.items):
        raise CaseImportConflict("requirement parent rows differ from reviewed request")
    expected: list[tuple[Any, str, str, Any, bool, str]] = []
    for item, item_row in zip(request.items, item_rows, strict=True):
        expected.extend(
            (
                item_row["id"],
                requirement.name,
                "equals",
                {"value": requirement.expected_value, "unit": requirement.unit},
                requirement.hard_requirement,
                requirement.source_reference or "",
            )
            for requirement in item.requirements
        )
    actual = [
        (
            row["request_item_id"],
            row["requirement_key"],
            row["operator"],
            row["expected_value"],
            row["is_hard"],
            row["source_pointer"],
        )
        for row in rows
    ]
    if sorted(expected, key=repr) != sorted(actual, key=repr):
        raise CaseImportConflict("existing requirement facts differ from reviewed request")


async def preflight_operator_import(
    pool: asyncpg.Pool,
    *,
    case_id: UUID,
    owner_id: UUID,
    request: ParsedProcurementRequest,
    expected_request_sha256: str,
    expected_projection_sha256: str,
    required_city: str = "Тараз",
) -> dict[str, Any]:
    """Read-only validation of owner, manual draft and stable row projections."""

    request_hash = validate_operator_request(
        request,
        expected_request_sha256=expected_request_sha256,
        required_city=required_city,
    )
    if len(expected_projection_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in expected_projection_sha256
    ):
        raise ValueError("expected_projection_sha256 must be a lowercase SHA-256 digest")
    async with pool.acquire() as connection:
        case = await connection.fetchrow(
            """SELECT c.id,c.owner_user_id,c.source_kind,c.status,c.title,c.city,u.active
               FROM procurement_cases c JOIN users u ON u.id=c.owner_user_id
               WHERE c.id=$1""",
            case_id,
        )
        if case is None or case["owner_user_id"] != owner_id or not case["active"]:
            raise CaseImportConflict("case owner is missing, inactive, or differs")
        if case["source_kind"] != "manual":
            raise CaseImportConflict("only a manual-source case can be imported")
        if case["status"] != "draft":
            raise CaseImportConflict("only a draft manual-source case can be preflighted")
        items = await connection.fetch(
            """SELECT id,line_number,name,specification_text,quantity,unit,status,analogs_allowed
               FROM request_items WHERE case_id=$1 ORDER BY line_number""",
            case_id,
        )
        validate_item_projection(request, items)
        projection_hash = item_projection_sha256(items)
        if projection_hash != expected_projection_sha256:
            raise CaseImportConflict("existing item projection hash differs from reviewed snapshot")
        if any(row["status"] != "draft" for row in items):
            raise CaseImportConflict("all existing item projections must still be drafts")
        requirements = await connection.fetch(
            """SELECT ir.request_item_id,ir.requirement_key,ir.operator,ir.expected_value,
                      ir.is_hard,ir.source_pointer
               FROM item_requirements ir JOIN request_items ri ON ri.id=ir.request_item_id
               WHERE ri.case_id=$1 ORDER BY ri.line_number,ir.requirement_key,ir.source_pointer""",
            case_id,
        )
        if requirements:
            validate_requirement_projection(request, requirements, items)
        requirement_hash, requirement_projection = canonical_requirement_projection_hash(
            request, items
        )
    return {
        "case_id": str(case_id),
        "owner_id": str(owner_id),
        "source_kind": case["source_kind"],
        "case_status": case["status"],
        "current_city": case["city"],
        "item_count": len(items),
        "item_ids": [str(row["id"]) for row in items],
        "request_sha256": request_hash,
        "projection_sha256": projection_hash,
        "requirement_sha256": requirement_hash,
        "requirement_count": len(requirement_projection),
        "requirements_to_add": 0 if requirements else len(requirement_projection),
        "ready_for_import": True,
        "writes": False,
    }


async def apply_operator_import(
    pool: asyncpg.Pool,
    *,
    case_id: UUID,
    owner_id: UUID,
    request: ParsedProcurementRequest,
    expected_request_sha256: str,
    expected_projection_sha256: str,
    source_sha256: str,
    source_artifacts: Sequence[Mapping[str, str]],
    source_reference: str,
    required_city: str = "Тараз",
) -> dict[str, Any]:
    """Atomically attach an explicit document import without replacing item rows."""

    if len(source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    if not source_reference.strip():
        raise ValueError("source_reference is required")
    if source_manifest_sha256(source_artifacts) != source_sha256:
        raise CaseImportConflict("source manifest hash differs from artifact list")
    request_hash = validate_operator_request(
        request,
        expected_request_sha256=expected_request_sha256,
        required_city=required_city,
    )
    if len(expected_projection_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in expected_projection_sha256
    ):
        raise ValueError("expected_projection_sha256 must be a lowercase SHA-256 digest")
    payload = request.model_dump(mode="json")
    event_key = stable_key("operator.document-import", case_id, source_sha256, request_hash)

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"operator-case-import:{case_id}",
        )
        case = await connection.fetchrow(
            """SELECT c.id,c.owner_user_id,c.source_kind,c.status,c.title,c.city,
                      u.active
               FROM procurement_cases c JOIN users u ON u.id=c.owner_user_id
               WHERE c.id=$1 FOR UPDATE OF c""",
            case_id,
        )
        if case is None or case["owner_user_id"] != owner_id or not case["active"]:
            raise CaseImportConflict("case owner is missing, inactive, or differs")
        if case["source_kind"] != "manual":
            raise CaseImportConflict("only a manual-source case can be imported")

        prior = await connection.fetchrow(
            """SELECT id,version,normalized_payload FROM case_parse_versions
               WHERE case_id=$1 AND parser_name='operator_verified_document_import'
                 AND status='applied' FOR UPDATE""",
            case_id,
        )
        if prior is not None:
            prior_request = ParsedProcurementRequest.model_validate(prior["normalized_payload"])
            if canonical_request_hash(prior_request) != request_hash:
                raise CaseImportConflict("a different operator import is already applied")
            event = await connection.fetchrow(
                "SELECT data FROM workflow_events WHERE idempotency_key=$1", event_key
            )
            current_items = await connection.fetch(
                """SELECT id,line_number,name,specification_text,quantity,unit,status,
                          analogs_allowed FROM request_items
                   WHERE case_id=$1 ORDER BY line_number""",
                case_id,
            )
            current_requirements = await connection.fetch(
                """SELECT ir.request_item_id,ir.requirement_key,ir.operator,
                          ir.expected_value,ir.is_hard,ir.source_pointer
                   FROM item_requirements ir JOIN request_items ri ON ri.id=ir.request_item_id
                   WHERE ri.case_id=$1
                   ORDER BY ri.line_number,ir.requirement_key,ir.source_pointer""",
                case_id,
            )
            expected_item_ids = [str(row["id"]) for row in current_items]
            if event is None:
                raise CaseImportConflict("applied import has no matching provenance event")
            event_item_ids = event["data"].get("item_ids")
            if event_item_ids != expected_item_ids or event["data"].get(
                "item_content_sha256"
            ) != item_content_sha256(current_items):
                raise CaseImportConflict("applied import item content or identity has drifted")
            try:
                validate_item_projection(request, current_items)
            except CaseImportConflict as exc:
                raise CaseImportConflict(
                    "applied import item content or identity has drifted"
                ) from exc
            validate_requirement_projection(request, current_requirements, current_items)
            if (
                event["data"].get("source_sha256") != source_sha256
                or event["data"].get("source_artifacts") != list(source_artifacts)
                or event["data"].get("request_sha256") != request_hash
                or event["data"].get("projection_sha256") != expected_projection_sha256
                or event["data"].get("requirement_sha256")
                != canonical_requirement_projection_hash(request, current_items)[0]
            ):
                raise CaseImportConflict("applied import has no matching provenance event")
            return {
                "case_id": str(case_id),
                "parse_version_id": str(prior["id"]),
                "version": prior["version"],
                "request_sha256": request_hash,
                "source_sha256": source_sha256,
                "idempotent": True,
            }

        if case["status"] != "draft":
            raise CaseImportConflict("only an unimported manual-source draft can be changed")

        rows = await connection.fetch(
            """SELECT id,line_number,name,specification_text,quantity,unit,status,analogs_allowed
               FROM request_items WHERE case_id=$1 ORDER BY line_number FOR UPDATE""",
            case_id,
        )
        validate_item_projection(request, rows)
        projection_hash = item_projection_sha256(rows)
        if projection_hash != expected_projection_sha256:
            raise CaseImportConflict("existing item projection hash differs from reviewed snapshot")
        if any(row["status"] != "draft" for row in rows):
            raise CaseImportConflict("all existing item projections must still be drafts")
        requirement_rows = await connection.fetch(
            """SELECT ir.request_item_id,ir.requirement_key,ir.operator,ir.expected_value,
                      ir.is_hard,ir.source_pointer
               FROM item_requirements ir JOIN request_items ri ON ri.id=ir.request_item_id
               WHERE ri.case_id=$1 ORDER BY ri.line_number,ir.requirement_key,ir.source_pointer""",
            case_id,
        )
        # Existing requirement IDs are immutable evidence; neither delete nor rewrite them.
        if requirement_rows:
            validate_requirement_projection(request, requirement_rows, rows)
        requirement_hash, requirement_projection = canonical_requirement_projection_hash(
            request, rows
        )
        inserted_requirement_ids: list[str] = []
        if not requirement_rows:
            for requirement in requirement_projection:
                requirement_id = await connection.fetchval(
                    """INSERT INTO item_requirements(
                         request_item_id,requirement_key,expected_value,is_hard,source_pointer
                       ) VALUES($1,$2,$3::jsonb,$4,$5) RETURNING id""",
                    UUID(requirement["request_item_id"]),
                    requirement["requirement_key"],
                    requirement["expected_value"],
                    requirement["is_hard"],
                    requirement["source_pointer"],
                )
                inserted_requirement_ids.append(str(requirement_id))

        current_version = await connection.fetchval(
            "SELECT COALESCE(max(version),0) FROM case_parse_versions WHERE case_id=$1",
            case_id,
        )
        version = int(current_version) + 1
        await connection.execute(
            "UPDATE case_parse_versions SET status='superseded' "
            "WHERE case_id=$1 AND status='applied'",
            case_id,
        )
        parse_version_id = await connection.fetchval(
            """INSERT INTO case_parse_versions(
                 case_id,source_message_id,source_extraction_id,version,
                 parser_name,parser_version,normalized_payload,status,applied_at
               ) VALUES($1,NULL,NULL,$2,'operator_verified_document_import','1',
                        $3::jsonb,'applied',now()) RETURNING id""",
            case_id,
            version,
            payload,
        )
        await connection.execute(
            """UPDATE procurement_cases SET city=$2,search_area_text=$3,
                      status='ready',updated_at=now() WHERE id=$1""",
            case_id,
            request.city,
            request.search_area_text or "",
        )
        await connection.execute(
            "UPDATE request_items SET status='ready',updated_at=now() "
            "WHERE case_id=$1 AND status='draft'",
            case_id,
        )
        await connection.execute(
            """INSERT INTO workflow_events(
                 case_id,event_type,actor_type,actor_id,idempotency_key,data
               ) VALUES($1,'operator_document_import_applied','agent',$2,$3,$4::jsonb)""",
            case_id,
            str(owner_id),
            event_key,
            {
                "parse_version_id": str(parse_version_id),
                "version": version,
                "parser_name": "operator_verified_document_import",
                "source_reference": source_reference,
                "source_sha256": source_sha256,
                "source_artifacts": list(source_artifacts),
                "request_sha256": request_hash,
                "projection_sha256": projection_hash,
                "item_content_sha256": item_content_sha256(rows),
                "requirement_sha256": requirement_hash,
                "requirement_count": len(requirement_projection),
                "inserted_requirement_ids": inserted_requirement_ids,
                "item_ids": [str(row["id"]) for row in rows],
                "item_source_references": [item.source_reference for item in request.items],
            },
        )
    return {
        "case_id": str(case_id),
        "parse_version_id": str(parse_version_id),
        "version": version,
        "request_sha256": request_hash,
        "source_sha256": source_sha256,
        "idempotent": False,
    }
