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
    {item_id: "skip", stage: "owner_excluded_from_sourcing", sourcing_evidence: [
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
        "research_review",
        "in_research",
        "supplier_candidates",
        "research_review",
        "not_started",
    ]
    assert result["total"] == 12
    assert sum(count > 0 for count in result["counts"]) == 8
    assert result["waitingConversations"] == 2
    assert result["staleInstruction"] == "purchase_review"
    assert result["accountLabel"] == "Закуплено · учёт"
    assert result["ownerLabel"] == "Исключено владельцем"


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
