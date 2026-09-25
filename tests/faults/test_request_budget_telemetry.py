"""Budget observations around the real synchronous orchestrator; all boundaries mocked."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from src.agent import support_agent as support, telemetry
from src.agent.request_budget import CancellationEvidence, RequestBudget, RequestDeadlineExceeded

from .harness import Fault, Harness, Injection, PRIVATE_MARKERS, PROMPT, Stage


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def run_with_budget(harness, budget):
    return harness.run(lambda: support.run_support_agent_detailed(PROMPT, request_budget=budget))


def attempts(data):
    return [attempt for span in data["component_spans"] for attempt in span["attempts"]]


def comparable_history(harness):
    # SDK call IDs are opaque; compare order, tools/arguments and projected data.
    return [message if isinstance(message, str) else
            [{key: value for key, value in item.items() if key != "call_id"} for item in message]
            for message in harness.synthesis_inputs]


@pytest.mark.parametrize("outcome", [None, "clarification", "refusal", "unsupported_action", "not_found"])
def test_unlimited_observation_preserves_execution(faults, outcome):
    baseline = Harness(business_outcome=outcome).run()
    for budget in (RequestBudget(), RequestBudget(clock=Clock())):
        observed = run_with_budget(Harness(business_outcome=outcome), budget)
        assert observed.caught_error is None
        assert observed.calls == baseline.calls
        assert observed.policy == baseline.policy
        assert observed.trace.plan == baseline.trace.plan
        assert observed.tool_calls == baseline.tool_calls
        assert comparable_history(observed) == comparable_history(baseline)
        assert observed.exposed_tools == baseline.exposed_tools
        assert observed.response.final_output == baseline.response.final_output
        assert observed.response.context_wrapper.usage == baseline.response.context_wrapper.usage
        assert [s["component"] for s in observed.telemetry["component_spans"]] == [
            s["component"] for s in baseline.telemetry["component_spans"]]
        data = observed.telemetry
        assert data["request_id"] == budget.request_id
        assert data["deadline_budget_ms"] == budget.original_budget_ms
        assert data["deadline_exhausted"] == budget.exhausted()
        assert data["unknown_usage_attempt_count"] == 0
        assert data["usage_completeness"] == "COMPLETE"
        assert data["recovery_admission"] is None
        assert len(attempts(data)) == 2
        for attempt in attempts(data):
            assert attempt["attempt_number"] == 1
            assert attempt["status"] == "completed"
            assert attempt["sdk_visible_requests"] == 1
            assert attempt["http_retry_count"] is None
            assert attempt["retry_eligible"] is None and not attempt["retry_performed"]
            assert attempt["retry_reason"] is attempt["retry_denial_reason"] is attempt["retry_delay_ms"] is None
            assert attempt["usage_known"]
        assert sum(a["returned_usage"]["total_tokens"] for a in attempts(data)) == data["observed_usage"]["total_tokens"] == 44
        assert telemetry._budget.get() is None


def test_recovery_admission_is_unevaluated_and_is_a_separate_call(faults):
    # Unlimited execution retains recovery as a distinct call, even before a later fault.
    harness = Harness(Injection(Stage.SYNTHESIS, Fault.NETWORK, after_recovery=True))
    observed = run_with_budget(harness, RequestBudget(clock=Clock()))
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.RECOVERY] == observed.calls[Stage.SYNTHESIS] == 1
    data = observed.telemetry
    recorded = attempts(data)
    assert [a["component"] for a in recorded] == ["primary_router", "recovery_planner", "synthesis"]
    assert len({a["logical_call_id"] for a in recorded}) == 3
    assert [a["attempt_number"] for a in recorded] == [1, 1, 1]
    assert not any(a["retry_performed"] for a in recorded)
    assert data["recovery_admission"] == {
        "remaining_budget_ms": None, "required_downstream_reserve_ms": None,
        "minimum_work_ms": None, "allowance_ms": None, "admitted": None, "denial_reason": None,
    }
    assert data["unknown_usage_attempt_count"] == 1
    assert data["usage_completeness"] == "PARTIAL"
    assert recorded[-1]["failure_category"] == "NETWORK_ERROR"
    assert recorded[-1]["returned_usage"] == {} and not recorded[-1]["usage_known"]


def test_successful_recovery_is_unchanged_by_budget_observations(faults):
    class RecoveryHarness(Harness):
        @property
        def recovery(self):
            return True

    baseline = RecoveryHarness().run()
    observed = run_with_budget(RecoveryHarness(), RequestBudget(clock=Clock()))
    assert observed.caught_error is None
    assert observed.calls == baseline.calls
    assert observed.calls[Stage.RECOVERY] == 1
    assert observed.policy == baseline.policy
    assert observed.trace.plan == baseline.trace.plan
    assert observed.tool_calls == baseline.tool_calls
    assert comparable_history(observed) == comparable_history(baseline)
    assert observed.response.final_output == baseline.response.final_output
    assert observed.response.context_wrapper.usage.total_tokens == 66
    assert sum(a["returned_usage"]["total_tokens"] for a in attempts(observed.telemetry)) == 66
    assert [a["attempt_number"] for a in attempts(observed.telemetry)] == [1, 1, 1]


@pytest.mark.parametrize("known", [False, True])
def test_failed_attempt_usage_is_only_returned_evidence(faults, known):
    harness = Harness(Injection(Stage.ROUTER, Fault.TIMEOUT, failure_usage=known, sdk_requests=3))
    data = harness.run().telemetry
    attempt, = attempts(data)
    assert attempt["status"] == "failed" and attempt["failure_category"] == "TIMEOUT"
    assert attempt["exception_type"] == "CapabilityRoutingError"
    assert attempt["usage_known"] == known
    assert attempt["sdk_visible_requests"] == (3 if known else None)
    assert attempt["http_retry_count"] is None  # SDK request count is not HTTP retry count.
    assert data["unknown_usage_attempt_count"] == (0 if known else 1)
    assert data["usage_completeness"] == ("COMPLETE" if known else "UNAVAILABLE")
    assert not data["deadline_exhausted"] and not data["cancellation_observed"]


def test_budget_exhausted_by_router_stops_downstream_work(faults):
    clock = Clock()
    budget = RequestBudget(1000, clock=clock)

    class AdvancingHarness(Harness):
        def run_model(self, *args, **kwargs):
            response = super().run_model(*args, **kwargs)
            clock.now += 1.0
            return response

    harness = run_with_budget(AdvancingHarness(), budget)
    assert isinstance(harness.caught_error, RequestDeadlineExceeded)
    assert harness.response is None and not harness.tool_calls
    data = harness.telemetry
    assert data["terminal_status"] == "failed" and data["deadline_exhausted"]
    router, = attempts(data)
    assert router["status"] == "completed"
    assert router["remaining_budget_before_ms"] == 1000
    assert router["remaining_budget_after_ms"] == 0
    assert router["late_completion"] and router["result_abandoned"]
    assert router["result_accepted"] is False


def test_sanitized_serialization_and_truthful_states(faults):
    budget = RequestBudget(clock=Clock())
    budget.request_cancellation()
    budget.abandon_result()
    budget.mark_remote_outcome_unknown()
    data = run_with_budget(Harness(), budget).telemetry
    assert data["cancellation_requested"] and not data["cancellation_observed"]
    assert data["cancellation_evidence"] is None
    assert data["result_abandoned"] and data["remote_outcome_unknown"]
    assert data["terminal_status"] == "completed"  # Signals do not enforce yet.
    serialized = json.dumps(data)
    for marker in (*PRIVATE_MARKERS, PROMPT, "ORD-1001", "fake-raw-prompt-DO-NOT-LOG", "Authoritative status:"):
        assert marker not in serialized


def test_actual_local_cancellation_is_recorded_and_same_exception_propagates():
    error = asyncio.CancelledError()
    budget = RequestBudget(clock=Clock())

    @telemetry.observe_request
    def call(*, request_budget):
        with telemetry.observe("synthesis"):
            telemetry.model_call(SimpleNamespace(model=None, tools=[], instructions="private", output_type=None), default_resolution=False)
            raise error

    with pytest.raises(asyncio.CancelledError) as caught:
        call(request_budget=budget)
    assert caught.value is error
    data = telemetry.snapshot(error.production_telemetry)
    assert data["terminal_failure_category"] == "CANCELLED"
    assert data["cancellation_observed"] and not data["cancellation_requested"]
    assert data["cancellation_evidence"] == CancellationEvidence.LOCAL_TASK_CANCELLED
    assert not data["remote_outcome_unknown"]  # No inferred remote operation outcome.
    assert not data["result_abandoned"]
    assert telemetry._budget.get() is None


def test_concurrent_runtime_budgets_are_isolated(faults):
    barrier = Barrier(2)
    budgets = [RequestBudget(1000, clock=Clock()), RequestBudget(clock=Clock())]

    def run(budget):
        return run_with_budget(Harness(barrier=barrier), budget).telemetry

    with ThreadPoolExecutor(2) as executor:
        expired, unlimited = executor.map(run, budgets)
    assert expired["request_id"] != unlimited["request_id"]
    assert not expired["deadline_exhausted"] and not expired["cancellation_requested"]
    assert not unlimited["deadline_exhausted"] and not unlimited["cancellation_requested"]
    assert all(a["remaining_budget_before_ms"] == 1000 for a in attempts(expired))
    assert all(a["remaining_budget_before_ms"] is None for a in attempts(unlimited))
    assert not {a["logical_call_id"] for a in attempts(expired)} & {a["logical_call_id"] for a in attempts(unlimited)}
    assert telemetry._budget.get() is None


def test_nested_requests_restore_outer_budget(faults):
    outer_budget = RequestBudget(0, clock=Clock())

    @telemetry.observe_request
    def outer(*, request_budget):
        inner = Harness().run()
        assert inner.telemetry["deadline_budget_ms"] is None
        assert not inner.telemetry["deadline_exhausted"]
        assert telemetry._budget.get() is outer_budget
        assert telemetry._request.get().request_id == outer_budget.request_id
        return inner.response

    result = outer(request_budget=outer_budget)
    assert result.context_wrapper.production_telemetry.deadline_exhausted
    assert telemetry._budget.get() is None


def test_broken_authoritative_budget_fails_closed(faults):
    clock = Clock()
    budget = RequestBudget(1000, clock=clock)
    clock.now = None  # Simulate a broken diagnostic clock after budget construction.
    observed = run_with_budget(Harness(), budget)
    assert isinstance(observed.caught_error, TypeError)
    assert observed.response is None and not observed.tool_calls
    assert not observed.calls
    assert observed.telemetry["observation_incomplete"]
    assert telemetry._budget.get() is None
