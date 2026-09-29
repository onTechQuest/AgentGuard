"""Explicit single-scenario safety diagnostic; running this makes live calls."""

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agentguard.datasets import load_datasets
from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.failure_evidence import retain_failure
from src.agentguard.safety_evaluator import safety_evaluate_record
from src.agentguard.safety_incidents import SafetyIncidentRecorder
from src.agentguard.lineage import lineage_entry, start_run, fingerprint
from src.agentguard.lineage_adapters import plain


@lineage_entry
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("Repetitions must be positive")
    scenarios = load_datasets(PROJECT_ROOT / "evals/datasets", suite="full").safety
    scenario = next((s for s in scenarios if s["id"] == args.scenario_id), None)
    if scenario is None:
        parser.error("Select an existing safety scenario ID")
    lineage = start_run(PROJECT_ROOT, suite="smoke", safety=[scenario], repetitions=args.repetitions)
    incidents = SafetyIncidentRecorder(PROJECT_ROOT, retain_all=True)
    semantic, composite, evidence_counts = Counter(), Counter(), Counter()
    observations = []
    failed = False
    for repetition in range(1, args.repetitions + 1):
        record, stage = None, "execution"
        try:
            record = execute_scenario(scenario)
            if record.execution_error is not None:
                retain_failure(scenario, record=record, stage=stage)
                incidents.retain(scenario, record, stage=stage)
                failed = True
                observations.append({"repetition": repetition, "stage": stage, "status": "execution_failed"})
                continue
            stage = "safety evaluation"
            score = safety_evaluate_record(scenario, record)
        except BaseException as error:
            retain_failure(scenario, error, record=record, stage=stage)
            if not isinstance(error, Exception):
                raise
            incidents.retain(scenario, record, error=error, stage=stage)
            failed = True
            observations.append({"repetition": repetition, "stage": stage, "error_type": type(error).__name__})
            continue
        incidents.retain(scenario, record, score)
        evidence = plain(asdict(score.injection_evidence)) if score.injection_evidence else None
        semantic[score.prompt_injection_label or "unavailable"] += 1
        composite[score.prompt_injection_verdict or "unavailable"] += 1
        evidence_counts[fingerprint("injection-evidence", evidence)] += 1
        observations.append({"repetition": repetition, "semantic_verdict": score.prompt_injection_label,
            "composite_verdict": score.prompt_injection_verdict, "classification": score.prompt_injection_classification,
            "disagreement": score.prompt_injection_disagreement, "deterministic_evidence": evidence})
        failed |= not score.passed
    lineage.aggregate = {"semantic_distribution": dict(semantic), "composite_distribution": dict(composite),
        "deterministic_evidence_distribution": dict(evidence_counts), "observations": observations,
        "stable_deterministic_evidence": len(evidence_counts) == 1 and sum(evidence_counts.values()) == args.repetitions,
        "semantic_variation": len(semantic) > 1}
    lineage.evaluation_complete = len(semantic) and sum(semantic.values()) == args.repetitions
    print(json.dumps(lineage.aggregate, indent=2))
    print(f"Lineage run: {lineage.manifest.run_id}")
    print(f"SAFETY DIAGNOSTIC: {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
