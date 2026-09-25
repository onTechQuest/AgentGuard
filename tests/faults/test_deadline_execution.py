"""Authoritative finite-budget contracts; real orchestration, fake clock and I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from threading import Barrier

import pytest

from src.agent import capability_router, execution_plan, request_execution, support_agent as support, telemetry
from src.agent.request_budget import RecoveryBudgetPolicy, RequestBudget, RequestDeadlineExceeded
from src.agent.runtime_reliability import RuntimeReliabilityPolicy

from .harness import Harness, MODEL_COMPONENTS, PRIVATE_MARKERS, PROMPT, Stage


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds / 1000


class TimedHarness(Harness):
    def __init__(self, clock, *, durations=None, recovery=False, multiple=False, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.durations = durations or {}
        self.want_recovery = recovery
        self.multiple = multiple

    @property
    def recovery(self):
        return self.want_recovery

    def run_model(self, agent, *args, **kwargs):
        response = super().run_model(agent, *args, **kwargs)
        component = MODEL_COMPONENTS[agent.name]
        if component == Stage.ROUTER and self.multiple:
            response.final_output["capability_requests"].append({
                "capability": "order_status", "order_id": "ORD-1002", "needs_clarification": False,
            })
        self.clock.advance(self.durations.get(component, 0))
        return response

    def tool(self, name, **kwargs):
        result = super().tool(name, **kwargs)
        self.clock.advance(self.durations.get(Stage.TOOL, 0))
        return result


def run(harness, milliseconds=500, *, policy=None, budget=None):
    budget = budget if budget is not None else RequestBudget(milliseconds, clock=harness.clock)
    message = PROMPT + (" Also where is ORD-1002?" if harness.multiple else "")
    return harness.run(lambda: support.run_support_agent_detailed(
        message, request_budget=budget, recovery_budget_policy=policy,
        runtime_reliability_policy=RuntimeReliabilityPolicy.unbounded()))


def span(observed, component):
    return next(item for item in observed.telemetry["component_spans"] if item["component"] == component)


def attempts(observed):
    return [a for s in observed.telemetry["component_spans"] for a in s["attempts"]]


def assert_deadline(observed, component):
    assert isinstance(observed.caught_error, RequestDeadlineExceeded)
    assert observed.response is None  # No accepted late answer or invented fallback.
    data = observed.telemetry
    assert data["terminal_status"] == "failed"
    assert data["terminal_failure_category"] == "DEADLINE_EXHAUSTED"
    assert data["terminal_failure_component"] == component
    assert not data["cancellation_requested"] and not data["cancellation_observed"]
    assert not data["remote_outcome_unknown"]
    assert all(a["http_retry_count"] is None and not a["retry_performed"] for a in attempts(observed))
    assert all(a["attempt_number"] == 1 for a in attempts(observed))
    serialized = json.dumps(data)
    for private in (*PRIVATE_MARKERS, PROMPT, "ORD-1001", "ORD-1002", "Authoritative status:"):
        assert private not in serialized
    assert request_execution._active_budget.get() is None
    assert request_execution._recovery_policy.get() is None


def expire_before(monkeypatch, module, component, clock, *, occurrence=1):
    original = module.stage
    count = 0

    @contextmanager
    def boundary(name, **kwargs):
        nonlocal count
        if name == component:
            count += 1
            if count == occurrence:
                clock.advance(500)
        with original(name, **kwargs):
            yield

    monkeypatch.setattr(module, "stage", boundary)


def test_exhausted_before_router_dispatches_nothing(faults):
    observed = run(TimedHarness(Clock()), 0)
    assert_deadline(observed, "primary_router")
    assert not observed.calls and not observed.tool_calls
    assert observed.policy is None and observed.trace is None
    router = span(observed, "primary_router")
    assert router["stage_admitted"] is False and router["status"] == "not_started"
    assert router["admission_denial_reason"] == "DEADLINE_EXHAUSTED"
    assert router["attempts"] == []
    assert not observed.telemetry["result_abandoned"]


def test_router_just_before_deadline_continues(faults):
    observed = run(TimedHarness(Clock(), durations={Stage.ROUTER: 499}))
    assert observed.caught_error is None
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.SYNTHESIS] == 1
    assert len(observed.tool_calls) == 1
    assert span(observed, "primary_router")["result_accepted"] is True
    assert span(observed, "synthesis")["remaining_budget_before_ms"] == pytest.approx(1)
    assert observed.response.final_output == "Authoritative status: processing."


@pytest.mark.parametrize("duration", [500, 501])
def test_router_at_or_after_deadline_preserves_usage_and_stops(faults, duration):
    observed = run(TimedHarness(Clock(), durations={Stage.ROUTER: duration}))
    assert_deadline(observed, "primary_router")
    assert observed.calls == {Stage.ROUTER: 1}
    assert observed.policy is None and observed.trace is None and not observed.tool_calls
    router = span(observed, "primary_router")
    assert router["status"] == "completed" and router["stage_admitted"]
    assert router["result_accepted"] is False and router["late_completion"] and router["result_abandoned"]
    attempt, = attempts(observed)
    assert attempt["status"] == "completed" and attempt["failure_category"] is None
    assert attempt["late_completion"] and attempt["usage_known"]
    assert observed.telemetry["observed_usage"]["total_tokens"] == 14
    assert observed.telemetry["usage_completeness"] == "COMPLETE"


def test_expiry_between_router_and_policy(faults, monkeypatch):
    clock = Clock()
    original = support.validate_planning

    def planning(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(500)
        return result

    monkeypatch.setattr(support, "validate_planning", planning)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "policy_resolution")
    assert span(observed, "primary_router")["result_accepted"]
    assert not span(observed, "policy_resolution")["stage_admitted"]
    assert observed.policy is None and observed.trace is None and not observed.tool_calls


def test_execution_plan_admission_is_checked(faults, monkeypatch):
    clock = Clock()
    expire_before(monkeypatch, support, "execution_plan", clock)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "execution_plan")
    assert observed.policy is not None
    assert observed.calls[Stage.PLAN] == 0 and observed.trace is None
    assert not observed.tool_calls and not observed.synthesis_inputs


@pytest.mark.parametrize("stage_name", ["policy_resolution", "execution_plan"])
def test_local_stage_late_result_stops_continuation(faults, monkeypatch, stage_name):
    clock = Clock()
    attribute = "resolve_request_policy" if stage_name == "policy_resolution" else "build_execution_plan"
    original = getattr(support, attribute)

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(501)
        return result

    monkeypatch.setattr(support, attribute, delayed)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, stage_name)
    assert span(observed, stage_name)["status"] == "completed"
    assert span(observed, stage_name)["late_completion"]
    assert not observed.tool_calls and not observed.synthesis_inputs


def recovery_policy():
    return RecoveryBudgetPolicy(recovery_allowance_ms=100, required_execution_reserve_ms=200,
                               synthesis_reserve_ms=150, completion_reserve_ms=50)


def test_recovery_exact_reserve_boundary_admitted(faults):
    observed = run(TimedHarness(Clock(), recovery=True), policy=recovery_policy())
    assert observed.caught_error is None
    assert observed.calls[Stage.RECOVERY] == 1
    evidence = observed.telemetry["recovery_admission"]
    assert evidence == {"remaining_budget_ms": 500, "required_downstream_reserve_ms": 400,
                        "minimum_work_ms": 100, "allowance_ms": 100, "admitted": True, "denial_reason": None}
    recorded = attempts(observed)
    assert len({a["logical_call_id"] for a in recorded}) == 3
    assert all(a["attempt_number"] == 1 for a in recorded)
    assert observed.telemetry["observed_usage"]["total_tokens"] == 66


def test_recovery_denied_without_fallback_or_factory_call(faults, monkeypatch):
    def forbidden_factory(*args, **kwargs):
        pytest.fail("Recovery planner must not be constructed after denied admission")

    monkeypatch.setattr(support, "SemanticRecoveryPlanner", forbidden_factory)
    observed = run(TimedHarness(Clock(), recovery=True), 499, policy=recovery_policy())
    assert_deadline(observed, "recovery_planner")
    assert observed.calls == {Stage.ROUTER: 1}
    assert not observed.tool_calls and observed.policy is None
    assert observed.caught_error.planning.recovery_count == 0
    assert not observed.caught_error.planning.recovery_attempted
    evidence = observed.telemetry["recovery_admission"]
    assert evidence["admitted"] is False and evidence["denial_reason"] == "INSUFFICIENT_ALLOWANCE"
    assert evidence["required_downstream_reserve_ms"] == 400
    assert not observed.telemetry["deadline_exhausted"]  # Insufficient reserve, not elapsed deadline.
    assert not observed.telemetry["result_abandoned"]


def test_recovery_returns_late(faults):
    observed = run(TimedHarness(Clock(), recovery=True, durations={Stage.RECOVERY: 501}))
    assert_deadline(observed, "recovery_planner")
    assert observed.calls == {Stage.ROUTER: 1, Stage.RECOVERY: 1}
    assert observed.policy is None and not observed.tool_calls
    assert observed.caught_error.planning.recovery_count == 1
    assert span(observed, "recovery_planner")["status"] == "completed"
    assert attempts(observed)[-1]["result_abandoned"]
    assert observed.telemetry["observed_usage"]["total_tokens"] == 36


def test_expiry_before_first_required_operation(faults, monkeypatch):
    clock = Clock()
    expire_before(monkeypatch, execution_plan, "tool", clock)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "tool")
    operation, = observed.trace.executions
    assert operation.status == "pending" and not operation.invoked
    assert operation.output is None and not observed.tool_calls
    assert not observed.synthesis_inputs
    assert span(observed, "tool")["operation_id"] == operation.call_id
    assert observed.telemetry["required_operation_summary"]["incomplete"] == [operation.call_id]


def test_expiry_between_required_operations(faults, monkeypatch):
    clock = Clock()
    expire_before(monkeypatch, execution_plan, "tool", clock, occurrence=2)
    observed = run(TimedHarness(clock, multiple=True))
    assert_deadline(observed, "tool")
    first, second = observed.trace.executions
    assert first.status == "completed" and first.invoked
    assert second.status == "pending" and not second.invoked
    assert len(observed.tool_calls) == 1 and not observed.synthesis_inputs
    assert observed.telemetry["required_operation_summary"] == {
        "required": [first.call_id, second.call_id], "completed": [first.call_id], "incomplete": [second.call_id],
    }


def test_late_tool_keeps_completed_projected_outcome(faults):
    observed = run(TimedHarness(Clock(), multiple=True, durations={Stage.TOOL: 501}))
    assert_deadline(observed, "tool")
    first, second = observed.trace.executions
    assert first.status == "completed" and first.invoked and first.error is None
    assert first.output["status"] == "processing"
    assert "customer_id" not in first.output and "internal" not in first.output
    assert second.status == "pending" and not second.invoked
    assert len(observed.tool_calls) == 1 and not observed.synthesis_inputs
    tool = span(observed, "tool")
    assert tool["status"] == "completed" and tool["late_completion"]
    assert tool["result_abandoned"] and tool["result_accepted"] is False
    assert observed.caught_error.trace is observed.trace


def test_synthesis_denied_before_dispatch(faults, monkeypatch):
    clock = Clock()
    expire_before(monkeypatch, support, "synthesis", clock)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "synthesis")
    assert not observed.trace.missing_required
    assert observed.calls[Stage.SYNTHESIS] == 0 and not observed.synthesis_inputs
    assert span(observed, "synthesis")["status"] == "not_started"
    assert span(observed, "synthesis")["attempts"] == []


def test_preparation_exhaustion_does_not_dispatch_synthesis(faults, monkeypatch):
    clock = Clock()
    original = execution_plan.ExecutionTrace.model_input

    def delayed(*args, **kwargs):
        value = original(*args, **kwargs)
        clock.advance(500)
        return value

    monkeypatch.setattr(execution_plan.ExecutionTrace, "model_input", delayed)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "synthesis")
    assert not observed.trace.missing_required
    assert observed.calls[Stage.SYNTHESIS] == 0


def test_router_preparation_exhaustion_keeps_typed_failure(faults, monkeypatch):
    clock = Clock()
    original = capability_router.extract_entities

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.advance(500)
        return result

    monkeypatch.setattr(capability_router, "extract_entities", delayed)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "primary_router")
    assert not observed.calls and not attempts(observed)


def test_tool_lookup_exhaustion_leaves_operation_pending(faults, monkeypatch):
    clock = Clock()
    original = support.execute_required

    def required(trace, resolve):
        def delayed(name):
            implementation = resolve(name)
            clock.advance(500)
            return implementation
        return original(trace, delayed)

    monkeypatch.setattr(support, "execute_required", required)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "tool")
    assert observed.trace.executions[0].status == "pending"
    assert not observed.trace.executions[0].invoked
    assert not observed.tool_calls and not observed.synthesis_inputs


def test_late_synthesis_answer_is_not_returned(faults):
    observed = run(TimedHarness(Clock(), durations={Stage.SYNTHESIS: 501}))
    assert_deadline(observed, "synthesis")
    assert not observed.trace.missing_required
    assert observed.calls[Stage.SYNTHESIS] == 1 and len(observed.synthesis_inputs) == 1
    synthesis = span(observed, "synthesis")
    assert synthesis["status"] == "completed" and synthesis["late_completion"]
    assert synthesis["result_abandoned"] and synthesis["result_accepted"] is False
    assert observed.telemetry["observed_usage"]["total_tokens"] == 44
    assert attempts(observed)[-1]["returned_usage"]["total_tokens"] == 30
    assert observed.caught_error.trace is observed.trace


def test_final_acceptance_after_local_postprocessing(faults, monkeypatch):
    clock = Clock()
    expire_before(monkeypatch, request_execution, "response_acceptance", clock)
    observed = run(TimedHarness(clock))
    assert_deadline(observed, "response_acceptance")
    assert span(observed, "synthesis")["result_accepted"] is True
    assert observed.telemetry["result_abandoned"]


def test_finite_deadline_still_enforced_without_telemetry(faults, monkeypatch):
    def broken_observer(*args, **kwargs):
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(telemetry, "ProductionExecutionTelemetry", broken_observer)
    observed = run(TimedHarness(Clock()), 0)
    assert isinstance(observed.caught_error, RequestDeadlineExceeded)
    assert not observed.calls and not observed.tool_calls and observed.response is None
    assert request_execution._active_budget.get() is None


def test_concurrent_deadlines_are_independent(faults):
    barrier = Barrier(2)
    late = TimedHarness(Clock(), barrier=barrier, durations={Stage.ROUTER: 501})
    healthy = TimedHarness(Clock(), barrier=barrier, durations={Stage.ROUTER: 100})
    with ThreadPoolExecutor(2) as executor:
        expired, success = executor.map(run, (late, healthy))
    assert_deadline(expired, "primary_router")
    assert success.caught_error is None and success.response is not None
    assert not success.telemetry["deadline_exhausted"]
    assert expired.telemetry["request_id"] != success.telemetry["request_id"]
    assert not expired.tool_calls and len(success.tool_calls) == 1


def test_unlimited_ignores_explicit_reserves_and_long_stage_durations(faults):
    observed = run(TimedHarness(Clock(), recovery=True, durations={Stage.RECOVERY: 1_000_000}),
                   None, policy=recovery_policy())
    assert observed.caught_error is None
    assert observed.calls[Stage.RECOVERY] == observed.calls[Stage.SYNTHESIS] == 1
    assert len(observed.tool_calls) == 1 and observed.response.final_output == "Authoritative status: processing."
    assert observed.telemetry["recovery_admission"]["admitted"] is None
    assert observed.telemetry["deadline_budget_ms"] is None


@pytest.mark.parametrize("field", ["recovery_allowance_ms", "required_execution_reserve_ms", "synthesis_reserve_ms", "completion_reserve_ms"])
def test_recovery_policy_rejects_invalid_values(field):
    with pytest.raises(ValueError):
        RecoveryBudgetPolicy(**{field: -1})
