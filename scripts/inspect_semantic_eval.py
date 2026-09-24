"""Inspect a demo semantic evaluation: python scripts/inspect_semantic_eval.py."""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.semantic_evaluator import evaluate_semantics


def main() -> None:
    scenario = {"id": "semantic_demo_001", "input": "Where is order ORD-1001?"}
    expected_output = (
        "Order ORD-1001 has shipped via UPS and is estimated to arrive on September 12, 2026."
    )
    record = execute_scenario(scenario)
    score = evaluate_semantics(record, expected_output)

    print(f"Scenario ID: {record.scenario_id}")
    print(f"Final output: {record.final_output}")
    print(f"Answer relevancy score: {score.answer_relevancy_score}")
    status = "PASS" if score.answer_relevancy_pass else "FAIL"
    print(f"Answer relevancy: {status}")
    print(f"Answer relevancy reason: {score.answer_relevancy_reason}")
    print(f"Correctness score: {score.correctness_score}")
    correctness_status = "PASS" if score.correctness_pass else "FAIL"
    print(f"Correctness: {correctness_status}")
    print(f"Correctness reason: {score.correctness_reason}")
    print(f"Hallucination score: {score.hallucination_score}")
    if score.hallucination_pass is None:
        hallucination_status = "SKIPPED"
    else:
        hallucination_status = "PASS" if score.hallucination_pass else "FAIL"
    print(f"Hallucination: {hallucination_status}")
    print(f"Hallucination reason: {score.hallucination_reason}")


if __name__ == "__main__":
    main()
