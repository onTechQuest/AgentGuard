from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.agentguard.quality_gate import load_quality_gate_config, evaluate_quality_gate
from src.agentguard.scorecard import AgentGuardScorecard


@pytest.fixture
def config():
    return load_quality_gate_config(Path(__file__).resolve().parents[1] / "config" / "quality-gates.yaml")


@pytest.fixture
def card(config):
    gates = config["quality_gates"]
    return AgentGuardScorecard(
        total_scenarios=10, passed_scenarios=10, failed_scenarios=gates["failed_scenarios"]["maximum"],
        functional_accuracy=gates["functional_accuracy"]["minimum"],
        tool_accuracy=gates["tool_accuracy"]["minimum"],
        argument_accuracy=gates["argument_accuracy"]["minimum"],
        average_latency_ms=1.0, p95_latency_ms=gates["p95_latency_ms"]["maximum"],
        average_tokens_per_run=gates["average_tokens_per_run"]["maximum"],
    )


def test_complete_pass_and_boundary_equality(config, card):
    before = deepcopy((config, card))
    result = evaluate_quality_gate(card, config)
    assert result.passed and result.failures == []
    assert len(result.checks) == 6
    for check in result.checks:
        assert check["passed"]
        assert check["actual"] == check["threshold"]
        assert check["comparison"] in (">=", "<=")
        assert check["metric"] in config["quality_gates"]
    assert (config, card) == before


@pytest.mark.parametrize("metric", [
    "functional_accuracy", "tool_accuracy", "argument_accuracy",
    "p95_latency_ms", "average_tokens_per_run", "failed_scenarios",
])
def test_one_failed_threshold(config, card, metric):
    setattr(card, metric, getattr(card, metric) + (-0.01 if metric.endswith("accuracy") else 1))
    result = evaluate_quality_gate(card, config)
    assert not result.passed
    assert len(result.failures) == 1
    assert metric in result.failures[0]
    assert sum(check["passed"] for check in result.checks) == 5


def test_multiple_failed_thresholds(config, card):
    card.functional_accuracy = -1
    card.tool_accuracy = -1
    card.average_tokens_per_run = None
    result = evaluate_quality_gate(card, config)
    assert not result.passed
    assert len(result.failures) == 3
    assert len(result.checks) == 6
    assert "unavailable" in result.failures[-1]


def test_thresholds_come_from_configuration(config, card, tmp_path):
    card.p95_latency_ms += 10
    config["quality_gates"]["p95_latency_ms"]["maximum"] = card.p95_latency_ms
    path = tmp_path / "gates.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert evaluate_quality_gate(card, load_quality_gate_config(path)).passed


@pytest.mark.parametrize("bad", [None, [], {}, {"version": 2}, {"version": True}, {"version": 1, "quality_gates": []}])
def test_invalid_structure(bad, card):
    with pytest.raises(ValueError):
        evaluate_quality_gate(card, bad)


@pytest.mark.parametrize("rule", [None, {}, {"maximum": 1}, {"minimum": "0.9"}, {"minimum": True}, {"minimum": float("nan")}, {"minimum": float("inf")}, {"minimum": -1}, {"minimum": 1.1}])
def test_invalid_thresholds(config, card, rule):
    config["quality_gates"]["functional_accuracy"] = rule
    with pytest.raises(ValueError, match="functional_accuracy"):
        evaluate_quality_gate(card, config)


def test_missing_metric(config, card):
    del config["quality_gates"]["failed_scenarios"]
    with pytest.raises(ValueError, match="failed_scenarios"):
        evaluate_quality_gate(card, config)


@pytest.mark.parametrize("contents", ["quality_gates: [", "", "version: 1\nquality_gates: {}"])
def test_invalid_yaml_file(tmp_path, contents):
    path = tmp_path / "bad.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_quality_gate_config(path)


def test_missing_file(tmp_path):
    with pytest.raises(ValueError, match="Unable to load"):
        load_quality_gate_config(tmp_path / "missing.yaml")
