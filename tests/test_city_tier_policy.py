from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest

from procurement_bot.city_tier_policy import (
    CITY_ORDER,
    AdmissionAction,
    AttemptOutcome,
    CityProgress,
    CityTierPlan,
    DiscoveryBudget,
    TierStatus,
    decide_city_tier,
    record_discovery_attempt,
)

U, R, E, B, S = (
    TierStatus.UNSTARTED,
    TierStatus.RUNNING,
    TierStatus.EXHAUSTED,
    TierStatus.BUDGET_COMPLETE,
    TierStatus.SATISFIED,
)
PLAN = CityTierPlan((DiscoveryBudget(requests=2, pages=3),) * 3)


def snapshot(*statuses):
    return tuple(
        CityProgress(
            city,
            status,
            open_discovery_work=1 if status is R else 0,
            pending_replies=0,
            completion_confirmed=status in (E, B, S),
            successful_requests=2 if status is B else 0,
        )
        for city, status in zip(CITY_ORDER, statuses, strict=True)
    )


def decide(rows, **overrides):
    inputs = dict(
        plan=PLAN,
        now=10,
        deadline=20,
        held=False,
        snapshot_complete=True,
        active_discovery_items=0,
        native_concurrency_budget=3,
        item_active_cities=0,
    )
    inputs.update(overrides)
    return decide_city_tier(rows, **inputs)


def with_row(rows, index=0, **changes):
    return (*rows[:index], replace(rows[index], **changes), *rows[index + 1 :])


def test_explicit_fixed_route_and_no_default_plan():
    assert tuple(CITY_ORDER) == ("Taraz", "Shymkent", "Almaty")
    assert decide(snapshot(U, U, U), plan=None).action is AdmissionAction.HOLD
    assert decide_city_tier(snapshot(U, U, U)).action is AdmissionAction.HOLD
    for rows, city in [
        (snapshot(U, U, U), "Taraz"),
        (snapshot(E, U, U), "Shymkent"),
        (snapshot(E, E, U), "Almaty"),
    ]:
        assert decide(rows).city == city
    with pytest.raises(FrozenInstanceError):
        PLAN.budgets = ()


@pytest.mark.parametrize("statuses", tuple(product(TierStatus, repeat=3)))
def test_every_status_combination_has_no_concurrent_city_fanout(statuses):
    result = decide(snapshot(*statuses))
    if result.action is AdmissionAction.ADMIT:
        index = CITY_ORDER.index(result.city)
        assert all(status in (E, B) for status in statuses[:index])
        assert statuses[index:] == (U,) * (3 - index)
    else:
        assert result.city is None
    if statuses.count(R) > 1:
        assert result.action is AdmissionAction.HOLD


@pytest.mark.parametrize("terminal", [E, B])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("replies", [None, 0, 1, 12])
def test_delayed_replies_remain_resumable_but_do_not_block_next_city(terminal, index, replies):
    rows = snapshot(*((E,) * index + (terminal,) + (U,) * (2 - index)))
    rows = with_row(rows, index, pending_replies=replies)
    before = rows
    result = decide(rows)
    assert result.action is AdmissionAction.ADMIT
    assert result.city is CITY_ORDER[index + 1]
    assert rows == before and rows[index].pending_replies == replies


def test_reply_wait_does_not_keep_a_discovery_item_slot_reserved():
    rows = with_row(snapshot(E, U, U), pending_replies=4)
    assert decide(rows).city == "Shymkent"
    assert decide(rows, item_active_cities=1).action is AdmissionAction.HOLD


@pytest.mark.parametrize("terminal", [E, B, S])
@pytest.mark.parametrize("work,confirmed", [(0, False), (1, True), (None, True)])
def test_terminal_requires_explicit_confirmation_and_no_discovery_work(terminal, work, confirmed):
    rows = with_row(
        snapshot(terminal, U, U), open_discovery_work=work, completion_confirmed=confirmed
    )
    assert decide(rows).action is AdmissionAction.HOLD


def test_empty_queue_or_spent_budget_does_not_implicitly_complete_a_running_tier():
    rows = with_row(snapshot(R, U, U), open_discovery_work=0, successful_requests=100)
    assert decide(rows).action is AdmissionAction.WAIT


@pytest.mark.parametrize(
    "budget,requests,pages,allowed",
    [
        (DiscoveryBudget(requests=2), 2, 0, True),
        (DiscoveryBudget(requests=2), 1, 100, False),
        (DiscoveryBudget(pages=3), 1, 3, True),
        (DiscoveryBudget(pages=3), 100, 2, False),
        (DiscoveryBudget(requests=2, pages=3), 1, 3, True),
        (None, 100, 100, False),
    ],
)
def test_only_explicit_successful_city_budget_can_complete_tier(budget, requests, pages, allowed):
    plan = CityTierPlan((budget, None, None))
    rows = with_row(
        snapshot(B, U, U), successful_requests=requests, successful_pages=pages, failed_attempts=100
    )
    assert (decide(rows, plan=plan).action is AdmissionAction.ADMIT) is allowed


@pytest.mark.parametrize(
    "failure", [value for value in AttemptOutcome if value is not AttemptOutcome.SUCCEEDED]
)
def test_failed_attempts_pause_and_do_not_consume_discovery_budget(failure):
    row = snapshot(R, U, U)[0]
    failed = record_discovery_attempt(row, failure, completed_pages=100)
    assert failed.successful_requests == row.successful_requests == 0
    assert failed.successful_pages == row.successful_pages == 0
    assert failed.failed_attempts == 1 and failed.provider_failure is failure
    assert failed.status is TierStatus.PAUSED
    rows = (failed, *snapshot(R, U, U)[1:])
    assert decide(rows).action is AdmissionAction.PAUSE
    assert (
        decide(
            with_row(
                rows,
                status=B,
                completion_confirmed=True,
                open_discovery_work=0,
                successful_requests=100,
            )
        ).action
        is AdmissionAction.PAUSE
    )
    with pytest.raises(ValueError, match="explicitly resumed"):
        record_discovery_attempt(failed, AttemptOutcome.SUCCEEDED)


def test_success_counts_once_without_automatically_ending_tier_or_mutating_input():
    row = snapshot(R, U, U)[0]
    counted = record_discovery_attempt(row, AttemptOutcome.SUCCEEDED, completed_pages=3)
    assert (counted.successful_requests, counted.successful_pages) == (1, 3)
    assert row.successful_requests == row.successful_pages == 0
    assert counted.status is R and counted.completion_confirmed is False


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.parametrize("terminal", [E, B])
@pytest.mark.parametrize(
    "now,action",
    [(19.999, AdmissionAction.ADMIT), (20, AdmissionAction.PAUSE), (20.001, AdmissionAction.PAUSE)],
)
def test_injected_clock_before_at_and_after_deadline(now, action, terminal):
    clock = FakeClock(now)
    rows = with_row(snapshot(terminal, U, U), pending_replies=2)
    assert decide(rows, now=clock(), deadline=20).action is action
    assert rows[0].status is terminal and rows[0].pending_replies == 2


@pytest.mark.parametrize("now", [19.999, 20, 21])
def test_deadline_alone_never_advances_running_city(now):
    rows = with_row(snapshot(R, U, U), open_discovery_work=0, pending_replies=3)
    result = decide(rows, now=now, deadline=20)
    assert result.action in (AdmissionAction.WAIT, AdmissionAction.PAUSE)
    assert result.city is None and rows[0].status is R


def test_deadline_pause_resumes_same_pending_city_with_explicit_new_window():
    clock = FakeClock(20)
    rows = snapshot(U, U, U)
    assert decide(rows, now=clock(), deadline=20).action is AdmissionAction.PAUSE
    assert decide(rows, now=clock(), deadline=30).city == "Taraz"
    assert decide(rows, now=clock(), deadline=30, held=True).action is AdmissionAction.HOLD


@pytest.mark.parametrize("bad", [None, True, "20", float("nan"), float("inf"), -1])
@pytest.mark.parametrize("field", ["now", "deadline"])
def test_unknown_invalid_time_holds(bad, field):
    assert decide(snapshot(U, U, U), **{field: bad}).action is AdmissionAction.HOLD


@pytest.mark.parametrize(
    "field,value",
    [
        ("held", True),
        ("held", None),
        ("held", 0),
        ("snapshot_complete", False),
        ("snapshot_complete", 1),
        ("active_discovery_items", None),
        ("active_discovery_items", 4),
        ("active_discovery_items", True),
    ],
)
def test_existing_unknown_or_overlapping_guards_hold(field, value):
    assert decide(snapshot(U, U, U), **{field: value}).action is AdmissionAction.HOLD


@pytest.mark.parametrize("bad", [None, -1, True, "0", 0.0])
def test_unknown_discovery_work_holds_even_when_replies_are_known(bad):
    assert (
        decide(with_row(snapshot(E, U, U), open_discovery_work=bad)).action is AdmissionAction.HOLD
    )


def test_missing_duplicate_or_reordered_city_holds():
    rows = snapshot(U, U, U)
    for invalid in [
        (),
        rows[:2],
        (*rows, rows[0]),
        (rows[0], rows[0], rows[2]),
        tuple(reversed(rows)),
        list(rows),
        (None, *rows[1:]),
    ]:
        assert decide(invalid).action is AdmissionAction.HOLD


@pytest.mark.parametrize(
    "budget",
    [
        DiscoveryBudget(),
        DiscoveryBudget(requests=0),
        DiscoveryBudget(pages=-1),
        DiscoveryBudget(requests=True),
    ],
)
def test_invalid_budget_never_authorizes_expansion(budget):
    assert (
        decide(snapshot(E, U, U), plan=CityTierPlan((budget, None, None))).action
        is AdmissionAction.HOLD
    )


def test_discovery_completion_is_not_order_purchase_or_supplier_reply_completion():
    rows = with_row(snapshot(E, B, E), pending_replies=5)
    result = decide(rows)
    assert result.action is AdmissionAction.COMPLETE
    assert result.reason == "route_discovery_complete"
    assert rows[0].pending_replies == 5


@pytest.mark.parametrize(
    "status",
    [
        TierStatus.UNCERTAIN,
        TierStatus.INCOMPLETE,
        TierStatus.PAUSED,
        TierStatus.BLOCKED,
        TierStatus.FAILED,
    ],
)
def test_nonterminal_city_cannot_be_reported_complete(status):
    assert decide(snapshot(status, U, U)).action in (AdmissionAction.HOLD, AdmissionAction.PAUSE)


def test_large_integer_clock_does_not_overflow_or_imply_exhaustion():
    result = decide(snapshot(R, U, U), now=10**500, deadline=10**500)
    assert result.action is AdmissionAction.PAUSE and result.reason == "execution_deadline"


@pytest.mark.parametrize(
    "failure",
    [
        AttemptOutcome.AUTH_REQUIRED,
        AttemptOutcome.CAPTCHA,
        AttemptOutcome.QUOTA,
        AttemptOutcome.TIMEOUT,
    ],
)
def test_failure_preserves_previous_success_without_consuming_remaining_budget(failure):
    row = replace(snapshot(R, U, U)[0], successful_requests=1, successful_pages=1)
    failed = record_discovery_attempt(row, failure, completed_pages=99)
    assert (failed.successful_requests, failed.successful_pages) == (1, 1)
    assert decide((failed, *snapshot(R, U, U)[1:])).action is AdmissionAction.PAUSE


def test_independent_item_groups_can_share_native_capacity():
    for active in (0, 1, 2):
        assert decide(snapshot(U, U, U), active_discovery_items=active).city == "Taraz"
    result = decide(snapshot(U, U, U), active_discovery_items=3)
    assert result.action is AdmissionAction.WAIT
    assert result.reason == "native_capacity_reserved"


@pytest.mark.parametrize("value", [None, True, -1, 2])
def test_same_item_cannot_overlap_two_cities(value):
    assert decide(snapshot(U, U, U), item_active_cities=value).action is AdmissionAction.HOLD


def test_running_item_does_not_block_an_independent_item():
    assert (
        decide(snapshot(R, U, U), item_active_cities=1, active_discovery_items=2).action
        is AdmissionAction.WAIT
    )
    assert decide(snapshot(E, U, U), active_discovery_items=2).city == "Shymkent"


def test_report_checkpoint_is_not_an_execution_deadline():
    assert decide(snapshot(U, U, U), now=None, deadline=None).city == "Taraz"
    assert decide(snapshot(E, U, U), now=None, deadline=None).city == "Shymkent"
