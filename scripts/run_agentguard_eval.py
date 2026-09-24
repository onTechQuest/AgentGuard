"""Run AgentGuard end to end: python scripts/run_agentguard_eval.py."""

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.quality_gate import evaluate_quality_gate, load_quality_gate_config
from src.agentguard.scorecard import build_scorecard
from src.agentguard.scoring import evaluate_record
from src.agentguard.semantic_evaluator import evaluate_semantics


GATE_LABELS = {
    "functional_accuracy": "Functional Accuracy",
    "tool_accuracy": "Tool Accuracy",
    "argument_accuracy": "Argument Accuracy",
    "p95_latency_ms": "P95 Latency",
    "average_tokens_per_run": "Token Usage",
    "failed_scenarios": "Failed Scenarios",
    "answer_relevancy": "Answer Relevancy",
    "correctness": "Correctness",
    "hallucination_score": "Hallucination Score",
    "semantic_pass_rate": "Semantic Pass Rate",
}


def main() -> int:
    print("AGENTGUARD EVALUATION", flush=True)
    try:
        with (PROJECT_ROOT / "evals/datasets/functional.json").open(encoding="utf-8") as file:
            scenarios = json.load(file)
        config = load_quality_gate_config(PROJECT_ROOT / "config/quality-gates.yaml")
        for scenario in scenarios:
            if not isinstance(scenario["expected_output"], str):
                raise ValueError("expected_output must be a string")
    except Exception as error:
        print(f"Evaluation setup failed ({type(error).__name__}).")
        print("Check the dataset expected_output fields and quality gate YAML.\nFINAL DECISION: FAIL")
        return 1

    records_and_scores = []
    semantic_scores = []
    for scenario in scenarios:
        stage = "execution"
        try:
            record = execute_scenario(scenario)
            stage = "deterministic evaluation"
            score = evaluate_record(scenario, record)
            stage = "semantic evaluation"
            semantic_score = evaluate_semantics(record, scenario["expected_output"])
        except Exception as error:
            # API exception messages can include request or credential details.
            print(f"\nScenario {scenario['id']}: {stage} failed ({type(error).__name__}).")
            print("Evaluation incomplete.\nFINAL DECISION: FAIL")
            return 1
        records_and_scores.append((record, score))
        semantic_scores.append(semantic_score)

    scorecard = build_scorecard(records_and_scores, semantic_scores=semantic_scores)
    gate = evaluate_quality_gate(scorecard, config)

    print("\nSCENARIOS")
    print(f"Total: {scorecard.total_scenarios}")
    print(f"Passed: {scorecard.passed_scenarios}")
    print(f"Failed: {scorecard.failed_scenarios}")
    print("\nDETERMINISTIC QUALITY")
    print(f"Functional Accuracy: {scorecard.functional_accuracy:.1%}")
    print(f"Tool Accuracy: {scorecard.tool_accuracy:.1%}")
    print(f"Argument Accuracy: {scorecard.argument_accuracy:.1%}")
    print("\nSEMANTIC QUALITY")
    for label, value in (
        ("Average Answer Relevancy", scorecard.average_answer_relevancy),
        ("Average Correctness", scorecard.average_correctness),
        ("Average Hallucination/Faithfulness", scorecard.average_hallucination_score),
    ):
        print(f"{label}: {value:.3f}" if value is not None else f"{label}: unavailable")
    rate = scorecard.semantic_pass_rate
    print(f"Semantic Pass Rate: {rate:.1%}" if rate is not None else "Semantic Pass Rate: unavailable")
    print("\nPERFORMANCE")
    print(f"Average Latency: {scorecard.average_latency_ms:.0f} ms")
    print(f"P95 Latency: {scorecard.p95_latency_ms:.0f} ms")
    tokens = scorecard.average_tokens_per_run
    print(f"Average Tokens/Run: {tokens:g}" if tokens is not None else "Average Tokens/Run: unavailable")

    printed_diagnostics = False
    for (_, score), semantic in zip(records_and_scores, semantic_scores):
        reasons = list(score.failures)
        for label, passed, reason in (
            ("Answer Relevancy", semantic.answer_relevancy_pass, semantic.answer_relevancy_reason),
            ("Correctness", semantic.correctness_pass, semantic.correctness_reason),
            ("Hallucination/Faithfulness", semantic.hallucination_pass, semantic.hallucination_reason),
        ):
            if passed is not True:
                status = "SKIPPED" if passed is None else "FAIL"
                reasons.append(f"{label} {status}: {reason or 'No reason provided.'}")
        if reasons:
            if not printed_diagnostics:
                print("\nSCENARIO DIAGNOSTICS")
                printed_diagnostics = True
            print(score.scenario_id)
            for failure in reasons:
                print(f"  - {failure}")

    print("\nQUALITY GATES")
    for check in gate.checks:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"{GATE_LABELS[check['metric']]} {status}")
    for failure in gate.failures:
        print(f"  - {failure}")
    print(f"\nFINAL DECISION: {'PASS' if gate.passed else 'FAIL'}")
    return 0 if gate.passed else 1


if __name__ == "__main__":
    sys.exit(main())
