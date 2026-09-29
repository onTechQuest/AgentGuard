"""Offline contracts for incomplete evaluation retention; no provider calls."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2 as httpx
import jsonschema
from openai import APIStatusError, RateLimitError
import pytest

from src.agent import telemetry as t
from src.agent.request_budget import RequestDeadlineExceeded, AdmissionEvidence, AdmissionDenial
from src.agentguard import evaluation_record as er, lineage as l
from src.agentguard.failure_evidence import retain_failure
from src.agentguard.lineage_adapters import build_manifest

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = dict(id="return_002", input="Synthetic input", tier="smoke", expected_output="Expected")


@pytest.fixture
def run(tmp_path, request):
    planned = [SCENARIO, dict(SCENARIO, id="completed")] if getattr(request, "param", False) else [SCENARIO]
    manifest, rows = build_manifest(ROOT, suite="smoke", functional=planned, safety=[])
    run = l.RunArtifacts(tmp_path, manifest, rows)
    token = l._active.set(run)
    yield run
    l._active.reset(token)


def deadline():
    return RequestDeadlineExceeded("synthesis", AdmissionEvidence(
        0, admitted=False, denial_reason=AdmissionDenial.DEADLINE_EXHAUSTED))


def observed():
    return t.ProductionExecutionTelemetry(
        request_id="a" * 32, total_latency_ms=1234, terminal_status="failed",
        terminal_failure_component="synthesis", terminal_failure_category=t.FailureCategory.DEADLINE_EXHAUSTED,
        deadline_budget_ms=1200, deadline_exhausted=True, result_abandoned=True,
        remote_outcome_unknown=True, retry_policy_enabled=True, retry_attempts_total=2,
        usage_completeness=t.UsageCompleteness.PARTIAL, observed_usage={"input_tokens":12,"output_tokens":3,"total_tokens":15},
        component_spans=[t.ComponentSpan(name, 0, duration_ms=duration, lower_layer_retries_configured=False,
                         resolved_model="gpt-4.1" if name != "required_execution" else None,
                         late_completion=name == "synthesis")
                         for name, duration in [("primary_router",100),("recovery_planner",200),("synthesis",900),("required_execution",34)]])


def evidence(run):
    return run.executed[-1]["failure_evidence"]


@pytest.mark.parametrize("with_telemetry", [True, False])
def test_deadline_retained_and_reraised_once(run, monkeypatch, with_telemetry):
    error = deadline()
    if with_telemetry:
        error.production_telemetry = observed()
    call = Mock(side_effect=error)
    monkeypatch.setattr(er, "run_support_agent_detailed", call)
    before = (run.path / "manifest.json").read_bytes()
    with pytest.raises(RequestDeadlineExceeded) as caught:
        er.execute_scenario(SCENARIO)
    assert caught.value is error
    call.assert_called_once_with(SCENARIO["input"])
    assert len(run.executed) == 1
    e = evidence(run)
    assert e["exception_type"] == "RequestDeadlineExceeded"
    assert e["failure_category"] == "DEADLINE_EXHAUSTED"
    assert e["telemetry_available"] is with_telemetry
    assert e["run_id"] == run.manifest.run_id and e["manifest_digest"] == run.manifest.digest
    if with_telemetry:
        assert e["request_id"] == "a" * 32
        assert e["total_latency_ms"] == 1234
        assert e["request_deadline_ms"] == 1200
        assert e["deadline_exhaustion_stage"] == "synthesis"
        assert e["late_completion"] is True
        assert e["result_abandoned"] is True and e["remote_outcome_unknown"] is True
        assert e["component_latency_ms"] == dict(primary_router=100,recovery_planner=200,synthesis=900,required_operations=34)
        assert e["retry_policy_enabled"] is True and e["retry_attempts_total"] == 2
        assert e["lower_layer_retries_configured"] is False
        assert e["production_tokens"]["total_tokens"] == 15
        assert e["usage_completeness"] == "PARTIAL"
        assert len(e["observed_model_identities"]) == 3
    else:
        assert e["request_id"] is None and e["total_latency_ms"] is None
        assert e["retry_attempts_total"] is None and e["retry_policy_enabled"] is None
        assert e["deadline_exhausted"] is None and e["late_completion"] is None
        assert set(e["production_tokens"].values()) == {None}
        assert e["usage_completeness"] == "UNAVAILABLE"
        assert e["observed_model_identities"] == []
    run.finish(state="INCOMPLETE")
    loaded = l.load_run(run.path)
    assert loaded["completion_state"] == "INCOMPLETE"
    assert loaded["results"]["completed_scenario_count"] == 0
    assert (run.path / "manifest.json").read_bytes() == before
    jsonschema.validate(loaded["results"], json.loads((ROOT / "config/evaluation-result.schema.json").read_text()))


@pytest.mark.parametrize("kind,category", [("provider","PROVIDER_ERROR"),("rate","RATE_LIMIT"),
    ("tool","TOOL_ERROR"),("generic","UNKNOWN_INTERNAL_FAILURE"),("cancel","CANCELLED")])
def test_generic_failures(run, monkeypatch, kind, category):
    response = httpx.Response(500, request=httpx.Request("GET", "https://example.invalid"))
    error = {"provider":APIStatusError("private",response=response,body=None),
             "rate":RateLimitError("private",response=response,body=None),
             "tool":RuntimeError("private"), "generic":ValueError("private"),
             "cancel":asyncio.CancelledError("private")}[kind]
    if kind == "tool":
        error.production_telemetry = t.ProductionExecutionTelemetry(
            terminal_status="failed", terminal_failure_component="tool", terminal_failure_category=t.FailureCategory.TOOL_ERROR)
    monkeypatch.setattr(er,"run_support_agent_detailed",Mock(side_effect=error))
    with pytest.raises(type(error)):
        er.execute_scenario(SCENARIO)
    assert evidence(run)["failure_category"] == category
    assert "private" not in json.dumps(evidence(run))


@pytest.mark.parametrize("run", [True], indirect=True)
def test_mixed_run_completed_unchanged(run, monkeypatch):
    result = SimpleNamespace(final_output="ok",new_items=[],context_wrapper=SimpleNamespace(usage=SimpleNamespace()))
    call = Mock(side_effect=[result,ValueError("private")])
    monkeypatch.setattr(er,"run_support_agent_detailed",call)
    er.execute_scenario(dict(SCENARIO,id="completed"))
    with pytest.raises(ValueError):
        er.execute_scenario(SCENARIO)
    assert run.executed[0] == dict(scenario_id="completed",completed=True)
    run.finish(state="INCOMPLETE")
    results = l.load_run(run.path)["results"]
    assert results["executed_scenario_count"] == 2 and results["completed_scenario_count"] == 1
    assert call.call_count == 2
    schema = json.loads((ROOT / "config/evaluation-result.schema.json").read_text())
    jsonschema.validate(results,schema)
    results.update(executions=[run.executed[0]],executed_scenario_count=1,completion_state="COMPLETED")
    jsonschema.validate(results,schema)


def test_privacy_and_unknowns(run):
    error = ValueError("Bearer private-secret and customer text")
    error.production_telemetry = dict(request_id="sk-abcdefghijklmnopqrstuv",headers={"Authorization":"secretvalue"},
        total_latency_ms=float("nan"),retry_attempts_total=-1,retry_policy_enabled="false",
        observed_usage={"total_tokens":False},component_spans=[dict(component="synthesis",resolved_model="secretvalue")])
    retain_failure(SCENARIO,error)
    e = evidence(run)
    assert e["request_id"] is None and e["observed_model_identities"] == []
    assert e["total_latency_ms"] is None and e["retry_attempts_total"] is None and e["retry_policy_enabled"] is None
    assert e["production_tokens"]["total_tokens"] is None
    for secret in ("headers","Authorization","secretvalue","private-secret","customer text","sk-"):
        assert secret not in json.dumps(e)


def test_cause_snapshot_and_idempotent_outer_capture(run):
    cause = deadline()
    cause.production_telemetry = observed()
    error = RuntimeError("wrapper")
    error.__cause__ = cause
    retain_failure(SCENARIO,error)
    original = json.dumps(evidence(run))
    cause.production_telemetry.total_latency_ms = 999
    retain_failure(SCENARIO,error)
    assert json.dumps(evidence(run)) == original
    assert len(run.executed) == 1


def test_scoring_exception_retains_production_snapshot(run):
    run.observe(SCENARIO["id"])
    raw = observed().snapshot()
    raw.update(terminal_status="completed",terminal_failure_category=None,terminal_failure_component=None)
    retain_failure(SCENARIO,ValueError("private"),record=SimpleNamespace(production_telemetry=raw),stage="semantic evaluation")
    assert evidence(run)["evaluation_stage"] == "semantic evaluation"
    assert evidence(run)["terminal_status"] == "completed"
    assert run.executed[-1]["completed"] is False


@pytest.mark.parametrize("policy", [False, True])
def test_converted_execution_failure_retains_evidence(run, monkeypatch, policy):
    from src.agent.execution_plan import ExecutionTrace, ExecutionPlan, ExecutionFailure
    trace = ExecutionTrace(ExecutionPlan((), (), ()))
    if policy:
        trace.prohibited_attempts.append({"name":"private-tool"})
    error = ExecutionFailure(trace, "Runtime failure")
    error.production_telemetry = observed()
    error.production_telemetry.terminal_failure_category = None
    error.production_telemetry.terminal_failure_component = "required_execution"
    monkeypatch.setattr(er,"run_support_agent_detailed",Mock(side_effect=error))
    record = er.execute_scenario(SCENARIO)
    assert record.execution_error == "Runtime failure"
    assert evidence(run)["exception_type"] == "ExecutionFailure"
    assert evidence(run)["failure_category"] == ("PROHIBITED_OPERATION_ATTEMPT" if policy else "REQUIRED_OPERATION_INCOMPLETE")
    assert evidence(run)["request_id"] == record.production_telemetry["request_id"]


def test_lineage_entry_closes_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(er,"run_support_agent_detailed",Mock(side_effect=asyncio.CancelledError()))
    @l.lineage_entry
    def evaluate():
        l.start_run(tmp_path,suite="smoke",functional=[SCENARIO],execution_mode="offline_fixture")
        er.execute_scenario(SCENARIO)
    with pytest.raises(asyncio.CancelledError):
        evaluate()
    directory = next((tmp_path / "reports/evaluations").iterdir())
    loaded = l.load_run(directory)
    assert loaded["completion_state"] == "INCOMPLETE"
    assert loaded["results"]["executions"][0]["failure_evidence"]["exception_type"] == "CancelledError"


def test_unreadable_telemetry_does_not_replace_exception(run, monkeypatch):
    class BrokenTelemetryError(RuntimeError):
        @property
        def production_telemetry(self):
            raise ValueError("unreadable")
    error = BrokenTelemetryError("private")
    monkeypatch.setattr(er,"run_support_agent_detailed",Mock(side_effect=error))
    with pytest.raises(BrokenTelemetryError) as caught:
        er.execute_scenario(SCENARIO)
    assert caught.value is error
    assert evidence(run)["telemetry_available"] is False
    assert evidence(run)["exception_type"] == "BrokenTelemetryError"


def test_record_usage_without_request_telemetry(run):
    record = SimpleNamespace(production_telemetry=None, latency_ms=250, input_tokens=10, output_tokens=0, total_tokens=10)
    retain_failure(SCENARIO,ValueError("private"),record=record)
    e = evidence(run)
    assert e["telemetry_available"] is False
    assert e["total_latency_ms"] == 250
    assert e["production_tokens"] == dict(input_tokens=10,output_tokens=0,total_tokens=10)
    assert e["usage_completeness"] == "UNKNOWN"


def test_wrapper_and_terminal_exception_types_retained(run):
    error = RuntimeError("wrapper")
    error.production_telemetry = observed()
    error.production_telemetry.terminal_exception_type = "APITimeoutError"
    retain_failure(SCENARIO,error)
    assert evidence(run)["exception_type"] == "RuntimeError"
    assert evidence(run)["terminal_exception_type"] == "APITimeoutError"


@pytest.mark.parametrize("component", ["primary_router", "recovery_planner", "synthesis"])
def test_stage_cap_evidence_available_without_overall_exhaustion(run, component):
    from src.agentguard.synthesis_qualification import normalize
    error = deadline()
    error.production_telemetry = observed()
    raw = error.production_telemetry
    raw.deadline_budget_ms = 20000
    raw.deadline_exhausted = False
    raw.total_latency_ms = 14000
    raw.terminal_failure_component = component
    span = next(s for s in raw.component_spans if s.component == component)
    span.duration_ms = 11438.89
    span.remaining_budget_before_ms = 17778
    span.configured_stage_cap_ms = 6000
    span.allocated_allowance_ms = 6000
    span.stage_admitted = True
    span.result_accepted = False
    span.late_completion = True
    span.result_abandoned = True
    retain_failure(SCENARIO,error)
    e = evidence(run)
    assert e["deadline_exhausted"] is False
    assert e["deadline_exhaustion_stage"] == component
    budget = next(s for s in e["stage_budgets"] if s["component"] == component)
    assert budget["configured_stage_cap_ms"] == budget["allocated_allowance_ms"] == 6000
    assert budget["remaining_budget_before_ms"] == 17778
    assert budget["result_accepted"] is False and budget["stage_admitted"] is True
    assert normalize(run.executed[-1])["classification"] == "STAGE_ALLOWANCE_EXCEEDED"
    run.finish(state="INCOMPLETE")
    jsonschema.validate(l.load_run(run.path)["results"],json.loads((ROOT / "config/evaluation-result.schema.json").read_text()))

