"""Pure admission tests: no real waiting, SDK, or network calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier

import pytest

from src.agent.request_budget import (
    AdmissionDenial, BudgetAdmissionError, CancellationEvidence, ReliabilityState, RequestBudget,
)


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance_ms(self, amount):
        self.now += amount / 1000


def test_unlimited_budget_and_unlimited_child():
    clock = Clock()
    budget = RequestBudget(clock=clock)
    clock.advance_ms(1_000_000)
    assert budget.remaining_ms() is budget.deadline_monotonic is budget.original_budget_ms is None
    assert not budget.exhausted()
    assert budget.can_start(minimum_ms=1e12, reserve_ms=1e12)
    child = budget.child_budget(reserve_ms=100)
    assert child.remaining_ms() is None
    assert child.request_id == budget.request_id
    assert budget.child_budget(cap_ms=50).remaining_ms() == pytest.approx(50)


def test_remaining_uses_only_injected_monotonic_clock(monkeypatch):
    clock = Clock()
    budget = RequestBudget(1000, clock=clock)
    monkeypatch.setattr("time.time", lambda: -999_999)  # Wall-clock jumps cannot affect admission.
    assert budget.started_at_monotonic == 100
    assert budget.deadline_monotonic == 101
    assert budget.remaining_ms() == 1000
    assert not budget.exhausted()
    clock.advance_ms(250)
    assert budget.remaining_ms() == 750
    assert budget.can_start(minimum_ms=500, reserve_ms=250)
    clock.advance_ms(750)
    assert budget.exhausted() and budget.remaining_ms() == 0
    assert not budget.can_start()
    clock.advance_ms(500)
    assert budget.remaining_ms() == 0


def test_admission_before_dispatch_and_between_stages():
    clock = Clock()
    budget = RequestBudget(1000, clock=clock)
    assert budget.require_remaining(minimum_ms=250, reserve_ms=250).admitted
    clock.advance_ms(750)
    evidence = budget.admission(minimum_ms=1, reserve_ms=250)
    assert evidence.remaining_budget_ms == 250
    assert evidence.required_downstream_reserve_ms == 250
    assert evidence.allowance_ms == 0
    assert evidence.denial_reason == AdmissionDenial.INSUFFICIENT_ALLOWANCE
    assert not evidence.admitted
    clock.advance_ms(250)
    with pytest.raises(BudgetAdmissionError) as caught:
        budget.require_remaining()
    assert caught.value.evidence.denial_reason == AdmissionDenial.DEADLINE_EXHAUSTED
    assert not RequestBudget(0, clock=clock).can_start()


def test_child_deadline_and_downstream_reserve():
    clock = Clock()
    parent = RequestBudget(1000, clock=clock)
    clock.advance_ms(250)
    child = parent.child_budget(cap_ms=2000, reserve_ms=250)
    assert child.remaining_ms() == 500
    assert child.deadline_monotonic == parent.deadline_monotonic - .250
    grandchild = child.child_budget(cap_ms=1000, reserve_ms=250)
    assert grandchild.remaining_ms() == 250
    clock.advance_ms(500)
    assert child.exhausted() and grandchild.exhausted()
    assert not parent.exhausted()
    with pytest.raises(BudgetAdmissionError):
        child.child_budget()


def test_child_creation_delay_cannot_extend_absolute_parent_deadline():
    # Admission's clock read and child creation are not simultaneous.
    moments = iter([100.0, 100.5, 100.75, 100.75])
    parent = RequestBudget(1000, clock=lambda: next(moments))
    child = parent.child_budget(reserve_ms=125)
    assert child.deadline_monotonic == 100.875
    assert child.remaining_ms() == 125
    with pytest.raises(FrozenInstanceError):
        child.deadline_monotonic = 200


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "100"])
def test_invalid_budget_or_admission_values(value):
    with pytest.raises(ValueError):
        RequestBudget(value)
    budget = RequestBudget()
    for name in ("minimum_ms", "reserve_ms", "cap_ms"):
        with pytest.raises(ValueError):
            budget.admission(**{name: value})


def test_reserve_and_cap_do_not_invent_a_deadline():
    budget = RequestBudget(clock=Clock())
    evidence = budget.admission(minimum_ms=25, reserve_ms=100, cap_ms=50)
    assert evidence.admitted and evidence.allowance_ms == 50
    assert evidence.remaining_budget_ms is None
    assert not budget.can_start(minimum_ms=51, cap_ms=50)
    assert not budget.can_start(cap_ms=0)
    assert budget.deadline_monotonic is None


def test_cancellation_abandonment_and_remote_outcome_are_independent():
    parent = RequestBudget(clock=Clock())
    child = parent.child_budget()
    parent.request_cancellation()
    parent.abandon_result()
    parent.mark_remote_outcome_unknown()
    assert parent.cancellation_requested and not parent.cancellation_observed
    assert parent.result_abandoned and parent.remote_outcome_unknown
    assert not parent.exhausted()
    assert child.result_abandoned and not child.can_start()
    assert child.admission().denial_reason == AdmissionDenial.CANCELLATION_REQUESTED
    with pytest.raises(ValueError):
        parent.observe_cancellation("wait timed out")
    assert not parent.cancellation_observed
    child.observe_cancellation(CancellationEvidence.COOPERATIVE_ACKNOWLEDGEMENT)
    assert parent.cancellation_observed
    assert parent.cancellation_evidence == CancellationEvidence.COOPERATIVE_ACKNOWLEDGEMENT
    assert parent.admission().denial_reason == AdmissionDenial.CANCELLED


def test_deadline_exhaustion_does_not_claim_cancellation_or_remote_outcome():
    budget = RequestBudget(0, clock=Clock())
    assert budget.exhausted()
    assert not budget.cancellation_requested and not budget.cancellation_observed
    assert not budget.result_abandoned and not budget.remote_outcome_unknown
    assert len(set(ReliabilityState)) == 5


def test_independent_concurrent_request_budgets():
    barrier = Barrier(2)

    def run(expire):
        clock = Clock()
        budget = RequestBudget(1000, clock=clock)
        barrier.wait(timeout=5)
        if expire:
            clock.advance_ms(1000)
            budget.request_cancellation()
        return budget

    with ThreadPoolExecutor(2) as executor:
        first, second = executor.map(run, (True, False))
    assert first.request_id != second.request_id
    assert first.exhausted() and first.cancellation_requested
    assert not second.exhausted() and not second.cancellation_requested
