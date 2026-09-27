"""Promoted production defaults through real orchestration with fake I/O/time."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.agent import runtime_reliability as runtime, support_agent as support, telemetry
from src.agent.request_budget import RequestBudget, RecoveryBudgetPolicy
from src.agent.retry_policy import ModelRetryPolicy
from .harness import PROMPT, Stage
from .test_deadline_execution import Clock, TimedHarness, assert_deadline, attempts, span


def run_default(harness, budget=None, **options):
    return harness.run(lambda: support.run_support_agent_detailed(
        PROMPT, request_budget=budget or RequestBudget(clock=harness.clock), **options))


def test_normal_caller_receives_v1_without_supplying_policy_or_budget(faults, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(runtime, "RequestBudget", lambda ms: RequestBudget(ms, clock=clock))
    observed = TimedHarness(clock).run(lambda: support.run_support_agent_detailed(PROMPT))
    assert observed.caught_error is None
    data = observed.telemetry
    policy = data["effective_runtime_policy"]
    assert data["deadline_budget_ms"] == policy["request_deadline_ms"] == 20000
    assert policy["name"] == "production_v1"
    assert policy["router_allowance_ms"] == 13000
    assert policy["recovery_reserve_ms"] == 3000
    assert policy["synthesis_allowance_ms"] == 6000
    assert policy["model_retry_policy"]["enabled"] is False
    assert policy["model_retry_policy"]["max_attempts"] == 1
    assert policy["model_retry_policy"]["shared_extra_attempts_per_request"] == 0
    assert policy["lower_layer_retry_policy"] == {
        "agents_sdk_max_retries": 0, "openai_client_max_retries": 0, "scope": "default_production_sdk_path"}
    assert span(observed, "primary_router")["allocated_allowance_ms"] == 13000
    assert span(observed, "synthesis")["allocated_allowance_ms"] == 6000
    assert len(observed.tool_calls) == 1
    assert all(a["attempt_number"] == 1 and not a["retry_performed"] for a in attempts(observed))


@pytest.mark.parametrize("milliseconds", [None, 100000, 20000])
def test_unbounded_or_larger_budget_cannot_bypass_default_router_cap(faults, milliseconds):
    clock = Clock()
    observed = run_default(TimedHarness(clock, durations={Stage.ROUTER: 13001}),
                           RequestBudget(milliseconds, clock=clock))
    assert_deadline(observed, "primary_router")
    assert not observed.tool_calls and not observed.synthesis_inputs
    assert observed.calls == {Stage.ROUTER: 1}
    assert not observed.telemetry["deadline_exhausted"]
    assert observed.telemetry["observed_usage"]["total_tokens"] == 14


def test_default_can_finish_observed_long_success_without_static_22s_reservation(faults):
    observed = run_default(TimedHarness(Clock(), durations={Stage.ROUTER: 12193, Stage.SYNTHESIS: 3133}))
    assert observed.caught_error is None
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.SYNTHESIS] == 1


def test_explicit_unbounded_policy_is_required_for_unlimited_execution(faults):
    observed = run_default(TimedHarness(Clock(), durations={Stage.ROUTER: 22000, Stage.SYNTHESIS: 7000}),
                           runtime_reliability_policy=runtime.RuntimeReliabilityPolicy.unbounded())
    assert observed.caught_error is None
    assert observed.telemetry["deadline_budget_ms"] is None
    assert observed.telemetry["effective_runtime_policy"]["name"] == "explicit_unbounded"
    assert not observed.telemetry["effective_runtime_policy"]["model_retry_policy"]["enabled"]


def test_none_policy_does_not_disable_production_default(faults):
    observed = run_default(TimedHarness(Clock(), durations={Stage.ROUTER: 13001}), runtime_reliability_policy=None)
    assert_deadline(observed, "primary_router")


def test_expired_before_router_preserves_effective_policy_and_dispatches_nothing(faults):
    clock = Clock()
    budget = RequestBudget(clock=clock)
    clock.advance(20001)
    observed = run_default(TimedHarness(clock), budget)
    assert_deadline(observed, "primary_router")
    assert not observed.calls and not observed.tool_calls
    assert observed.policy is None
    assert observed.telemetry["effective_runtime_policy"]["name"] == "production_v1"
    assert observed.telemetry["deadline_budget_ms"] == 20000


def test_shorter_caller_deadline_keeps_original_start(faults):
    clock = Clock()
    budget = RequestBudget(1000, clock=clock)
    clock.advance(999)
    observed = run_default(TimedHarness(clock, durations={Stage.ROUTER: 1}), budget)
    assert_deadline(observed, "primary_router")
    assert observed.telemetry["deadline_budget_ms"] == 1000
    assert observed.telemetry["effective_runtime_policy"]["request_deadline_ms"] == 20000


def test_default_recovery_denied_before_factory_after_slow_router(faults, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recovery factory called despite insufficient reserve")
    monkeypatch.setattr(support, "SemanticRecoveryPlanner", forbidden)
    observed = run_default(TimedHarness(Clock(), recovery=True, durations={Stage.ROUTER: 12000}))
    assert_deadline(observed, "recovery_planner")
    assert observed.calls == {Stage.ROUTER: 1} and not observed.tool_calls
    assert observed.telemetry["recovery_admission"] == {
        "remaining_budget_ms": 8000, "required_downstream_reserve_ms": 6000,
        "minimum_work_ms": 3000, "allowance_ms": 2000, "admitted": False,
        "denial_reason": "INSUFFICIENT_ALLOWANCE"}


def test_default_early_recovery_is_distinct_from_retry(faults):
    observed = run_default(TimedHarness(Clock(), recovery=True, durations={
        Stage.ROUTER: 1716, Stage.RECOVERY: 1573, Stage.SYNTHESIS: 1069}))
    assert observed.caught_error is None
    assert observed.telemetry["recovery_admission"]["admitted"]
    assert observed.telemetry["recovery_admission"]["minimum_work_ms"] == 3000
    assert len(attempts(observed)) == len({a["logical_call_id"] for a in attempts(observed)}) == 3
    assert observed.telemetry["retry_attempts_total"] == 0
    assert len(observed.tool_calls) == 1


def test_default_recovery_reserve_is_not_a_new_3s_timeout(faults):
    observed = run_default(TimedHarness(Clock(), recovery=True, durations={
        Stage.ROUTER: 1000, Stage.RECOVERY: 4000, Stage.SYNTHESIS: 1000}))
    assert observed.caught_error is None
    assert observed.telemetry["recovery_admission"]["admitted"]


@pytest.mark.parametrize("duration", [6000, 6001])
def test_default_late_synthesis_preserves_tools_and_usage(faults, duration):
    observed = run_default(TimedHarness(Clock(), durations={Stage.SYNTHESIS: duration}))
    assert_deadline(observed, "synthesis")
    assert observed.telemetry["result_abandoned"]
    assert observed.telemetry["observed_usage"]["total_tokens"] == 44
    assert observed.telemetry["usage_completeness"] == "COMPLETE"
    assert len(observed.tool_calls) == 1
    assert observed.trace.executions[0].status == "completed"
    assert attempts(observed)[-1]["late_completion"]
    assert not observed.telemetry["cancellation_observed"]


def test_tool_completion_survives_default_request_expiry(faults):
    observed = run_default(TimedHarness(Clock(), durations={Stage.TOOL: 20001}))
    assert_deadline(observed, "tool")
    assert len(observed.tool_calls) == 1 and not observed.synthesis_inputs
    assert observed.trace.executions[0].status == "completed"
    assert not observed.telemetry["required_operation_summary"]["incomplete"]


def test_effective_default_enforcement_survives_telemetry_failure(faults, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Observer unavailable")
    monkeypatch.setattr(telemetry, "ProductionExecutionTelemetry", broken)
    observed = run_default(TimedHarness(Clock(), durations={Stage.ROUTER: 13001}))
    assert observed.caught_error.component == "primary_router"
    assert observed.calls == {Stage.ROUTER: 1}
    assert not observed.tool_calls


def test_production_modules_do_not_import_qualification_or_evaluation():
    for path in (Path(__file__).resolve().parents[2] / "src/agent").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            # Existing shared business catalog is runtime policy, despite its
            # historical package location; this promotion does not relocate it.
            assert not any("qualification" in name or name.startswith(("src.agentguard", "evals", "deepeval"))
                           for name in names if name != "src.agentguard.tool_policy"), path


@pytest.mark.parametrize("field", runtime.BUDGET_FIELDS)
def test_partial_policy_does_not_silently_become_unbounded(field):
    with pytest.raises(ValueError):
        replace(runtime.default_runtime_policy(), **{field: None})


def test_loose_retry_override_cannot_enable_production_retries(faults):
    observed = run_default(TimedHarness(Clock()), retry_policy=ModelRetryPolicy(max_attempts=2))
    assert isinstance(observed.caught_error, ValueError) and not observed.calls


def test_loose_recovery_override_cannot_remove_production_reserves(faults):
    observed = run_default(TimedHarness(Clock()), recovery_budget_policy=RecoveryBudgetPolicy())
    assert isinstance(observed.caught_error, ValueError) and not observed.calls


@pytest.mark.parametrize("invalid", ["missing", "malformed", "unbounded", "retry_allowance"])
def test_invalid_checked_in_configuration_fails_closed(faults, monkeypatch, tmp_path, invalid):
    path = tmp_path / "runtime.json"
    raw = json.loads(runtime.POLICY_PATH.read_text())
    if invalid == "unbounded":
        raw.update({field: None for field in runtime.BUDGET_FIELDS})
    if invalid == "retry_allowance":
        raw["model_retry_policy"]["shared_extra_attempts_per_request"] = 1
    if invalid != "missing":
        path.write_text("{" if invalid == "malformed" else json.dumps(raw))
    monkeypatch.setattr(runtime, "POLICY_PATH", path)
    runtime.default_runtime_policy.cache_clear()
    try:
        observed = run_default(TimedHarness(Clock()))
        assert observed.caught_error is not None and not observed.calls
    finally:
        runtime.default_runtime_policy.cache_clear()


def test_concurrent_default_and_unbounded_request_scopes_are_independent(faults):
    def execute(explicit):
        options = {"runtime_reliability_policy": runtime.RuntimeReliabilityPolicy.unbounded()} if explicit else {}
        return run_default(TimedHarness(Clock(), durations={Stage.ROUTER: 14000}), **options)
    with ThreadPoolExecutor(2) as pool:
        bounded, unbounded = list(pool.map(execute, [False, True]))
    assert_deadline(bounded, "primary_router")
    assert unbounded.caught_error is None
    assert bounded.telemetry["request_id"] != unbounded.telemetry["request_id"]
