"""Offline verification of diagnostic observation, without any model calls."""

import json
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from agents import Agent
from agents.usage import Usage

from scripts import audit_token_usage as audit
from src.agentguard.evaluation_record import EvaluationRecord
from src.agent.execution_plan import ExecutionTrace, build_execution_plan, execute_required, ExecutionFailure
from src.agent.request_policy import RequestToolPolicy, ToolGrant


def test_observer_copies_usage_before_merge_and_forwards_requests(monkeypatch):
    router = Agent(name="Capability Router", instructions="route")
    agent = Agent(name="AgentGuard Support Agent", instructions="answer")

    def result(usage):
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=usage),
                               raw_responses=[SimpleNamespace(usage=Usage(**audit.usage_snapshot(usage)))])

    routed = result(Usage(requests=1, input_tokens=100, output_tokens=20, total_tokens=120))
    answered = result(Usage(requests=2, input_tokens=500, output_tokens=50, total_tokens=550))
    run = Mock(side_effect=[routed, answered])
    monkeypatch.setattr(audit.Runner, "run_sync", run)

    def execute(scenario):
        first = audit.Runner.run_sync(router, "routing envelope", max_turns=1)
        second = audit.Runner.run_sync(agent, scenario["input"])
        second.context_wrapper.usage.add(first.context_wrapper.usage)
        usage = second.context_wrapper.usage
        return EvaluationRecord(scenario["id"], scenario["input"], "answer", [], 10,
                                usage.requests, usage.input_tokens, usage.output_tokens, usage.total_tokens)

    execute_mock = Mock(side_effect=execute)
    monkeypatch.setattr(audit, "execute_scenario", execute_mock)
    scenario = {"id": "demo", "input": "question"}
    row = audit.profile_scenario(scenario)
    execute_mock.assert_called_once_with(scenario)
    assert run.call_count == 2
    assert run.call_args_list[0].args == (router, "routing envelope")
    assert run.call_args_list[0].kwargs == {"max_turns": 1}
    assert run.call_args_list[1].args == (agent, "question")
    assert [component["total_tokens"] for component in row["components"]] == [120, 550]
    assert row["production_usage"]["total_tokens"] == 670
    assert row["production_usage"]["requests"] == 3
    assert row["usage_reconciled"] is True
    assert row["evaluation_record"]["final_output"] == "answer"
    assert row["evaluation_record"]["total_tokens"] == 670
    assert row["http_retry_count"] is None
    assert row["retry_tokens"] is None
    assert row["evaluation_usage"] == {"model_calls": 0, "total_tokens": 0}
    assert audit.Runner.run_sync is run
    # Real SDK usage contains nested Pydantic models, not just dataclasses.
    restored = json.loads(json.dumps(row))
    assert restored["components"][0]["response_usage"][0]["input_tokens_details"]["cached_tokens"] == 0


def test_recovery_is_reported_as_a_separate_production_component():
    agent = Agent(name="Planning Completeness Reviewer", instructions="review", tools=[])
    usage = Usage(requests=1, input_tokens=90, output_tokens=10, total_tokens=100)
    response = SimpleNamespace(context_wrapper=SimpleNamespace(usage=usage),
                               raw_responses=[SimpleNamespace(usage=usage)])
    component = audit.component_snapshot(agent, response, 200)
    assert component["component"] == "planning_recovery"
    assert component["requests"] == 1 and component["total_tokens"] == 100
    assert component["context_characters"]["tool_definitions"] == 0


def test_main_profiles_each_scenario_once_and_sorts_report(monkeypatch, tmp_path):
    scenarios = [{"id": "small"}, {"id": "large"}]
    monkeypatch.setattr(audit, "load_dataset", Mock(return_value=scenarios))
    profile = Mock(side_effect=[
        {"scenario_id": "small", "production_usage": {"total_tokens": 10, "requests": 1}},
        {"scenario_id": "large", "production_usage": {"total_tokens": 30, "requests": 2}},
    ])
    monkeypatch.setattr(audit, "profile_scenario", profile)
    output = tmp_path / "report.json"
    assert audit.main(["--output", str(output)]) == 0
    assert [call.args[0] for call in profile.call_args_list] == scenarios
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["average_tokens_per_run"] == 20
    assert [row["scenario_id"] for row in report["scenarios"]] == ["large", "small"]


def test_failure_stops_without_retry_and_omits_exception_text(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "load_dataset", Mock(return_value=[{"id": "first"}, {"id": "second"}]))
    profile = Mock(side_effect=RuntimeError("sensitive exception details"))
    monkeypatch.setattr(audit, "profile_scenario", profile)
    output = tmp_path / "report.json"
    assert audit.main(["--output", str(output)]) == 1
    profile.assert_called_once_with({"id": "first"})
    text = output.read_text(encoding="utf-8")
    assert "sensitive" not in text
    assert json.loads(text)["error"] == {"scenario_id": "first", "type": "RuntimeError"}


def test_resume_preserves_successes_and_excludes_requested_scenario(monkeypatch, tmp_path):
    scenarios = [{"id": "done"}, {"id": "lost"}, {"id": "pending"}]
    monkeypatch.setattr(audit, "load_dataset", Mock(return_value=scenarios))
    output = tmp_path / "report.json"
    output.write_text(json.dumps({"scenarios": [
        {"scenario_id": "done", "production_usage": {"total_tokens": 10, "requests": 1}}
    ]}), encoding="utf-8")
    profile = Mock(return_value={"scenario_id": "pending", "production_usage": {"total_tokens": 20, "requests": 1}})
    monkeypatch.setattr(audit, "profile_scenario", profile)
    assert audit.main(["--output", str(output), "--resume", "--exclude-scenario", "lost"]) == 0
    profile.assert_called_once_with(scenarios[2])
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False


@pytest.mark.parametrize("failed", [False, True])
def test_direct_runtime_tool_latency_is_preserved_and_failure_not_qualified(monkeypatch, failed):
    agent = Agent(name="AgentGuard Support Agent", instructions="answer", tools=[])
    usage = Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)
    monkeypatch.setattr(audit.Runner, "run_sync", Mock(return_value=SimpleNamespace(
        context_wrapper=SimpleNamespace(usage=usage), raw_responses=[SimpleNamespace(usage=usage)])))

    def execute(scenario):
        trace = ExecutionTrace(build_execution_plan(RequestToolPolicy((
            ToolGrant("get_order_status", "ORD-1001", ("order_status",)),
        ), False)))
        tool = Mock(side_effect=RuntimeError("private")) if failed else Mock(return_value={"found": True})
        try:
            execute_required(trace, lambda name: tool)
        except ExecutionFailure:
            pass
        if not failed:
            audit.Runner.run_sync(agent, scenario["input"])
        return EvaluationRecord(scenario["id"], scenario["input"], "" if failed else "answer", [], 20,
                                0 if failed else 1, 0 if failed else 10, 0 if failed else 5, 0 if failed else 15,
                                execution=trace.snapshot(), execution_error="Required operation unresolved" if failed else None)

    monkeypatch.setattr(audit, "execute_scenario", execute)
    diagnostics = {}
    if failed:
        with pytest.raises(RuntimeError, match="Production execution failed"):
            audit.profile_scenario({"id": "offline", "input": "question"}, measure_tools=True, diagnostics=diagnostics)
        assert diagnostics["evaluation_record"]["execution"]["missing_required_operations"]
        assert diagnostics["tool_timings"][0]["status"] == "failed"
    else:
        row = audit.profile_scenario({"id": "offline", "input": "question"}, measure_tools=True, diagnostics=diagnostics)
        assert row["tool_timings"][0]["source"] == "runtime"
        assert row["tool_timings"][0]["latency_ms"] >= 0
        assert row["usage_reconciled"]
