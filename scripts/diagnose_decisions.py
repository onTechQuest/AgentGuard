"""One explicit, owned-transport diagnostic execution; no judges or reruns."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from src.agent.decision_evidence import classify
from src.agentguard.datasets import load_dataset
from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.scoring import evaluate_record
from src.agentguard.safety_evaluator import factual_grounding_failures
from src.hosting.collector import CampaignCollector
from src.hosting.runtime import WorkerRuntime


def diagnose(scenario, runtime_factory=WorkerRuntime):
    collector = CampaignCollector()
    runtime = runtime_factory(1, 0, diagnostic_mode=True)
    try:
        admission = runtime.submit(scenario["input"])
        collector.observe(admission)
        if not admission.accepted:
            raise RuntimeError("Diagnostic request was not admitted; no retry")
        result = admission.result()
        collector.record(result)
    finally:
        runtime.shutdown()
    report = collector.snapshot(runtime.status())
    trace = json.loads(result.execution_json) or {}
    operations = trace.get("operations", [])
    calls = [{"name": o["operation"]["tool"], "arguments": dict(o["operation"]["arguments"])}
             for o in operations if o.get("invoked")]
    outputs = [{"name": o["operation"]["tool"], "output": o.get("output")}
               for o in operations if o.get("status") == "completed"]
    record = EvaluationRecord(scenario["id"], scenario["input"], result.final_output or "", calls,
                              result.end_to_end_ms, None, None, None, None, tool_outputs=outputs)
    score = evaluate_record(scenario, record)
    grounded = not factual_grounding_failures(scenario, record)
    evidence = report["requests"][0].get("decision_evidence")
    report["diagnostic"] = {"scenario_id": scenario["id"], "timestamp": datetime.now(timezone.utc).isoformat(),
        "executions": 1, "functional_pass": score.functional_pass, "tool_pass": score.tool_pass,
        "argument_pass": score.argument_pass, "grounding_pass": grounded,
        "decision_categories": classify(evidence, expected_business_work=bool(scenario["expected_tools"])) if evidence else [],
        "evidence_available": evidence is not None,
        "runtime_policy": runtime._policy.snapshot()}
    # No prompt, final prose, exception message, or tool-result payload is persisted.
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true", required=True)
    args = parser.parse_args(argv)
    selected = [s for s in load_dataset(ROOT / "evals/datasets/functional.json", dataset_type="functional", suite="full")
                if s["id"] == args.scenario]
    if len(selected) != 1:
        parser.error("Select exactly one existing functional scenario")
    destination = args.output.resolve()
    if not destination.is_relative_to((ROOT / "reports").resolve()):
        parser.error("Diagnostic output must be under gitignored reports/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        parser.error("Output already exists; choose a new diagnostic target")
    # Exclusive claim BEFORE execution; failed publications never trigger another request.
    with destination.with_suffix(destination.suffix + ".started").open("x") as stream:
        stream.write(datetime.now(timezone.utc).isoformat())
    report = diagnose(selected[0])
    temporary = destination.with_name(".decisions-" + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(destination)
    return 0 if report["diagnostic"]["evidence_available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
