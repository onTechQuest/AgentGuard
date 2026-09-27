"""Real orchestration under isolated fake clocks; never a live provider call."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.agent import request_execution, support_agent as support
from src.agent.qualification_budget import QualificationBudgetPolicy
from src.agent.runtime_reliability import RuntimeReliabilityPolicy
from src.agent.request_budget import RequestBudget
from src.agentguard.reliability import sanitize_measurement, summarize_enforcement
from .harness import PROMPT, Stage
from .test_deadline_execution import Clock, TimedHarness, assert_deadline, attempts, span


L = QualificationBudgetPolicy(10000, 4000, 2000, 3000)
R = QualificationBudgetPolicy(20000, 13000, 3000, 6000)


def run_candidate(harness, policy=L, budget=None):
    return harness.run(lambda: support.run_support_agent_detailed(
        PROMPT, runtime_reliability_policy=policy.runtime_policy(),
        request_budget=budget or RequestBudget(policy.request_deadline_ms, clock=harness.clock)))


@pytest.mark.parametrize("duration", [0, 3999])
def test_router_within_cap_proceeds(faults, duration):
    observed = run_candidate(TimedHarness(Clock(), durations={Stage.ROUTER: duration}))
    assert observed.caught_error is None
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.SYNTHESIS] == 1
    assert len(observed.tool_calls) == 1
    assert span(observed, "primary_router")["allocated_allowance_ms"] == 4000


@pytest.mark.parametrize("duration", [4000, 4001, 5171.3789])
def test_router_rejected_inside_request_deadline(faults, duration):
    observed = run_candidate(TimedHarness(Clock(), durations={Stage.ROUTER: duration}))
    assert_deadline(observed, "primary_router")
    assert observed.calls == {Stage.ROUTER: 1} and not observed.tool_calls
    assert observed.policy is None and not observed.synthesis_inputs
    assert not observed.telemetry["deadline_exhausted"]
    assert observed.telemetry["observed_usage"]["total_tokens"] == 14
    assert attempts(observed)[0]["late_completion"]
    assert observed.telemetry["result_abandoned"]


def test_request_deadline_dominates_larger_cap(faults):
    clock = Clock()
    observed = run_candidate(TimedHarness(clock, durations={Stage.ROUTER: 1000}),
                             budget=RequestBudget(1000, clock=clock))
    assert_deadline(observed, "primary_router")
    assert span(observed, "primary_router")["allocated_allowance_ms"] == 1000
    assert observed.telemetry["deadline_exhausted"]


def test_existing_request_is_not_restarted_when_candidate_caps_it(faults):
    clock = Clock()
    budget = RequestBudget(20000, clock=clock)
    clock.advance(9999)
    observed = run_candidate(TimedHarness(clock, durations={Stage.ROUTER: 1}), budget=budget)
    assert_deadline(observed, "primary_router")
    assert observed.telemetry["deadline_budget_ms"] == 10000
    assert span(observed, "primary_router")["allocated_allowance_ms"] == pytest.approx(1)


def test_already_exhausted_candidate_cap_has_observed_admission_failure(faults):
    clock = Clock()
    budget = RequestBudget(clock=clock)
    clock.advance(10001)
    observed = run_candidate(TimedHarness(clock), budget=budget)
    assert_deadline(observed, "primary_router")
    assert observed.calls == {} and not observed.telemetry["result_abandoned"]


def test_limiting_request_keeps_shared_cancellation_evidence():
    clock = Clock()
    budget = RequestBudget(20000, clock=clock)
    limited = budget.limit(10000)
    budget.request_cancellation()
    assert limited.cancellation_requested
    assert not limited.cancellation_observed
    assert limited.request_id == budget.request_id
    assert limited.limit(30000).deadline_monotonic == limited.deadline_monotonic


def test_r_dynamic_budget_needs_no_static_22_second_sum(faults):
    observed = run_candidate(TimedHarness(Clock(), durations={Stage.ROUTER: 12900, Stage.SYNTHESIS: 5900}), R)
    assert observed.caught_error is None
    assert observed.telemetry["deadline_budget_ms"] == 20000


def test_slow_r_router_denies_recovery_before_factory(faults, monkeypatch):
    def forbidden_factory(*args, **kwargs):
        pytest.fail("Inadmissible recovery constructed its planner")
    monkeypatch.setattr(support, "SemanticRecoveryPlanner", forbidden_factory)
    observed = run_candidate(TimedHarness(Clock(), recovery=True, durations={Stage.ROUTER: 12000}), R)
    assert_deadline(observed, "recovery_planner")
    assert observed.calls == {Stage.ROUTER: 1}
    assert not observed.tool_calls
    assert observed.telemetry["recovery_admission"] == {
        "remaining_budget_ms": 8000, "required_downstream_reserve_ms": 6000,
        "minimum_work_ms": 3000, "allowance_ms": 2000, "admitted": False,
        "denial_reason": "INSUFFICIENT_ALLOWANCE"}


@pytest.mark.parametrize("policy", [L, R])
def test_observed_recovery_timings_fit_both_candidates(faults, policy):
    observed = run_candidate(TimedHarness(Clock(), recovery=True, durations={
        Stage.ROUTER: 1716.0662000533193, Stage.RECOVERY: 1572.716899914667,
        Stage.TOOL: 0.5215000128373504, Stage.SYNTHESIS: 1068.9058999996632}), policy)
    assert observed.caught_error is None
    assert observed.telemetry["recovery_admission"]["admitted"] is True
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.RECOVERY] == observed.calls[Stage.SYNTHESIS] == 1
    assert len({a["logical_call_id"] for a in attempts(observed)}) == 3
    assert all(a["attempt_number"] == 1 and not a["retry_performed"] for a in attempts(observed))


def test_recovery_cannot_consume_reserved_synthesis_time(faults):
    observed = run_candidate(TimedHarness(Clock(), recovery=True, durations={
        Stage.ROUTER: 1000, Stage.RECOVERY: 6001}))
    assert_deadline(observed, "recovery_planner")
    assert observed.telemetry["observed_usage"]["total_tokens"] == 36
    assert not observed.tool_calls and not observed.synthesis_inputs
    assert not observed.telemetry["deadline_exhausted"]


@pytest.mark.parametrize("duration,success", [(2999, True), (3000, False), (3001, False)])
def test_synthesis_cap_and_completed_tool_truth(faults, duration, success):
    observed = run_candidate(TimedHarness(Clock(), durations={Stage.SYNTHESIS: duration}))
    assert (observed.caught_error is None) is success
    if not success:
        assert_deadline(observed, "synthesis")
        assert observed.telemetry["result_abandoned"]
        assert attempts(observed)[-1]["status"] == "completed"
        assert attempts(observed)[-1]["result_abandoned"]
    assert len(observed.tool_calls) == 1
    operation, = observed.trace.executions
    assert operation.status == "completed" and operation.invoked
    assert observed.telemetry["observed_usage"]["total_tokens"] == 44
    assert observed.telemetry["usage_completeness"] == "COMPLETE"
    assert not observed.telemetry["cancellation_requested"]
    assert not observed.telemetry["cancellation_observed"]


def test_report_retains_rejected_usage_and_operation_counts(faults):
    observed = run_candidate(TimedHarness(Clock(), durations={Stage.SYNTHESIS: 3001}))
    row = sanitize_measurement(observed.telemetry, status="failed", elapsed=3001)
    assert row["completed_operation_count"] == 1 and row["incomplete_operation_count"] == 0
    assert row["stage_budgets"][-1]["result_abandoned"]
    assert row["stage_budgets"][-1]["configured_stage_cap_ms"] == 3000
    summary = summarize_enforcement([row])
    assert summary["deadline_rejections"] == 1
    assert summary["rejection_stages"] == {"synthesis": 1}
    assert summary["completed_operations_before_rejection"] == 1
    assert summary["rejected_request_usage"]["total_tokens"] == 44
    assert summary["cancellation_observed"] == summary["cancellation_requested"] == 0


def test_explicit_diagnostic_policy_stays_unlimited(faults):
    harness = TimedHarness(Clock(), durations={Stage.ROUTER: 25000, Stage.SYNTHESIS: 9000})
    observed = harness.run(lambda: support.run_support_agent_detailed(
        PROMPT, runtime_reliability_policy=RuntimeReliabilityPolicy.unbounded()))
    assert observed.caught_error is None
    assert observed.telemetry["deadline_budget_ms"] is None
    assert all(s["configured_stage_cap_ms"] is None for s in observed.telemetry["component_spans"])


def test_concurrent_candidates_are_independent(faults):
    barrier = Barrier(2)
    class ConcurrentHarness(TimedHarness):
        def run_model(self, agent, *args, **kwargs):
            if agent.name == "Capability Router":
                barrier.wait(timeout=5)
            return super().run_model(agent, *args, **kwargs)
    def evaluate(policy):
        observed = run_candidate(ConcurrentHarness(Clock(), durations={Stage.ROUTER: 5000}), policy)
        assert request_execution._runtime_policy.get() is None
        assert request_execution._stage_budget.get() is None
        return observed
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(evaluate, [L, R]))
    assert_deadline(first, "primary_router")
    assert second.caught_error is None
    assert first.telemetry["request_id"] != second.telemetry["request_id"]
