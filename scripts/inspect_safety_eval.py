"""Explicit single-scenario safety diagnostic; running this makes live calls."""

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agentguard.datasets import load_datasets
from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.safety_evaluator import safety_evaluate_record
from src.agentguard.safety_incidents import SafetyIncidentRecorder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-id", required=True)
    args = parser.parse_args(argv)
    scenarios = load_datasets(PROJECT_ROOT / "evals/datasets", suite="full").safety
    scenario = next((s for s in scenarios if s["id"] == args.scenario_id), None)
    if scenario is None:
        parser.error("Select an existing safety scenario ID")
    incidents = SafetyIncidentRecorder(PROJECT_ROOT, retain_all=True)
    record, stage = None, "execution"
    try:
        record = execute_scenario(scenario)
        if record.execution_error is not None:
            incidents.retain(scenario, record, stage=stage)
            print("SAFETY DIAGNOSTIC: execution failed")
            return 1
        stage = "safety evaluation"
        score = safety_evaluate_record(scenario, record)
    except Exception as error:
        incidents.retain(scenario, record, error=error, stage=stage)
        print(f"SAFETY DIAGNOSTIC: {stage} failed ({type(error).__name__})")
        return 1
    incidents.retain(scenario, record, score)
    print(f"SAFETY DIAGNOSTIC: {'PASS' if score.passed else 'FAIL'}")
    return 0 if score.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
