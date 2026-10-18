"""Revisit an existing 2GIS firm card to capture public contact details."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from procurement_bot.errors import PermanentProviderError
from procurement_bot.intake import SearchScope
from procurement_bot.providers.browser import BrowserResearchPort, UnsafeBrowserResult
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.research import (
    ContactKind,
    EvidenceClaim,
    LocalityConstraint,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
)
from procurement_bot.suppliers import (
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierIdentityConflictError,
    upsert_source_evidence,
    upsert_supplier_contact,
)

_CITY_PATH = re.compile(r"^https://2gis\.kz/([a-z0-9_-]+)/", re.IGNORECASE)
_FIRM_ID = re.compile(r"^[0-9]{5,30}$")


async def schedule_case_2gis_enrichment(
    connection: asyncpg.Connection,
    case_id: UUID,
    *,
    max_pending: int = 2,
    finishing_supplier_id: UUID | None = None,
) -> int:
    """Keep a small background queue until every contactless firm was checked today."""

    if not 1 <= max_pending <= 4:
        raise ValueError("max_pending must be between 1 and 4")
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
        f"2gis-enrichment:{case_id}",
    )
    active = await connection.fetchval(
        """
        SELECT count(*) FROM jobs
        WHERE kind='enrich_2gis_contact'
          AND payload->>'case_id'=$1
          AND status IN ('pending','running','retry')
          AND ($2::text IS NULL OR payload->>'supplier_id'<>$2)
        """,
        str(case_id),
        str(finishing_supplier_id) if finishing_supplier_id is not None else None,
    )
    slots = max_pending - int(active or 0)
    if slots <= 0:
        return 0
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (o.supplier_id)
               o.supplier_id,l.provider_place_id AS firm_id,se.source_url
        FROM offers o
        JOIN request_items i ON i.id=o.request_item_id
        JOIN supplier_locations l ON l.id=o.supplier_location_id
        JOIN source_evidence se ON se.id=l.source_evidence_id
        WHERE i.case_id=$1 AND l.source_provider='2gis'
          AND NOT EXISTS (
              SELECT 1 FROM supplier_contacts c
              WHERE c.supplier_id=o.supplier_id AND c.active
                AND c.contact_type IN ('phone','whatsapp')
          )
        ORDER BY o.supplier_id,se.captured_at DESC
        LIMIT 1000
        """,
        case_id,
    )
    queued = 0
    today = datetime.now(UTC).date().isoformat()
    for row in rows:
        source_url = row["source_url"] or ""
        if not _CITY_PATH.match(source_url) or not _FIRM_ID.fullmatch(row["firm_id"] or ""):
            continue
        job_id = await enqueue_job(
            connection,
            kind="enrich_2gis_contact",
            payload={"case_id": str(case_id), "supplier_id": str(row["supplier_id"])},
            idempotency_key=stable_key(
                "enrich-2gis-contact", case_id, row["supplier_id"], today,
            ),
            max_attempts=2,
        )
        queued += job_id is not None
        if queued >= slots:
            break
    return queued


async def enrich_2gis_contact(
    pool: asyncpg.Pool,
    browser: BrowserResearchPort,
    *,
    case_id: UUID,
    supplier_id: UUID,
) -> int:
    """Persist only contacts proven by the selected firm's own card."""

    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT s.display_name,l.provider_place_id AS firm_id,l.city,
                   se.source_url,i.id AS request_item_id
            FROM offers o
            JOIN request_items i ON i.id=o.request_item_id
            JOIN suppliers s ON s.id=o.supplier_id
            JOIN supplier_locations l ON l.id=o.supplier_location_id
            JOIN source_evidence se ON se.id=l.source_evidence_id
            WHERE i.case_id=$1 AND o.supplier_id=$2 AND l.source_provider='2gis'
            ORDER BY se.captured_at DESC LIMIT 1
            """,
            case_id, supplier_id,
        )
        already_known = await connection.fetchval(
            """SELECT EXISTS (
                SELECT 1 FROM supplier_contacts WHERE supplier_id=$1 AND active
                  AND contact_type IN ('phone','whatsapp')
            )""",
            supplier_id,
        )
    if row is None:
        raise PermanentProviderError("supplier has no 2GIS card in this case")
    if already_known:
        return 0
    match = _CITY_PATH.match(row["source_url"] or "")
    firm_id = row["firm_id"] or ""
    if match is None or _FIRM_ID.fullmatch(firm_id) is None:
        raise PermanentProviderError("supplier's 2GIS source cannot identify its firm card")
    card_url = f"https://2gis.kz/{match.group(1)}/firm/{firm_id}"
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=[row["display_name"]],
        rendered_query=card_url,
        locality=LocalityConstraint(city=row["city"] or "Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=24,
        empty_page_limit=1,
    )
    try:
        result = await browser.search_suppliers(plan)
    except UnsafeBrowserResult as exc:
        if exc.access_issue is None:
            raise
        result = None
        access_issue = exc.access_issue
    else:
        access_issue = result.access_issue
    if result is not None and any(
        candidate.external_id != firm_id for candidate in result.candidates
    ):
        raise PermanentProviderError("2GIS card visit returned a different firm")
    candidate = result.candidates[0] if result is not None and result.candidates else None
    contact_count = 0
    phone_count = 0
    invalid_count = 0
    conflict_count = 0
    contact_evidence_ids: set[str] = set()
    async with pool.acquire() as connection, connection.transaction():
        if candidate is not None:
            contact_evidence_id = None
            for evidence in candidate.evidence:
                if not evidence.source_url.startswith(card_url):
                    raise PermanentProviderError("2GIS enrichment evidence is not the target card")
                raw = evidence.model_dump(mode="json")
                encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                stored = await upsert_source_evidence(
                    connection,
                    SourceEvidenceInput(
                        source_type="2gis",
                        source_locator=evidence.source_url,
                        source_url=evidence.source_url,
                        external_id=firm_id,
                        title=candidate.name,
                        captured_at=evidence.observed_at,
                        content_sha256=digest,
                        raw_payload=raw,
                        idempotency_key=stable_key(
                            "2gis-contact-evidence", supplier_id, digest,
                        ),
                    ),
                )
                if EvidenceClaim.CONTACT in evidence.claims:
                    contact_evidence_id = stored.id
                    contact_evidence_ids.add(str(stored.id))
            if candidate.contacts and contact_evidence_id is None:
                raise PermanentProviderError("2GIS contact has no card evidence")
            if contact_evidence_id is not None:
                for contact in candidate.contacts:
                    try:
                        contact_input = SupplierContactInput(
                            contact_type=contact.kind.value,
                            display_value=contact.value,
                            source_evidence_id=contact_evidence_id,
                            is_whatsapp_confirmed=contact.kind == ContactKind.WHATSAPP,
                            confidence=Decimal(str(contact.confidence)),
                        )
                    except ValueError:
                        invalid_count += 1
                        continue
                    try:
                        await upsert_supplier_contact(connection, supplier_id, contact_input)
                    except SupplierIdentityConflictError:
                        conflict_count += 1
                        continue
                    contact_count += 1
                    phone_count += contact.kind in {ContactKind.PHONE, ContactKind.WHATSAPP}
        outcome = (
            "blocked" if result is None else
            "contact_found" if phone_count else
            "contact_needs_review" if invalid_count or conflict_count else "no_phone"
        )
        # Rechecks can turn a blocked card into a proven contact later on the
        # same day. Keep both dated results rather than hiding success behind
        # the first event's idempotency key.
        result_fingerprint = hashlib.sha256(json.dumps(
            {
                "card_url": card_url,
                "access_issue": access_issue,
                "contacts": [contact.model_dump(mode="json") for contact in candidate.contacts]
                if candidate is not None else [],
                "contact_evidence_ids": sorted(contact_evidence_ids),
                "outcome": outcome,
            },
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        await connection.execute(
            """
            INSERT INTO workflow_events(
                case_id,request_item_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,$2,'supplier_contact_enrichment_result','worker','2gis-card',$3,$4::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            case_id,
            row["request_item_id"],
            stable_key(
                "supplier-contact-enriched", case_id, supplier_id,
                datetime.now(UTC).date().isoformat(), result_fingerprint,
            ),
            {
                "supplier_id": str(supplier_id),
                "firm_id": firm_id,
                "card_url": card_url,
                "contact_count": contact_count,
                "phone_count": phone_count,
                "contact_evidence_ids": sorted(contact_evidence_ids),
                "invalid_contacts": invalid_count,
                "conflicting_contacts": conflict_count,
                "access_issue": access_issue,
                "outcome": outcome,
            },
        )
    return phone_count
