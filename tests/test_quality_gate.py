from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.agentguard.quality_gate import load_quality_gate_config, evaluate_quality_gate
from src.agentguard.scorecard import AgentGuardScorecard


SEMANTIC_FIELDS = {
    "answer_relevancy": "average_answer_relevancy",
    "correctness": "average_correctness",
    "hallucination_score": "average_hallucination_score",
    "semantic_pass_rate": "semantic_pass_rate",
}


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
        average_answer_relevancy=gates["answer_relevancy"]["minimum"],
        average_correctness=gates["correctness"]["minimum"],
        average_hallucination_score=gates["hallucination_score"]["minimum"],
        semantic_pass_rate=gates["semantic_pass_rate"]["minimum"],
    )


def test_complete_pass_and_boundary_equality(config, card):
    before = deepcopy((config, card))
    result = evaluate_quality_gate(card, config)
    assert result.passed and result.failures == []
    assert len(result.checks) == 10
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
    assert sum(check["passed"] for check in result.checks) == 9


def test_multiple_failed_thresholds(config, card):
    card.functional_accuracy = -1
    card.tool_accuracy = -1
    card.average_tokens_per_run = None
    result = evaluate_quality_gate(card, config)
    assert not result.passed
    assert len(result.failures) == 3
    assert len(result.checks) == 10
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


def test_semantic_metrics_all_pass(config, card):
    for field in SEMANTIC_FIELDS.values():
        setattr(card, field, 1.0)

    result = evaluate_quality_gate(card, config)

    assert result.passed
    assert result.failures == []
    assert len(result.checks) == 10
    assert all(check["passed"] for check in result.checks)


@pytest.mark.parametrize("metric, field", SEMANTIC_FIELDS.items())
def test_one_semantic_metric_fails(config, card, metric, field):
    setattr(card, field, config["quality_gates"][metric]["minimum"] - 0.01)

    result = evaluate_quality_gate(card, config)

    assert not result.passed
    assert len(result.failures) == 1
    assert metric in result.failures[0]
    assert [check["metric"] for check in result.checks if not check["passed"]] == [metric]
    check = next(check for check in result.checks if check["metric"] == metric)
    assert check["actual"] == getattr(card, field)
    assert check["comparison"] == ">="


def test_multiple_semantic_metrics_fail(config, card):
    card.average_correctness = 0.0
    card.average_hallucination_score = 0.0
    card.semantic_pass_rate = 0.0

    result = evaluate_quality_gate(card, config)

    assert not result.passed
    assert len(result.failures) == 3
    assert [check["metric"] for check in result.checks if not check["passed"]] == [
        "correctness", "hallucination_score", "semantic_pass_rate",
    ]
    assert all(check["passed"] for check in result.checks if check["metric"] not in SEMANTIC_FIELDS)


@pytest.mark.parametrize("metric, field", SEMANTIC_FIELDS.items())
def test_unavailable_semantic_metric_fails_clearly(config, card, metric, field):
    setattr(card, field, None)

    result = evaluate_quality_gate(card, config)

    assert not result.passed
    assert len(result.failures) == 1
    assert metric in result.failures[0]
    assert "metric unavailable" in result.failures[0]
    check = next(check for check in result.checks if check["metric"] == metric)
    assert check["actual"] is None
    assert not check["passed"]


@pytest.mark.parametrize("metric, field", SEMANTIC_FIELDS.items())
def test_semantic_thresholds_and_exact_boundary_come_from_yaml(config, card, tmp_path, metric, field):
    setattr(card, field, 0.37)
    path = tmp_path / "gates.yaml"
    config["quality_gates"][metric]["minimum"] = 0.37
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = evaluate_quality_gate(card, load_quality_gate_config(path))

    assert result.passed
    check = next(check for check in result.checks if check["metric"] == metric)
    assert check["actual"] == check["threshold"] == 0.37
    config["quality_gates"][metric]["minimum"] = 0.38
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert not evaluate_quality_gate(card, load_quality_gate_config(path)).passed


def test_semantic_gates_can_be_omitted_for_existing_configs(config, card):
    for metric, field in SEMANTIC_FIELDS.items():
        del config["quality_gates"][metric]
        setattr(card, field, None)

    result = evaluate_quality_gate(card, config)

    assert result.passed
    assert len(result.checks) == 6


@pytest.mark.parametrize("metric", SEMANTIC_FIELDS)
@pytest.mark.parametrize("rule", [
    None, {}, {"maximum": 1.0}, {"minimum": "0.9"}, {"minimum": True},
    {"minimum": float("nan")}, {"minimum": float("inf")},
    {"minimum": -0.1}, {"minimum": 1.1},
])
def test_invalid_semantic_thresholds_are_rejected(config, card, metric, rule):
    config["quality_gates"][metric] = rule

    with pytest.raises(ValueError, match=metric):
        evaluate_quality_gate(card, config)
