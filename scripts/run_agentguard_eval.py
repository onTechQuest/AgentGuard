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


GATE_LABELS = {
    "functional_accuracy": "Functional Accuracy",
    "tool_accuracy": "Tool Accuracy",
    "argument_accuracy": "Argument Accuracy",
    "p95_latency_ms": "P95 Latency",
    "average_tokens_per_run": "Token Usage",
    "failed_scenarios": "Failed Scenarios",
}


def main() -> int:
    print("# AGENTGUARD EVALUATION", flush=True)
    with (PROJECT_ROOT / "evals/datasets/functional.json").open(encoding="utf-8") as file:
        scenarios = json.load(file)

    records_and_scores = []
    for scenario in scenarios:
        try:
            record = execute_scenario(scenario)
        except Exception as error:
            # API exception messages can include request or credential details.
            print(f"\nScenario {scenario['id']}: execution failed ({type(error).__name__}).")
            print("Evaluation incomplete.\nFINAL DECISION: FAIL")
            return 1
        score = evaluate_record(scenario, record)
        records_and_scores.append((record, score))

    scorecard = build_scorecard(records_and_scores)
    config = load_quality_gate_config(PROJECT_ROOT / "config/quality-gates.yaml")
    gate = evaluate_quality_gate(scorecard, config)

    print(f"\nScenarios: {scorecard.total_scenarios}")
    print(f"Passed: {scorecard.passed_scenarios}")
    print(f"Failed: {scorecard.failed_scenarios}")
    print("\nQUALITY METRICS")
    print(f"Functional Accuracy: {scorecard.functional_accuracy:.1%}")
    print(f"Tool Accuracy: {scorecard.tool_accuracy:.1%}")
    print(f"Argument Accuracy: {scorecard.argument_accuracy:.1%}")
    print("\nPERFORMANCE")
    print(f"Average Latency: {scorecard.average_latency_ms:.0f} ms")
    print(f"P95 Latency: {scorecard.p95_latency_ms:.0f} ms")
    tokens = scorecard.average_tokens_per_run
    print(f"Average Tokens/Run: {tokens:g}" if tokens is not None else "Average Tokens/Run: unavailable")

    failed_scores = [score for _, score in records_and_scores if not score.overall_pass]
    if failed_scores:
        print("\nFAILED SCENARIOS")
        for score in failed_scores:
            print(score.scenario_id)
            for failure in score.failures:
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
