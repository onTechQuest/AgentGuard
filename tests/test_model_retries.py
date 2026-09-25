"""AgentGuard retry orchestration against the real SDK and an offline HTTP wire."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from threading import Barrier

import pytest
from agents.usage import Usage

from src.agent import model_execution
from src.agent.request_budget import RequestBudget, RecoveryBudgetPolicy
from src.agent.retry_policy import current_retry_state
from test_retry_policy import policy
from test_model_execution_transport import (
    transport, recorded_attempts, normalized_bodies, COMPONENTS,
)


def run(wire, **options):
    delays = []
    def sleep(seconds):
        delays.append(seconds)
        wire.clock.now += seconds
    wire.delays = delays
    return wire.run(retry_policy=options.pop("retry_policy", policy()),
                    retry_sleeper=options.pop("retry_sleeper", sleep), **options)


def attempts(wire, component):
    return [a for a in recorded_attempts(wire) if a["component"] == component]


def assert_wire_matches_attempts(wire, transport):
    assert sum(wire.attempts.values()) == len(recorded_attempts(wire))
    assert all(h["x-stainless-retry-count"] == "0" for h in wire.headers)
    assert all(a["lower_layer_retries_configured"] is False for a in recorded_attempts(wire))
    assert transport.sleeps == []  # No lower-layer retry slept or amplified attempts.


@pytest.mark.parametrize("component", COMPONENTS)
@pytest.mark.parametrize("failure", ["network", "connect_timeout", "500", "503", "429"])
def test_transient_retry_success_preserves_logical_call(transport, component, failure):
    wire = run(transport(failure=failure, failed_component=component, recovery=component == "recovery_planner"))
    assert wire.error is None and wire.result is not None
    affected = attempts(wire, component)
    assert [a["attempt_number"] for a in affected] == [1, 2]
    assert affected[0]["logical_call_id"] == affected[1]["logical_call_id"]
    assert affected[0]["status"] == "failed" and affected[0]["retry_performed"]
    assert affected[1]["status"] == "completed"
    assert affected[0]["delivery_certainty"] == ("NOT_SENT" if failure in {"network", "connect_timeout"} else "KNOWN_FAILED")
    assert wire.observed["terminal_failure_category"] is None
    assert wire.observed["retry_allowance_consumed"] == wire.observed["retry_attempts_total"] == 1
    assert wire.observed["retry_allowance_remaining"] == 0
    assert len(wire.tool_calls) == 1 and all(body["tools"] == [] for body in wire.bodies)
    assert wire.result.context_wrapper.usage.total_tokens == (66 if wire.recovery else 44)
    assert wire.observed["observed_usage"]["total_tokens"] == wire.result.context_wrapper.usage.total_tokens
    assert wire.observed["usage_completeness"] == "PARTIAL"
    assert wire.observed["unknown_usage_attempt_count"] == 1
    assert wire.delays == [0.01]
    assert_wire_matches_attempts(wire, transport)
    if wire.recovery:
        assert len({a["logical_call_id"] for a in recorded_attempts(wire)}) == 3
        assert wire.attempts["primary_router"] == 1  # No new recovery cycle.


@pytest.mark.parametrize("component", COMPONENTS)
def test_retry_exhaustion_preserves_both_failures(transport, component):
    wire = run(transport(failure_schedule={component: ["network", "429"]}, recovery=component == "recovery_planner"))
    assert wire.error is not None and wire.result is None
    affected = attempts(wire, component)
    assert len(affected) == 2 and all(a["status"] == "failed" for a in affected)
    assert affected[-1]["retry_denial_reason"] == "MAX_ATTEMPTS_EXHAUSTED"
    assert wire.observed["retry_exhausted"]
    assert wire.observed["terminal_failure_category"] == "RATE_LIMIT"
    assert len(wire.tool_calls) == (1 if component == "synthesis" else 0)
    assert_wire_matches_attempts(wire, transport)


def test_third_attempt_requires_explicit_allowance(transport):
    wire = run(transport(failure_schedule={"primary_router": ["500", "network"]}),
               retry_policy=policy(max_attempts=3, shared_extra_attempts_per_request=2))
    assert wire.error is None and wire.attempts["primary_router"] == 3
    assert wire.observed["retry_allowance_consumed"] == 2
    assert [a["attempt_number"] for a in attempts(wire, "primary_router")] == [1, 2, 3]
    assert_wire_matches_attempts(wire, transport)


@pytest.mark.parametrize("failure", ["401", "403", "invalid", "501", "locked", "timeout", "read_error", "write_error"])
def test_ineligible_failures_never_retried(transport, failure):
    wire = run(transport(failure=failure))
    assert wire.error is not None and wire.attempts == {"primary_router": 1}
    assert not wire.delays and wire.observed["retry_allowance_consumed"] == 0
    assert_wire_matches_attempts(wire, transport)


def test_shared_allowance_router_prevents_synthesis_retry(transport):
    wire = run(transport(failure_schedule={"primary_router": ["500"], "synthesis": ["500"]}))
    assert wire.error is not None and wire.attempts == {"primary_router": 2, "synthesis": 1}
    assert attempts(wire, "synthesis")[0]["retry_denial_reason"] == "SHARED_ALLOWANCE_EXHAUSTED"
    assert wire.observed["retry_exhausted"] and len(wire.tool_calls) == 1
    assert wire.observed["observed_usage"]["total_tokens"] == 14
    assert_wire_matches_attempts(wire, transport)


def test_shared_allowance_recovery_prevents_synthesis_retry(transport):
    wire = run(transport(recovery=True, failure_schedule={"recovery_planner": ["500"], "synthesis": ["500"]}))
    assert wire.error and wire.attempts == {"primary_router": 1, "recovery_planner": 2, "synthesis": 1}
    assert attempts(wire, "synthesis")[0]["retry_denial_reason"] == "SHARED_ALLOWANCE_EXHAUSTED"
    assert len(wire.tool_calls) == 1 and wire.observed["retry_allowance_consumed"] == 1
    assert_wire_matches_attempts(wire, transport)


@pytest.mark.parametrize("category", ["configuration", "protocol", "cancelled"])
def test_local_terminal_failures_do_not_dispatch_again(transport, monkeypatch, category):
    from agents.exceptions import UserError, MaxTurnsExceeded
    errors = {"configuration": UserError("offline"), "protocol": MaxTurnsExceeded("offline"),
              "cancelled": asyncio.CancelledError()}
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise errors[category]
    monkeypatch.setattr(model_execution.Runner, "run_sync", fail)
    wire = transport()
    if category == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            run(wire)
    else:
        run(wire)
        assert wire.error and wire.observed["retry_allowance_consumed"] == 0
        assert not attempts(wire, "primary_router")[0]["retry_eligible"]
    assert calls == [1] and not wire.attempts and not wire.tool_calls
    assert current_retry_state() is None


def test_tool_failure_cannot_trigger_model_or_request_retry(transport, monkeypatch):
    from src.agent import support_agent as support
    calls = []
    def fail(order_id):
        calls.append(order_id)
        raise ConnectionError("offline tool failure")
    monkeypatch.setattr(support.orders, "get_order_status", fail)
    wire = run(transport())
    assert wire.error and wire.attempts == {"primary_router": 1}
    assert calls == ["ORD-1001"] and wire.observed["retry_allowance_consumed"] == 0


def test_deadline_expired_during_failed_attempt_prevents_retry(transport):
    wire = transport(failure="500", late_component="primary_router")
    run(wire, budget=RequestBudget(100, clock=wire.clock))
    assert wire.error and wire.attempts == {"primary_router": 1}
    assert wire.delays == [] and wire.observed["deadline_exhausted"]
    assert attempts(wire, "primary_router")[0]["retry_denial_reason"] == "INSUFFICIENT_RETRY_BUDGET"


def test_unknown_failed_usage_is_unavailable_not_zero(transport):
    wire = run(transport(failure_schedule={"primary_router": ["500", "500"]}))
    assert wire.error and wire.observed["usage_completeness"] == "UNAVAILABLE"
    assert wire.observed["observed_usage"]["total_tokens"] is None
    assert all(not a["usage_known"] and a["returned_usage"] == {} for a in recorded_attempts(wire))
    assert all(a["usage_completeness"] == "UNAVAILABLE" for a in recorded_attempts(wire))


@pytest.mark.parametrize("component", COMPONENTS)
def test_retry_reuses_same_model_input_and_settings(transport, component):
    wire = run(transport(failure="500", failed_component=component, recovery=component == "recovery_planner"))
    assert wire.error is None
    index = {"primary_router": 0, "recovery_planner": 1, "synthesis": 1}[component]
    assert wire.bodies[index] == wire.bodies[index + 1]


def test_retry_control_survives_unavailable_telemetry(transport, monkeypatch):
    from src.agent import telemetry
    def unavailable(*args, **kwargs):
        raise RuntimeError("offline observation failure")
    monkeypatch.setattr(telemetry, "AttemptTelemetry", unavailable)
    wire = run(transport(failure="500"))
    assert wire.error is None and wire.attempts == {"primary_router": 2, "synthesis": 1}
    assert len(wire.tool_calls) == 1


@pytest.mark.parametrize("budget_ms,header,reason", [
    (39, None, "INSUFFICIENT_RETRY_BUDGET"),
    (50, "0.025", "RETRY_AFTER_EXCEEDS_BUDGET"),
    (1000, "0.2", "RETRY_AFTER_EXCEEDS_DELAY_LIMIT"),
    (1000, "invalid", "INVALID_RETRY_AFTER"),
])
def test_retry_admission_denies_without_sleep_or_dispatch(transport, budget_ms, header, reason):
    wire = transport(failure="429", guidance_headers={} if header is None else {"retry-after": header})
    budget = RequestBudget(budget_ms, clock=wire.clock)
    original_deadline = budget.deadline_monotonic
    run(wire, budget=budget)
    assert wire.error and wire.attempts == {"primary_router": 1}
    assert attempts(wire, "primary_router")[0]["retry_denial_reason"] == reason
    assert wire.delays == [] and wire.observed["retry_allowance_consumed"] == 0
    assert budget.deadline_monotonic == original_deadline


def test_retry_after_and_backoff_spend_original_budget(transport):
    wire = transport(failure="429", guidance_headers={"retry-after": "0.025"})
    budget = RequestBudget(100, clock=wire.clock)
    deadline = budget.deadline_monotonic
    run(wire, budget=budget)
    assert wire.error is None and wire.delays == [0.025]
    assert attempts(wire, "primary_router")[1]["remaining_budget_before_ms"] == pytest.approx(75)
    assert budget.deadline_monotonic == deadline and budget.remaining_ms() == pytest.approx(75)


@pytest.mark.parametrize("cancel", [False, True])
def test_budget_and_cancellation_rechecked_after_backoff(transport, cancel):
    wire = transport(failure="500")
    budget = RequestBudget(100, clock=wire.clock)
    def sleep(_):
        if cancel:
            budget.request_cancellation()
        else:
            wire.clock.now = 0.08  # Still before deadline, insufficient useful work + reserve.
    run(wire, budget=budget, retry_sleeper=sleep)
    assert wire.error and wire.attempts == {"primary_router": 1}
    expected = "CANCELLATION_REQUESTED" if cancel else "INSUFFICIENT_RETRY_BUDGET"
    assert attempts(wire, "primary_router")[0]["retry_denial_reason"] == expected
    assert wire.observed["retry_allowance_consumed"] == 0


def test_cancellation_interrupts_backoff_without_new_attempt(transport):
    wire = transport(failure="500")
    def sleep(_):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        run(wire, retry_sleeper=sleep)
    assert wire.attempts == {"primary_router": 1} and current_retry_state() is None
    observed = caught.value.production_telemetry
    assert observed.retry_allowance_consumed == 0 and observed.terminal_failure_category == "CANCELLED"


def test_expired_request_does_not_dispatch_initial_or_retry_attempt(transport):
    wire = transport(failure="500")
    run(wire, budget=RequestBudget(0, clock=wire.clock))
    assert wire.error and not wire.attempts and not recorded_attempts(wire)


def test_recovery_retry_retains_existing_downstream_reserves(transport):
    wire = transport(failure="500", failed_component="recovery_planner", recovery=True)
    run(wire, budget=RequestBudget(65, clock=wire.clock), recovery_budget_policy=RecoveryBudgetPolicy(
        recovery_allowance_ms=20, required_execution_reserve_ms=20, synthesis_reserve_ms=20))
    assert wire.error and wire.attempts == {"primary_router": 1, "recovery_planner": 1}
    assert attempts(wire, "recovery_planner")[0]["retry_denial_reason"] == "INSUFFICIENT_RETRY_BUDGET"
    assert not wire.tool_calls


@pytest.mark.parametrize("component", COMPONENTS)
@pytest.mark.parametrize("terminal", [False, True])
def test_known_failed_usage_preserved_once(transport, monkeypatch, component, terminal):
    original = model_execution.Runner.run_sync
    def with_returned_usage(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as error:
            error.run_data = SimpleNamespace(context_wrapper=SimpleNamespace(
                usage=Usage(requests=1, input_tokens=9, output_tokens=4, total_tokens=13)))
            raise
    monkeypatch.setattr(model_execution.Runner, "run_sync", with_returned_usage)
    wire = run(transport(failure_schedule={component: ["500", "500"] if terminal else ["500"]},
                         recovery=component == "recovery_planner"))
    affected = attempts(wire, component)
    assert affected[0]["returned_usage"]["total_tokens"] == 13
    assert wire.observed["usage_completeness"] == "COMPLETE"
    assert wire.observed["observed_usage"]["total_tokens"] == sum(a["returned_usage"]["total_tokens"] for a in recorded_attempts(wire))
    if terminal:
        assert wire.error is not None and affected[-1]["returned_usage"]["total_tokens"] == 13
    else:
        assert wire.error is None
        assert wire.result.context_wrapper.usage.total_tokens == (66 if wire.recovery else 44) + 13
        assert wire.result.context_wrapper.usage.total_tokens == wire.observed["observed_usage"]["total_tokens"]


def test_no_failure_behavior_identical_with_explicit_policy(transport):
    baseline = transport().run()
    enabled = run(transport())
    assert normalized_bodies(enabled) == normalized_bodies(baseline)
    assert enabled.tool_calls == baseline.tool_calls
    assert enabled.result.final_output == baseline.result.final_output
    assert enabled.result.context_wrapper.usage.total_tokens == baseline.result.context_wrapper.usage.total_tokens
    assert enabled.observed["usage_completeness"] == "COMPLETE"
    assert enabled.observed["retry_allowance_consumed"] == 0
    assert not baseline.observed["retry_policy_enabled"]


def test_concurrent_requests_have_separate_shared_allowances(transport):
    barrier = Barrier(2)
    shared = policy()
    wires = [transport(failure="500") for _ in range(2)]
    def qualify(wire):
        def sleep(seconds):
            barrier.wait(timeout=10)
            wire.clock.now += seconds
        return run(wire, retry_policy=shared, retry_sleeper=sleep)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(qualify, wires))
    assert all(w.error is None and w.attempts["primary_router"] == 2 for w in results)
    assert all(w.observed["retry_allowance_consumed"] == 1 for w in results)
    assert results[0].observed["request_id"] != results[1].observed["request_id"]
    assert current_retry_state() is None
    assert_wire_matches_attempts(results[0], transport)
    assert_wire_matches_attempts(results[1], transport)
