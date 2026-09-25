import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents import Agent, FunctionTool
from agents.usage import Usage

from scripts import audit_latency as audit
from scripts import audit_token_usage as token_audit
from src.agentguard.evaluation_record import EvaluationRecord


def observation(scenario="example", repetition=1, latency=1000):
    return {
        "scenario_id": scenario, "repetition": repetition, "status": "completed", "latency_ms": latency,
        "production_usage": {"requests": 3, "input_tokens": 1100, "output_tokens": 100, "total_tokens": 1200},
        "tool_calls": [{"name": "lookup", "arguments": {"id": "x"}}],
        "tool_timings": [{"tool": "lookup", "status": "completed", "latency_ms": 2}],
        "components": [{"component": "router", "latency_ms": latency - 400, "runner_visible_failed_attempts": 0},
                       {"component": "agent", "latency_ms": 400, "runner_visible_failed_attempts": 0}],
    }


def test_nearest_rank_retains_outlier_and_distinguishes_median():
    result = audit.distribution(list(range(1, 40)) + [22000])
    assert result["count"] == 40
    assert result["p50"] == 20 and result["median"] == 20.5
    assert result["p90"] == 36 and result["p95"] == 38
    assert result["p99"] == result["max"] == 22000
    assert audit.distribution(list(range(8)))["p95"] == 7
    assert audit.distribution(list(range(5)))["p95"] == 4
    assert audit.distribution([]) == {"count": 0}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_bad_measurement_fails_instead_of_being_discarded(value):
    with pytest.raises(ValueError):
        audit.distribution([value])


def test_outliers_use_strict_threshold_and_do_not_claim_no_http_retries():
    result = audit.summarize([observation(latency=7500), observation(repetition=2, latency=22000)], 7500)
    assert result["latency_ms"]["count"] == 2
    assert len(result["outliers"]) == 1
    outlier = result["outliers"][0]
    assert outlier["http_retries"] == outlier["provider_delay_cause"] == "unknown"
    assert outlier["sdk_visible_failed_attempts"] == 0
    assert outlier["same_trajectory_as_normal_peers"] is True
    assert outlier["tool_invocation_sum_ms"] == 2


def test_failed_attempt_is_retained_separately_from_completed_distribution():
    failure = {"scenario_id": "example", "repetition": 2, "status": "failed", "latency_ms": 30000}
    result = audit.summarize([observation(), failure], 7500)
    assert result["failed"] == 1
    assert result["latency_ms"]["count"] == 1
    assert result["all_attempt_latency_ms"]["max"] == 30000
    assert result["by_scenario"]["example"]["failed"] == 1
    assert result["outliers"][0]["sdk_visible_failed_attempts"] is None


def test_five_passes_execute_each_scenario_exactly_five_times_without_retries(monkeypatch, tmp_path):
    scenarios = [{"id": f"scenario_{i}"} for i in range(8)]
    monkeypatch.setattr(audit, "load_dataset", Mock(return_value=scenarios))
    monkeypatch.setattr(audit, "load_quality_gate_config", Mock(return_value={"quality_gates": {"p95_latency_ms": {"maximum": 7500}}}))
    calls = []

    def profile(scenario, **kwargs):
        calls.append(scenario["id"])
        assert kwargs["measure_tools"] is True
        row = observation(scenario=scenario["id"])
        del row["repetition"]
        if len(calls) == 3:
            raise RuntimeError("secret credential must not be saved")
        return row

    monkeypatch.setattr(audit, "profile_scenario", profile)
    output = tmp_path / "report.json"
    assert audit.main(["--output", str(output)]) == 1
    assert calls == [scenario["id"] for scenario in scenarios] * 5
    serialized = output.read_text(encoding="utf-8")
    assert "secret credential" not in serialized
    report = json.loads(serialized)
    assert report["complete"] and len(report["observations"]) == 40
    assert report["summary"]["failed"] == 1
    assert set(report["execution_counts"].values()) == {5}
    assert [r["repetition"] for r in report["observations"]] == [rep for rep in range(1, 6) for _ in range(8)]


def test_public_tool_timer_preserves_arguments_return_and_restores_callable(monkeypatch):
    invoke = AsyncMock(return_value={"found": True})
    tool = FunctionTool(name="lookup", description="Lookup", params_json_schema={"type": "object", "properties": {}}, on_invoke_tool=invoke)
    agent = Agent(name="AgentGuard Support Agent", instructions="test", tools=[tool])
    context = object()
    usage = Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)
    response = SimpleNamespace(context_wrapper=SimpleNamespace(usage=usage), raw_responses=[SimpleNamespace(usage=usage)])

    def run(target, text):
        assert asyncio.run(target.tools[0].on_invoke_tool(context, '{"order_id":"x"}')) == {"found": True}
        return response

    monkeypatch.setattr(token_audit.Runner, "run_sync", Mock(side_effect=run))

    def execute(scenario):
        token_audit.Runner.run_sync(agent, "original request")
        return EvaluationRecord(scenario["id"], "original request", "answer", [], 20, 1, 10, 5, 15)

    monkeypatch.setattr(token_audit, "execute_scenario", execute)
    row = token_audit.profile_scenario({"id": "example"}, measure_tools=True)
    invoke.assert_awaited_once_with(context, '{"order_id":"x"}')
    assert tool.on_invoke_tool is invoke
    assert row["tool_timings"][0]["latency_ms"] >= 0
    assert row["tool_timings"][0]["status"] == "completed"
    assert row["usage_reconciled"]
