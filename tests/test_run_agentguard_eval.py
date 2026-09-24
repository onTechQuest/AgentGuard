from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import yaml

from scripts import run_agentguard_eval as runner
from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.semantic_evaluator import SemanticScore


@pytest.fixture
def run_setup(monkeypatch, tmp_path):
    scenarios = [
        {
            "id": f"scenario-{index}", "input": f"Where is order {index}?",
            "expected_output": f"Order {index} has shipped via UPS.",
            "expected_contains": ["shipped"], "forbidden_contains": [], "expected_tools": [],
        }
        for index in range(2)
    ]
    records = [
        EvaluationRecord(
            scenario_id=scenario["id"], input=scenario["input"], final_output=scenario["expected_output"],
            tool_calls=[], latency_ms=100, request_count=1, input_tokens=20,
            output_tokens=10, total_tokens=30,
            tool_outputs=[{"name": "get_order_status", "call_id": f"call-{index}", "output": {"status": "shipped"}}],
        )
        for index, scenario in enumerate(scenarios)
    ]
    semantics = [SemanticScore(record.scenario_id, 1.0, True, None, 1.0, True, None, 1.0, True, None) for record in records]
    dataset_path = tmp_path / "evals/datasets/functional.json"
    dataset_path.parent.mkdir(parents=True)
    dataset_path.write_text(json.dumps(scenarios), encoding="utf-8")
    config = runner.load_quality_gate_config(runner.PROJECT_ROOT / "config/quality-gates.yaml")
    config_path = tmp_path / "config/quality-gates.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    execute = Mock(side_effect=records)
    semantic_eval = Mock(side_effect=semantics)
    deterministic_eval = Mock(wraps=runner.evaluate_record)
    build = Mock(wraps=runner.build_scorecard)
    gate = Mock(wraps=runner.evaluate_quality_gate)
    for name, mock in (
        ("execute_scenario", execute), ("evaluate_semantics", semantic_eval),
        ("evaluate_record", deterministic_eval), ("build_scorecard", build),
        ("evaluate_quality_gate", gate),
    ):
        monkeypatch.setattr(runner, name, mock)
    return SimpleNamespace(
        scenarios=scenarios, records=records, semantics=semantics, execute=execute,
        semantic_eval=semantic_eval, deterministic_eval=deterministic_eval,
        build=build, gate=gate, dataset_path=dataset_path, config_path=config_path, config=config,
    )


def test_complete_pass_reuses_each_record_once(run_setup, capsys):
    setup = run_setup
    before = deepcopy((setup.records, setup.semantics))

    assert runner.main() == 0

    output = capsys.readouterr().out
    assert setup.execute.call_args_list == [call(scenario) for scenario in setup.scenarios]
    assert setup.deterministic_eval.call_count == setup.semantic_eval.call_count == 2
    for index, record in enumerate(setup.records):
        assert setup.deterministic_eval.call_args_list[index].args[1] is record
        assert setup.semantic_eval.call_args_list[index].args[0] is record
        assert setup.semantic_eval.call_args_list[index].args[1] == setup.scenarios[index]["expected_output"]
    setup.build.assert_called_once()
    assert setup.build.call_args.kwargs["semantic_scores"] == setup.semantics
    assert all(pair[0] is record for pair, record in zip(setup.build.call_args.args[0], setup.records))
    setup.gate.assert_called_once()
    assert setup.gate.call_args.args[1] == setup.config
    assert setup.gate.call_args.args[0].average_correctness == 1.0
    for heading in ("AGENTGUARD EVALUATION", "SCENARIOS", "DETERMINISTIC QUALITY", "SEMANTIC QUALITY", "PERFORMANCE", "QUALITY GATES"):
        assert heading in output.splitlines()
    for line in (
        "Passed: 2", "Failed: 0", "Functional Accuracy: 100.0%", "Tool Accuracy: 100.0%",
        "Argument Accuracy: 100.0%", "Average Answer Relevancy: 1.000", "Average Correctness: 1.000",
        "Average Hallucination/Faithfulness: 1.000", "Semantic Pass Rate: 100.0%",
        "Average Latency: 100 ms", "P95 Latency: 100 ms", "Average Tokens/Run: 30",
    ):
        assert line in output
    for metric in setup.config["quality_gates"]:
        assert f"{runner.GATE_LABELS[metric]} PASS" in output
    assert output.rstrip().endswith("FINAL DECISION: PASS")
    assert (setup.records, setup.semantics) == before


def test_semantic_failure_affects_release_and_prints_reason(run_setup, capsys):
    run_setup.semantics[0].correctness_score = 0.0
    run_setup.semantics[0].correctness_pass = False
    run_setup.semantics[0].correctness_reason = "The delivery date contradicts the expected answer."

    assert runner.main() == 1

    output = capsys.readouterr().out
    assert "Average Correctness: 0.500" in output
    assert "scenario-0" in output
    assert "Correctness FAIL: The delivery date contradicts the expected answer." in output
    assert "Correctness FAIL\n" in output
    assert "FINAL DECISION: FAIL" in output
    assert run_setup.execute.call_count == run_setup.semantic_eval.call_count == 2


def test_deterministic_failure_still_affects_release(run_setup, capsys):
    run_setup.records[0].final_output = "unrelated"

    assert runner.main() == 1

    output = capsys.readouterr().out
    assert "Passed: 1\nFailed: 1" in output
    assert "Missing expected output 'shipped'" in output
    assert "Functional Accuracy FAIL" in output
    assert "Average Correctness: 1.000" in output
    assert run_setup.semantic_eval.call_count == 2


def test_skipped_hallucination_prints_unavailable_and_fails_gate(run_setup, capsys):
    for semantic in run_setup.semantics:
        semantic.hallucination_score = None
        semantic.hallucination_pass = None
        semantic.hallucination_reason = "Skipped: no captured tool outputs available as context."

    assert runner.main() == 1

    output = capsys.readouterr().out
    assert "Average Hallucination/Faithfulness: unavailable" in output
    assert "Hallucination/Faithfulness SKIPPED: Skipped: no captured tool outputs" in output
    assert "Hallucination Score FAIL" in output
    assert "metric unavailable" in output


@pytest.mark.parametrize("stage, attribute, expected_evaluations", [
    ("execution", "execute", 0),
    ("deterministic evaluation", "deterministic_eval", 0),
    ("semantic evaluation", "semantic_eval", 1),
])
def test_errors_fail_without_retry_or_exposing_exception_text(
    run_setup, capsys, monkeypatch, stage, attribute, expected_evaluations,
):
    secret = "fake-secret-for-offline-test"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    getattr(run_setup, attribute).side_effect = RuntimeError(f"Request failed with key {secret}")

    assert runner.main() == 1

    output = capsys.readouterr().out
    assert f"Scenario scenario-0: {stage} failed (RuntimeError)." in output
    assert "FINAL DECISION: FAIL" in output
    assert secret not in output
    assert run_setup.execute.call_count == 1
    assert run_setup.semantic_eval.call_count == expected_evaluations
    run_setup.build.assert_not_called()
    run_setup.gate.assert_not_called()


def test_missing_expected_output_fails_before_any_execution(run_setup, capsys):
    del run_setup.scenarios[1]["expected_output"]
    run_setup.dataset_path.write_text(json.dumps(run_setup.scenarios), encoding="utf-8")

    assert runner.main() == 1

    run_setup.execute.assert_not_called()
    assert "FINAL DECISION: FAIL" in capsys.readouterr().out


def test_release_decision_uses_yaml_thresholds(run_setup, capsys):
    for semantic in run_setup.semantics:
        semantic.correctness_score = 0.8
    run_setup.config["quality_gates"]["correctness"]["minimum"] = 0.8
    run_setup.config_path.write_text(yaml.safe_dump(run_setup.config), encoding="utf-8")

    assert runner.main() == 0

    assert "Correctness PASS" in capsys.readouterr().out
