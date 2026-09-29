"""SQL predicates for case-owned provenance of globally deduplicated contacts."""

from __future__ import annotations

import re

_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")


def case_owned_supplier_contact_evidence(
    *, contact_alias: str, evidence_alias: str, scope: str = "case",
    include_enrichment: bool = True, case_param: str = "$1",
) -> str:
    """Return an EXISTS predicate proving contact evidence belongs to a case scope.

    ``scope='case'`` uses ``$1`` as a case UUID. ``scope='owner'`` uses ``$2`` as
    an owner UUID. Only trusted, static SQL aliases may be supplied by callers.
    New contact-enrichment events carry exact evidence IDs. Older events are
    accepted only for their exact 2GIS firm-card URL and firm ID when they
    recorded at least one phone.
    """

    if not _ALIAS.fullmatch(contact_alias) or not _ALIAS.fullmatch(evidence_alias):
        raise ValueError("SQL aliases must be static identifiers")
    if scope not in {"case", "owner"}:
        raise ValueError("scope must be 'case' or 'owner'")
    if case_param not in {"$1", "$2", "$3", "$4"}:
        raise ValueError("case_param must be a positional SQL parameter")
    scope_predicate = (
        "scope_case.id=__CASE_PARAM__" if scope == "case"
        else "scope_case.owner_user_id=$2"
    )
    research_run = """
        EXISTS (
          SELECT 1 FROM research_queries rq
          JOIN research_runs rr ON rr.id=rq.research_run_id
          JOIN procurement_cases scope_case ON scope_case.id=rr.case_id
          WHERE rq.id=__EVIDENCE_ALIAS__.research_query_id
            AND __SCOPE_PREDICATE__
        )
        OR EXISTS (
          SELECT 1 FROM offer_observations own_observation
          JOIN offers own_offer ON own_offer.id=own_observation.offer_id
          JOIN request_items own_item ON own_item.id=own_offer.request_item_id
          JOIN procurement_cases scope_case ON scope_case.id=own_item.case_id
          WHERE own_observation.source_evidence_id=__EVIDENCE_ALIAS__.id
            AND own_offer.supplier_id=__CONTACT_ALIAS__.supplier_id
            AND __SCOPE_PREDICATE__
        )
    """
    enrichment = """
        OR EXISTS (
          SELECT 1 FROM workflow_events enrichment
          JOIN procurement_cases scope_case ON scope_case.id=enrichment.case_id
          WHERE enrichment.event_type IN (
              'supplier_contact_enrichment_result','supplier_contact_enriched'
          )
            AND enrichment.data->>'supplier_id'=__CONTACT_ALIAS__.supplier_id::text
            AND __SCOPE_PREDICATE__
            AND (
              (
                enrichment.data ? 'contact_evidence_ids'
                AND enrichment.data->'contact_evidence_ids'
                    @> jsonb_build_array(__EVIDENCE_ALIAS__.id::text)
              )
              OR (
                NOT (enrichment.data ? 'contact_evidence_ids')
                AND (
                  enrichment.data->>'outcome'='contact_found'
                  OR (
                    enrichment.event_type='supplier_contact_enriched'
                    AND enrichment.data->>'phone_count' ~ '^[1-9][0-9]*$'
                  )
                )
                AND enrichment.data->>'firm_id'=__EVIDENCE_ALIAS__.external_id
                AND __EVIDENCE_ALIAS__.source_type='2gis'
                AND (
                  __EVIDENCE_ALIAS__.source_url=enrichment.data->>'card_url'
                  OR __EVIDENCE_ALIAS__.source_url=(enrichment.data->>'card_url') || '/'
                )
              )
            )
        )
    """ if include_enrichment else ""
    predicate = f"({research_run}__ENRICHMENT_CLAUSE__)"
    return (
        predicate.replace("__ENRICHMENT_CLAUSE__", enrichment)
        .replace("__CONTACT_ALIAS__", contact_alias)
        .replace("__EVIDENCE_ALIAS__", evidence_alias)
        .replace("__SCOPE_PREDICATE__", scope_predicate)
        .replace("__CASE_PARAM__", case_param)
    )
