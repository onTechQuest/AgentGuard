"""Inspect an exact retained evaluation run; never executes an evaluation."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agentguard.lineage import load_run
from src.agentguard.observability import project, dispatch, ConsoleObserver, JsonObserver


def inspect(root, run_id, scenario_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id): raise ValueError("Invalid run ID")
    loaded = load_run(Path(root) / "reports" / "evaluations" / run_id)
    if scenario_id not in {s["scenario_id"] for s in loaded["scenarios"]}:
        raise ValueError("Scenario is not in this run")
    invocations = []
    for path in (Path(root) / "reports/continuous_evaluation/invocations").glob("*/receipt.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("evaluation_run_id") == run_id: invocations.append(receipt.get("invocation_id"))
        except (OSError, ValueError):
            continue
    invocation_id = invocations[0] if len(invocations) == 1 else None
    results = loaded.get("results", {})
    observations = results.get("observations", [])
    passed = results.get("aggregate_results", {}).get("quality_result", {}).get("passed")
    decision = "PASS" if passed is True else "FAIL" if passed is False else None
    events, repetition = [], 0
    for execution in results.get("executions", []):
        if execution["scenario_id"] != scenario_id: continue
        repetition += 1
        observation = next((o for o in observations if o["scenario_id"] == scenario_id and o.get("repetition", 1) == repetition),
                           dict(scenario_id=scenario_id, repetition=repetition))
        events.extend(project(run_id, observation, failure=execution.get("failure_evidence"), invocation_id=invocation_id, quality_decision=decision))
    if not events: events = project(run_id, dict(scenario_id=scenario_id), invocation_id=invocation_id)
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--json", action="store_true", help="Also export sanitized local events")
    args = parser.parse_args()
    try:
        events = inspect(ROOT, args.run_id, args.scenario_id)
        dispatch(ConsoleObserver(), events)
        if args.json: dispatch(JsonObserver(ROOT), events)
    except (OSError, ValueError, KeyError):
        parser.exit(2, "Unable to inspect validated retained artifacts.\n")


if __name__ == "__main__": main()
