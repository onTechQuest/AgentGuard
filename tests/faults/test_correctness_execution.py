"""Correctness ceilings through real orchestration with fake time/model/tool I/O."""
import pytest

from src.agent import runtime_reliability as runtime, telemetry
from src.agent.request_budget import RequestBudget, RequestDeadlineExceeded
from src.agentguard import evaluation_record as er, lineage as l
from src.agentguard.correctness_execution import load_correctness_policy, production_slo_observation
from src.agentguard.scoring import evaluate_record
from .harness import PROMPT, Stage, Injection, Fault
from .test_deadline_execution import Clock, TimedHarness, attempts


SCENARIO = dict(id="correctness", input=PROMPT, expected_output="processing", expected_contains=["processing"],
    forbidden_contains=[], expected_tools=[dict(name="get_order_status", arguments=dict(order_id="ORD-1001"))])


@pytest.fixture
def execute(faults, monkeypatch, tmp_path):
    token = l._active.set(None)
    def invoke(*, suite="smoke", synthesis=18000, router=1000, recovery=False, injection=None):
        clock = Clock()
        monkeypatch.setattr(runtime, "RequestBudget", lambda ms: RequestBudget(ms, clock=clock))
        monkeypatch.setattr(telemetry.time, "perf_counter", clock)
        policy = load_correctness_policy() if suite in {"smoke", "full"} else None
        run = l.start_run(tmp_path, suite=suite, functional=[SCENARIO], execution_mode="offline_fixture",
            effective_runtime_policy=policy.snapshot() if policy else None,
            execution_profile=policy.profile_identity() if policy else None)
        if policy:
            run.execution_policy = policy
        harness = TimedHarness(clock, recovery=recovery, injection=injection,
            durations={Stage.ROUTER: router, Stage.RECOVERY: 1000, Stage.SYNTHESIS: synthesis})
        observed = harness.run(lambda: er.execute_scenario(SCENARIO))
        run.finish(state="INCOMPLETE" if observed.caught_error or observed.response.execution_error else "COMPLETED")
        return observed, l.load_run(run.path)
    yield invoke
    l._active.reset(token)


@pytest.mark.parametrize("suite", ["smoke", "full"])
def test_correctness_accepts_slow_synthesis_and_retains_slo(execute, suite):
    observed, run = execute(suite=suite)
    assert observed.caught_error is None
    assert evaluate_record(SCENARIO, observed.response).overall_pass
    assert run["completion_state"] == "COMPLETED"
    profile = run["manifest"]["protocol"]["execution_profile"]
    assert profile["request_deadline_ms"] == 30000 and profile["synthesis_allowance_ms"] == 25000
    assert not profile["retries_enabled"]
    evidence = run["results"]["aggregate_results"]["production_slo_observations"][0]
    assert evidence["production_synthesis_allowance_ms"] == 6000
    assert evidence["observed_synthesis_latency_ms"] == 18000
    assert evidence["production_synthesis_allowance_exceeded"] is True
    assert evidence["production_request_deadline_exceeded"] is False
    assert evidence["request_id"] == run["results"]["observations"][0]["operational"]["request_id"]
    assert observed.calls[Stage.ROUTER] == observed.calls[Stage.SYNTHESIS] == 1
    assert len(observed.tool_calls) == 1
    assert observed.telemetry["retry_attempts_total"] == 0
    assert all(a["attempt_number"] == 1 and not a["retry_performed"] for a in attempts(observed))


@pytest.mark.parametrize("suite", ["performance", "reliability"])
def test_other_suites_still_default_to_production(execute, suite):
    observed, run = execute(suite=suite, synthesis=6001)
    assert isinstance(observed.caught_error, RequestDeadlineExceeded)
    assert run["completion_state"] == "INCOMPLETE"
    assert "execution_profile" not in run["manifest"]["protocol"]
    assert observed.telemetry["effective_runtime_policy"]["name"] == "production_v1"
    assert observed.telemetry["effective_runtime_policy"]["synthesis_allowance_ms"] == 6000
    assert observed.telemetry["deadline_budget_ms"] == 20000


@pytest.mark.parametrize("router,synthesis", [(1000, 25000), (1000, 25001), (12000, 18001)])
def test_evaluation_ceilings_remain_real(execute, router, synthesis):
    observed, run = execute(router=router, synthesis=synthesis)
    assert isinstance(observed.caught_error, RequestDeadlineExceeded)
    assert run["completion_state"] == "INCOMPLETE"
    failure = run["results"]["executions"][0]["failure_evidence"]
    assert failure["exception_type"] == "RequestDeadlineExceeded"
    assert failure["request_deadline_ms"] == 30000
    assert run["results"]["observations"][0]["failure_evidence_reference"]
    assert observed.calls[Stage.SYNTHESIS] == 1


def test_larger_synthesis_cap_does_not_inflate_recovery_reservation(execute):
    observed, run = execute(router=6000, synthesis=18000, recovery=True)
    assert observed.caught_error is None
    assert observed.telemetry["recovery_admission"]["required_downstream_reserve_ms"] == 6000
    assert observed.calls[Stage.RECOVERY] == 1 and run["completion_state"] == "COMPLETED"


@pytest.mark.parametrize("stage", [Stage.ROUTER, Stage.TOOL, Stage.SYNTHESIS])
def test_non_latency_failures_remain_failures(execute, stage):
    observed, run = execute(injection=Injection(stage, Fault.TIMEOUT))
    assert observed.caught_error is not None or observed.response.execution_error is not None
    assert run["completion_state"] == "INCOMPLETE"
    assert observed.telemetry["retry_attempts_total"] == 0


def test_missing_slo_measurements_are_unknown():
    evidence = production_slo_observation("s", None)
    assert evidence["observed_synthesis_latency_ms"] is None
    assert evidence["production_synthesis_allowance_exceeded"] is None


def test_fast_correctness_result_has_no_production_synthesis_violation(execute):
    observed, run = execute(synthesis=2000)
    assert observed.caught_error is None
    evidence = run["results"]["aggregate_results"]["production_slo_observations"][0]
    assert evidence["observed_synthesis_latency_ms"] == 2000
    assert evidence["production_synthesis_allowance_exceeded"] is False


def test_production_configuration_unchanged():
    policy = runtime.default_runtime_policy()
    assert (policy.request_deadline_ms, policy.synthesis_allowance_ms) == (20000, 6000)
    assert not policy.model_retry_policy.enabled and policy.model_retry_policy.max_attempts == 1
