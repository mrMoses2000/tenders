from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_stage_scenario(scenario: str) -> dict:
    node = shutil.which("node") or "/home/moses/.hermes/node/bin/node"
    if not Path(node).exists():
        pytest.skip("Node.js is required for Mini App stage helper tests")

    source = (Path(__file__).parents[1] / "mini_app" / "app.js").read_text()
    pieces = [
        re.search(r"const caseStageGroups = \[.*?\];", source, re.DOTALL),
        *[
            re.search(
                rf"function {name}\(.*?\n\}}",
                source,
                re.DOTALL,
            )
            for name in (
                "effectiveOwnerSourcingInstruction",
                "isFullOwnerSourcingInstruction",
                "badge",
                "itemBadge",
                "ownerInstructionBadgeLabel",
                "classifyCaseItemStage",
                "caseStageBadgeLabel",
                "renderCaseStageSummary",
                "groupCaseItems",
            )
        ],
    ]
    assert all(piece is not None for piece in pieces)
    helpers = "\n".join(piece.group(0) for piece in pieces if piece is not None)
    script = """
const labels = {needs_clarification: "Нужно уточнить", ready: "Готова к поиску"};
class FakeNode {
  constructor(tag, className, text) {
    this.tag = tag;
    this.className = className;
    this.textContent = text || "";
    this.children = [];
    this.attributes = {};
    this.handlers = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  click() { this.handlers.click?.(); }
}
function el(tag, className, value) { return new FakeNode(tag, className, value); }
function countLabel(count, one, few, many) {
  const value = Number(count);
  const mod10 = value % 10;
  const mod100 = value % 100;
  return value + " " + (mod10 === 1 && mod100 !== 11 ? one
    : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? few : many);
}
""" + helpers + "\n" + scenario
    result = subprocess.run(  # noqa: S603 - isolated local source helpers only
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    return json.loads(result.stdout)


def test_primary_groups_are_exclusive_and_use_only_current_source_instruction() -> None:
    result = _run_stage_scenario(
        """
const hash = "a".repeat(64);
const items = [
  {id: "accounted", status: "ready"},
  {id: "skip", status: "ready"},
  {id: "evidence", status: "ready"},
  {id: "clarify", status: "needs_clarification"},
  {id: "compare", status: "ready"},
  {id: "candidate", status: "ready"},
  {id: "running", status: "ready"},
  {id: "paused", status: "ready"},
  {id: "failed", status: "ready"},
  {id: "item_running", status: "researching"},
  {id: "offers_status", status: "offers_found"},
  {id: "no_match", status: "no_match"},
  {id: "untouched", status: "ready"}
];
const source = {
  source_payload_sha256: hash,
  items: [
    {item_id: "accounted", stage: "bought", sourcing_evidence: []},
    {item_id: "skip", stage: "owner_excluded_from_sourcing",
      disposition_applied_to_plan: false, sourcing_evidence: [
        {applies_to_sourcing: true, disposition: "skip", source_payload_sha256: hash}]},
    {item_id: "evidence", stage: "evidence_only", sourcing_evidence: [
      {applies_to_sourcing: false, disposition: "bought", source_payload_sha256: null}]},
    {item_id: "clarify", stage: "needs_review", sourcing_evidence: []},
    {item_id: "stale", stage: "owner_excluded_from_sourcing", sourcing_evidence: [
      {applies_to_sourcing: true, disposition: "skip", source_payload_sha256: "b".repeat(64)}]}
  ]
};
const progress = [
  {request_item_id: "running", status: "researching"},
  {request_item_id: "paused", status: "paused"},
  {request_item_id: "failed", status: "failed"}
];
const offers = [
  {request_item_id: "compare", comparison: {rank: 1, price_state: "supplier_confirmed"}},
  {request_item_id: "candidate", price_amount: 500,
    comparison: {rank: null, price_state: "public_price"}}
];
const grouped = groupCaseItems(items, source, progress, offers, 2);
console.log(JSON.stringify({
  assignments: items.map(item => grouped.assignments.get(item.id)),
  counts: grouped.groups.map(group => group.items.length),
  total: grouped.groups.reduce((sum, group) => sum + group.items.length, 0),
  waitingConversations: grouped.waitingConversations,
  staleInstruction: classifyCaseItemStage(items[1],
    source.items.find(row => row.item_id === "stale"), null, [], hash),
  accountLabel: caseStageBadgeLabel("owner_sourcing",
    source.items.find(row => row.item_id === "accounted"), hash),
  ownerLabel: caseStageBadgeLabel("owner_sourcing",
    source.items.find(row => row.item_id === "skip"), hash)
}));
"""
    )
    assert result["assignments"] == [
        "owner_sourcing",
        "owner_sourcing",
        "purchase_review",
        "needs_clarification",
        "comparable_options",
        "supplier_candidates",
        "in_research",
        "paused",
        "research_review",
        "in_research",
        "supplier_candidates",
        "research_review",
        "not_started",
    ]
    assert result["total"] == 13
    assert sum(count > 0 for count in result["counts"]) == 9
    assert result["waitingConversations"] == 2
    assert result["staleInstruction"] == "purchase_review"
    assert result["accountLabel"] == "Закуплено · учёт"
    assert result["ownerLabel"] == "Исключение в следующем плане"


def test_summary_dom_keeps_reply_count_case_level_and_filters_primary_group() -> None:
    result = _run_stage_scenario(
        """
const grouped = {
  groups: [
    {key: "owner_sourcing", label: "Уже закуплено / исключено", items: [{id: "a"}]},
    {key: "purchase_review", label: "Покупка требует сверки", items: []},
    {key: "not_started", label: "Поиск не запущен", items: [{id: "b"}, {id: "c"}]}
  ],
  waitingConversations: 3
};
let selected = null;
const summary = renderCaseStageSummary(grouped, 3, key => { selected = key; });
const filters = summary.element.children[0].children;
filters[2].click();
console.log(JSON.stringify({
  filterLabels: filters.map(button => button.textContent),
  pressed: filters.map(button => button.attributes["aria-pressed"]),
  selected,
  waiting: summary.element.children[1].textContent
}));
"""
    )
    assert result["filterLabels"] == [
        "Все позиции · 3",
        "Уже закуплено / исключено · 1",
        "Поиск не запущен · 2",
    ]
    assert result["selected"] == "not_started"
    assert "3 диалога" in result["waiting"]
    assert "не позиций" in result["waiting"]


def test_partial_owner_quantities_preserve_actual_stage_and_full_quantity_excludes() -> None:
    result = _run_stage_scenario(
        """
const hash = "a".repeat(64);
const makeSource = (disposition, quantity, applies = true) => ({
  stage: "evidence_only",
  disposition_applied_to_plan: false,
  sourcing_evidence: [{applies_to_sourcing: applies, disposition, quantity, unit: "шт",
    source_payload_sha256: applies ? hash : null}]
});
const researching = {id: "partial_bought", quantity: 10, unit: "шт", status: "ready"};
const comparable = {id: "partial_reduce_quote", quantity: 10, unit: "шт", status: "ready"};
const untouched = {id: "partial_reduce_none", quantity: 10, unit: "шт", status: "ready"};
const full = {id: "full_reduce", quantity: 10, unit: "шт", status: "ready"};
const items = [researching, comparable, untouched, full];
const source = {source_payload_sha256: hash, items: [
  {item_id: researching.id, ...makeSource("bought", 3)},
  {item_id: comparable.id, ...makeSource("reduce_quantity", 3)},
  {item_id: untouched.id, ...makeSource("reduce_quantity", 3)},
  {item_id: full.id, ...makeSource("reduce_quantity", 10)}
]};
const progress = [
  {request_item_id: researching.id, status: "researching"},
  {request_item_id: comparable.id, status: "succeeded"}
];
const offers = [{request_item_id: comparable.id,
  comparison: {rank: 1, price_state: "supplier_confirmed"}}];
const grouped = groupCaseItems(items, source, progress, offers, 0);
console.log(JSON.stringify({
  assignments: items.map(item => grouped.assignments.get(item.id)),
  partialLabel: ownerInstructionBadgeLabel(researching,
    source.items.find(row => row.item_id === researching.id), hash),
  fullLabel: caseStageBadgeLabel("owner_sourcing",
    source.items.find(row => row.item_id === full.id), hash),
  unitMismatchFull: isFullOwnerSourcingInstruction(full,
    {disposition: "bought", quantity: 10, unit: "кг"}),
  noCurrentBinding: ownerInstructionBadgeLabel(untouched,
    {...source.items.find(row => row.item_id === untouched.id),
      disposition_applied_to_plan: false}, hash)
}));
"""
    )
    assert result["assignments"] == [
        "in_research",
        "comparable_options",
        "not_started",
        "owner_sourcing",
    ]
    assert "Владелец сообщает о покупке 3 шт" in result["partialLabel"]
    assert "учтётся в следующем плане" in result["partialLabel"]
    assert result["fullLabel"] == "Количество изменено владельцем"
    assert result["unitMismatchFull"] is False
    assert result["noCurrentBinding"] is not None


def test_paused_research_has_distinct_group_from_active_research() -> None:
    result = _run_stage_scenario(
        """
const hash = "a".repeat(64);
const items = [{id: "active", status: "ready"}, {id: "paused", status: "ready"}];
const grouped = groupCaseItems(items, {source_payload_sha256: hash, items: []}, [
  {request_item_id: "active", status: "researching"},
  {request_item_id: "paused", status: "paused"}
], [], 0);
console.log(JSON.stringify(items.map(item => grouped.assignments.get(item.id))));
"""
    )
    assert result == ["in_research", "paused"]


def test_current_source_stage_overrides_stale_flat_clarification_status() -> None:
    result = _run_stage_scenario(
        """
const hash = "a".repeat(64);
const items = [
  {id: "ready", status: "needs_clarification"},
  {id: "comparable", status: "needs_clarification"}
];
const source = {source_payload_sha256: hash, items: [
  {item_id: "ready", stage: "ready_for_search", sourcing_evidence: []},
  {item_id: "comparable", stage: "comparable_quote", sourcing_evidence: []}
]};
const offers = [{request_item_id: "comparable",
  comparison: {rank: 1, price_state: "supplier_confirmed"}}];
const grouped = groupCaseItems(items, source, [], offers, 0);
console.log(JSON.stringify(items.map(item => grouped.assignments.get(item.id))));
"""
    )
    assert result == ["not_started", "comparable_options"]


def test_current_stage_hides_stale_generic_badge_but_keeps_accounting_and_notion() -> None:
    result = _run_stage_scenario(
        """
const item = {id: "item", status: "needs_clarification", quantity: 10, unit: "шт",
  event_count: 0, purchased_quantity: null, accepted_quantity: null};
const sourceItem = {stage: "comparable_quote"};
const purchased = {...item, event_count: 1, purchased_quantity: 10,
  accepted_quantity: 10, delivered_quantity: 10};
const notion = {...item, source_record_url: "https://example.org/item"};
console.log(JSON.stringify({
  staleBadge: itemBadge(item, sourceItem)?.textContent ?? null,
  currentStage: classifyCaseItemStage(item, sourceItem, null,
    [{comparison: {rank: 1, price_state: "supplier_confirmed"}}], null),
  purchasedBadge: itemBadge(purchased, sourceItem)?.textContent,
  notionBadge: itemBadge(notion, sourceItem)?.textContent,
  fallbackBadge: itemBadge(item, null)?.textContent
}));
"""
    )
    assert result == {
        "staleBadge": None,
        "currentStage": "comparable_options",
        "purchasedBadge": "Исполнено",
        "notionBadge": "История из Notion",
        "fallbackBadge": "Нужно уточнить",
    }
