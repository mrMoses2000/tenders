from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_node_scenario(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the isolated Mini App helper test")

    source = (Path(__file__).parents[1] / "mini_app" / "app.js").read_text()
    helper = re.search(
        r"async function loadCaseDetail\(id, signal\) \{.*?\n\}", source, re.DOTALL
    )
    assert helper is not None
    script = helper.group(0) + "\n" + scenario
    result = subprocess.run(  # noqa: S603 - executes the extracted local helper only
        [node, "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_case_detail_paginates_sequentially_deduplicates_and_reuses_abort_signal() -> None:
    result = _run_node_scenario(
        """
const calls = [];
const signal = {name: "same-signal"};
const pages = new Map([
  ["/api/cases/case-1", {offers_offset: 0, offers_total: 4,
    offers_next_offset: 2, offers_truncated: true,
    offers: [{id: "a", version: 1}, {id: "b", version: 1}], items: [{id: "item"}]}],
  ["/api/cases/case-1?offers_offset=2", {offers_offset: 2, offers_total: 4,
    offers_next_offset: 3, offers_truncated: true,
    offers: [{id: "b", version: 2}, {id: "c", version: 1}]}],
  ["/api/cases/case-1?offers_offset=3", {offers_offset: 3, offers_total: 4,
    offers_next_offset: null, offers_truncated: false,
    offers: [{id: "d", version: 1}]}]
]);
async function api(path, passedSignal) {
  calls.push({path, sameSignal: passedSignal === signal});
  return pages.get(path);
}
(async () => {
  const data = await loadCaseDetail("case-1", signal);
  console.log(JSON.stringify({calls, ids: data.offers.map(offer => offer.id),
    versions: data.offers.map(offer => offer.version),
    truncated: data.offers_truncated, items: data.items}));
})();
"""
    )

    assert [call["path"] for call in result["calls"]] == [
        "/api/cases/case-1",
        "/api/cases/case-1?offers_offset=2",
        "/api/cases/case-1?offers_offset=3",
    ]
    assert all(call["sameSignal"] for call in result["calls"])
    assert result["ids"] == ["a", "b", "c", "d"]
    assert result["versions"] == [1, 2, 1, 1]
    assert result["truncated"] is False
    assert result["items"] == [{"id": "item"}]


def test_legacy_and_incomplete_responses_keep_truncation_visible() -> None:
    result = _run_node_scenario(
        """
const calls = [];
async function api(path) {
  calls.push(path);
  if (path === "/api/cases/legacy") return {offers: [{id: "a"}], offers_total: 2,
    offers_truncated: true};
  if (path === "/api/cases/broken") return {offers_offset: 0, offers_total: 3,
    offers_next_offset: 2, offers_truncated: true, offers: [{id: "a"}]};
  if (path === "/api/cases/broken?offers_offset=2") throw new Error("offline");
}
(async () => {
  const legacy = await loadCaseDetail("legacy");
  const legacyCalls = calls.length;
  const broken = await loadCaseDetail("broken");
  console.log(JSON.stringify({legacy: legacy.offers_truncated,
    legacyCalls, broken: broken.offers_truncated, brokenIds: broken.offers.map(x => x.id)}));
})();
"""
    )

    assert result == {
        "legacy": True,
        "legacyCalls": 1,
        "broken": True,
        "brokenIds": ["a"],
    }


def test_repeated_offset_or_page_without_progress_stops_and_marks_partial() -> None:
    result = _run_node_scenario(
        """
const calls = [];
async function api(path) {
  calls.push(path);
  if (path === "/api/cases/repeat") return {offers_offset: 0, offers_total: 3,
    offers_next_offset: 2, offers_truncated: true, offers: [{id: "a"}]};
  return {offers_offset: 2, offers_total: 3, offers_next_offset: 2,
    offers_truncated: true, offers: [{id: "a"}]};
}
(async () => {
  const data = await loadCaseDetail("repeat");
  console.log(JSON.stringify({calls, truncated: data.offers_truncated, count: data.offers.length}));
})();
"""
    )

    assert result["calls"] == ["/api/cases/repeat", "/api/cases/repeat?offers_offset=2"]
    assert result["truncated"] is True
    assert result["count"] == 1


def test_page_walk_has_a_strict_limit_below_one_hundred_requests() -> None:
    result = _run_node_scenario(
        """
let calls = 0;
async function api(path) {
  calls += 1;
  if (path === "/api/cases/long") return {offers_offset: 0, offers_total: 1000,
    offers_next_offset: 1, offers_truncated: true, offers: [{id: "0"}]};
  const offset = Number(new URLSearchParams(path.split("?")[1]).get("offers_offset"));
  return {offers_offset: offset, offers_total: 1000,
    offers_next_offset: offset + 1, offers_truncated: true, offers: [{id: String(offset)}]};
}
(async () => {
  const data = await loadCaseDetail("long");
  console.log(JSON.stringify({calls, truncated: data.offers_truncated}));
})();
"""
    )

    assert result["calls"] == 99
    assert result["truncated"] is True


def test_abort_during_a_later_page_is_propagated() -> None:
    result = _run_node_scenario(
        """
const signal = {aborted: false};
let calls = 0;
async function api(path, passedSignal) {
  calls += 1;
  if (passedSignal !== signal) throw new Error("wrong signal");
  if (calls === 1) return {offers_offset: 0, offers_total: 2,
    offers_next_offset: 1, offers_truncated: true, offers: [{id: "a"}]};
  signal.aborted = true;
  const error = new Error("request aborted");
  error.name = "AbortError";
  throw error;
}
(async () => {
  let propagated = false;
  try { await loadCaseDetail("abort", signal); }
  catch (error) { propagated = error.name === "AbortError"; }
  console.log(JSON.stringify({calls, propagated}));
})();
"""
    )

    assert result == {"calls": 2, "propagated": True}
