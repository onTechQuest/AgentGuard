from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts import audit_latency, run_agentguard_eval as runner
from src.agentguard.performance import qualify_performance, production_usage
from src.agentguard.evaluation_record import EvaluationRecord


IDS = [f"scenario-{index}" for index in range(8)]


def observations(repetitions=5):
    return [dict(scenario_id=scenario_id, repetition=rep, status="completed", latency_ms=1000)
            for rep in range(1, repetitions + 1) for scenario_id in IDS]


@pytest.mark.parametrize("slow_count,expected", [(1, True), (2, True), (3, False)])
def test_forty_observation_qualification_keeps_every_outlier(slow_count, expected):
    rows = observations()
    for row in rows[:slow_count]:
        row["latency_ms"] = 22493
    before = deepcopy(rows)
    result = qualify_performance(rows, IDS, 5, 7500)
    assert result.passed is expected
    assert result.observation_count == 40
    assert result.p95_latency_ms == (22493 if slow_count >= 3 else 1000)
    assert rows == before


def test_exact_boundary_and_larger_benchmark():
    rows = observations(25)
    for row in rows:
        row["latency_ms"] = 7500
    result = qualify_performance(rows, IDS, 25, 7500)
    assert result.passed and result.observation_count == 200


@pytest.mark.parametrize("defect", ["eight", "missing", "duplicate", "failed", "running", "invalid", "unavailable"])
def test_unqualified_sampling_cannot_pass(defect):
    rows = observations()
    repetitions = 5
    if defect == "eight":
        rows, repetitions = rows[:8], 1
    elif defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows[-1] = dict(rows[0])
    elif defect in {"failed", "running"}:
        rows[0]["status"] = defect
    else:
        rows[0]["latency_ms"] = float("nan") if defect == "invalid" else None
    result = qualify_performance(rows, IDS, repetitions, 7500)
    assert not result.passed and result.failures


def test_usage_counts_missing_tokens_without_imputing_zero():
    records = [EvaluationRecord("x", "input", "output", [], 22493, 3, None, None, None),
               EvaluationRecord("y", "input", "output", [], 7500, 3, 100, 20, 120)]
    usage = production_usage(records)
    assert usage.execution_count == 2 and len(usage.latency_observations) == 2
    assert usage.maximum_latency_ms == 22493
    assert len(usage.exceeding(7500)) == 1
    assert usage.token_observation_count == 1 and usage.average_tokens_per_execution == 120


@pytest.mark.parametrize("slow_count,exit_code", [(1, 0), (3, 1)])
def test_performance_cli_reuses_diagnostic_without_judges(monkeypatch, tmp_path, capsys, slow_count, exit_code):
    scenarios = [{"id": scenario_id} for scenario_id in IDS]
    monkeypatch.setattr(audit_latency, "load_dataset", Mock(return_value=scenarios))
    monkeypatch.setattr(audit_latency, "load_quality_gate_config", Mock(return_value={
        "quality_gates": {"p95_latency_ms": {"maximum": 7500}},
    }))
    profile = Mock()

    def sample(scenario, **kwargs):
        return {"scenario_id": scenario["id"], "latency_ms": 22493 if profile.call_count <= slow_count else 1000,
                "production_usage": {"requests": 3, "total_tokens": 1206}, "tool_calls": []}

    profile.side_effect = sample
    monkeypatch.setattr(audit_latency, "profile_scenario", profile)
    forbidden = Mock(side_effect=AssertionError("Correctness/judge path must not run"))
    for name in ("execute_scenario", "evaluate_semantics", "safety_evaluate_record", "evaluate_record", "load_datasets"):
        monkeypatch.setattr(runner, name, forbidden)
    output = tmp_path / "qualification.json"
    assert runner.main(["--suite", "performance", "--report", str(output)]) == exit_code
    forbidden.assert_not_called()
    assert profile.call_count == 40
    assert [call.args[0] for call in profile.call_args_list] == scenarios * 5
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["qualification"]["passed"] is (exit_code == 0)
    assert report["summary"]["exceedance_count"] == slow_count
    assert report["summary"]["exceedance_rate"] == slow_count / 40
    assert report["summary"]["latency_ms"]["max"] == 22493
    assert len(report["observations"]) == 40
    text = capsys.readouterr().out
    assert "N: 40" in text and "MAX: 22493.00 ms" in text
    assert "scenario-0 repetition 1: 22493.00 ms" in text
    assert f"FINAL DECISION: {'PASS' if exit_code == 0 else 'FAIL'}" in text


@pytest.mark.parametrize("args", [
    ["--suite", "performance", "--repetitions", "4"],
    ["--suite", "smoke", "--repetitions", "5"],
    ["--suite", "full", "--report", "report.json"],
])
def test_invalid_mode_options_rejected_before_execution(args):
    with pytest.raises(SystemExit) as error:
        runner.parse_args(args)
    assert error.value.code == 2
