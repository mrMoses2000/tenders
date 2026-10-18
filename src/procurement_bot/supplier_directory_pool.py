"""Shared TTL cache for validated 2GIS directory-page descriptors."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from procurement_bot.research import ResearchSource, SourcePlan

_PAGE_FIELDS = frozenset(
    {
        "spec_sha256", "request_sha256", "cards", "page_url", "raw_snapshot_sha256",
        "pages_examined", "next_cursor", "exhausted", "access_issue",
        "end_of_results_observed",
    }
)
_LEGACY_PAGE_FIELDS = _PAGE_FIELDS - {"end_of_results_observed"}
_CARD_FIELDS = frozenset({"external_id", "url", "name"})


def directory_pool_identity(
    plan: SourcePlan, *, search_cursor: str | None = None
) -> tuple[str, str, dict[str, Any], str]:
    """Return the provider/category/locality/cursor identity used by the shared pool."""
    if plan.source != ResearchSource.TWO_GIS:
        raise ValueError("the shared supplier directory pool supports 2GIS only")
    category_query = " | ".join(" ".join(term.casefold().split()) for term in plan.query_terms)
    if not category_query:
        raise ValueError("2GIS directory pool requires a category query")
    locality = plan.locality.model_dump(mode="json")
    cursor = search_cursor if search_cursor is not None else (plan.search_cursor or "")
    identity = {
        "source": ResearchSource.TWO_GIS.value,
        "category_query": category_query,
        "locality": locality,
        "search_cursor": cursor,
    }
    if plan.execution_scope_revision_sha256 is not None:
        # A priority-area result page must not satisfy a later citywide query
        # merely because the business category is the same.
        identity["scope_revision_sha256"] = plan.execution_scope_revision_sha256
        identity["rendered_query"] = " ".join(plan.rendered_query.casefold().split())
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), category_query, locality, cursor


def _validated_page_descriptor(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload_fields = set(payload)
    if frozenset(payload_fields) not in {_PAGE_FIELDS, _LEGACY_PAGE_FIELDS}:
        raise ValueError("directory pool accepts only DiscoveryPage fields")
    cards = payload.get("cards")
    if not isinstance(cards, list) or any(
        not isinstance(card, Mapping) or set(card) != _CARD_FIELDS for card in cards
    ):
        raise ValueError("directory pool cards must contain only IDs, URLs, and names")
    if payload.get("access_issue") is not None:
        raise ValueError("access failures are not reusable directory results")
    if not isinstance(payload.get("pages_examined"), int) or payload["pages_examined"] < 1:
        raise ValueError("directory pool page must record an examined page")
    end_observed = payload.get("end_of_results_observed", False)
    if not isinstance(end_observed, bool):
        raise ValueError("end_of_results_observed must be a boolean")
    exhausted = payload.get("exhausted")
    if not isinstance(exhausted, bool):
        raise ValueError("exhausted must be a boolean")
    for digest_key in ("spec_sha256", "request_sha256", "raw_snapshot_sha256"):
        digest = payload.get(digest_key)
        if digest is not None and not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
            raise ValueError(f"{digest_key} must be a SHA-256 digest")
    normalized = dict(payload)
    normalized["end_of_results_observed"] = end_observed
    # Old cached pages have no explicit end proof. Keep the cards reusable but
    # never let an old optimistic exhaustion flag stop a newer search early.
    if not end_observed or len(cards) == 20 or payload.get("access_issue") is not None:
        normalized["exhausted"] = False
    return json.loads(json.dumps(normalized, ensure_ascii=False, allow_nan=False))


async def load_directory_page(
    connection: asyncpg.Connection,
    plan: SourcePlan,
    *,
    search_cursor: str | None = None,
) -> dict[str, Any] | None:
    cache_key, _category_query, _locality, _cursor = directory_pool_identity(
        plan, search_cursor=search_cursor
    )
    row = await connection.fetchrow(
        """
        SELECT payload FROM supplier_directory_page_pool
        WHERE cache_key=$1 AND expires_at>now()
          AND created_at + make_interval(hours => $2::integer) > now()
        """,
        cache_key,
        plan.result_ttl_hours,
    )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _validated_page_descriptor(payload)


async def store_directory_page(
    connection: asyncpg.Connection,
    plan: SourcePlan,
    payload: Mapping[str, Any],
    *,
    search_cursor: str | None = None,
) -> None:
    descriptor = _validated_page_descriptor(payload)
    cache_key, category_query, locality, cursor = directory_pool_identity(
        plan, search_cursor=search_cursor
    )
    await connection.execute(
        """
        INSERT INTO supplier_directory_page_pool(
            cache_key,source,category_query,locality,search_cursor,result_ttl_hours,
            payload,created_at,expires_at
        ) VALUES ($1,'2gis',$2,$3::jsonb,$4,$5::integer::smallint,$6::jsonb,now(),
                  now()+make_interval(hours => $5::integer))
        ON CONFLICT (cache_key) DO UPDATE SET
            category_query=EXCLUDED.category_query,
            locality=EXCLUDED.locality,
            search_cursor=EXCLUDED.search_cursor,
            result_ttl_hours=EXCLUDED.result_ttl_hours,
            payload=EXCLUDED.payload,
            created_at=now(),
            expires_at=now()+make_interval(hours => EXCLUDED.result_ttl_hours::integer)
        """,
        cache_key,
        category_query,
        locality,
        cursor,
        plan.result_ttl_hours,
        descriptor,
    )
