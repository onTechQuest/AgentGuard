"""Prepare or analyze 14A-R2 locally. This command never executes a request."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from src.agentguard.synthesis_qualification import historical_populations, normalize, replay, summarize

HISTORY = ("performance_qualification.json", "latency_audit_smoke.json", "reliability_baseline.json",
           "reliability_13c4b_pass1.json", "reliability_13c4b_pass2.json", "reliability_13c4d_tail_probe.json",
           "reliability_13c4f_candidate_R.json", "reliability_13c4g_candidate_R_tail.json")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/latency_14ar2")
    parser.add_argument("--historical", type=Path, nargs="*")
    args = parser.parse_args(argv)
    dataset = json.loads((ROOT / "evals/datasets/functional.json").read_text(encoding="utf-8"))
    scenario_ids = [r["id"] for r in dataset if r.get("tier") == "smoke"]
    if len(scenario_ids) != 8:
        parser.error("The planned population must contain exactly eight functional smoke scenarios")
    rows, report = [], {}
    if args.input:
        report = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(report.get("observations"), list):
            parser.error("Input must be an existing audit_latency/performance report")
        rows = [normalize(r) for r in report["observations"]]
    expected = Counter((scenario, rep) for rep in range(1, 6) for scenario in scenario_ids)
    actual = Counter((r["scenario_id"], r["repetition"]) for r in rows)
    balanced = (actual == expected and report.get("complete") is True
                and all(r.get("status") in {"completed", "failed"} for r in report.get("observations", [])))
    campaign = dict(status="PLANNED_NOT_EXECUTED" if args.prepare else "COMPLETE" if balanced else "INCOMPLETE",
        protocol=dict(scenario_ids=scenario_ids, repetitions=5, requests_planned=40,
            sampling="Sequential dataset-order passes; no warmup exclusion, no replacement of failures, no judges.",
            request_deadline_ms=20000, router_allowance_ms=13000, recovery_reserve_ms=3000, synthesis_allowance_ms=6000),
        source=str(args.input) if args.input else None, balanced_population=balanced,
        run_id=report.get("run_id"), manifest_digest=report.get("manifest_digest"),
        observations=rows, summary=summarize(rows))
    paths = args.historical if args.historical is not None else [ROOT / "reports" / name for name in HISTORY]
    history = historical_populations([(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in paths if p.exists()])
    for population in history:
        population["matches_planned_scenario_ids"] = population["scenario_ids"] == sorted(scenario_ids)
        population["matches_planned_sample_size"] = population["summary"]["request_count"] == 40
    production_history = [p for p in history if p["source"] in {"production", "legacy_unspecified"}]
    historical_count = sum(p["summary"]["distributions_ms"]["synthesis"]["count"] for p in production_history)
    known_tail = (any(p["summary"]["exceedances"]["synthesis"].get("6000", 0) for p in production_history)
                  if historical_count else None)
    comparison = dict(current=campaign["summary"], historical_populations=history,
        historical_sources_missing=[str(p) for p in paths if not p.exists()],
        historical_over_6s_observed=known_tail,
        interpretation=("Retained historical populations already contain >6s synthesis completions; the single recent observation alone does not establish a new tail."
                        if known_tail else "No retained comparable >6s measurement found; absence is not evidence that a tail did not exist."),
        current_distribution_available=bool(rows),
        comparison_limits="Keep populations separate. Matching N and IDs does not establish policy/model/prompt equivalence; no causal regression claim.",
        policy_analysis_document="docs/SYNTHESIS_LATENCY_REQUALIFICATION.md")
    policies = replay(rows)
    # The user's diagnostic is explicitly separate from the later measured campaign.
    supplied = dict(scenario_id="missing_order_001", request_id=None, repetition=None, status="incomplete",
        component_latency_ms=dict(primary_router=2220.27, recovery_planner=None, required_operations=1.34, synthesis=11438.89),
        total_request_ms=13663.03, request_deadline_ms=20000, overall_deadline_exhausted=False,
        configured_synthesis_allowance_ms=6000, failure_component="synthesis", failure_category="DEADLINE_EXHAUSTED",
        late_completion=True, result_abandoned=True, retry_attempts_total=0, lower_layer_retries_configured=False,
        remaining_request_budget_before_synthesis_ms=None)
    from src.agentguard.synthesis_qualification import classify
    comparison["user_supplied_observation"] = dict(source="User-provided diagnostic; not added to N=40", classification=classify(supplied),
        measured_synthesis_ms=11438.89, measured_total_ms=13663.03,
        replay_limitation="Remaining budget before synthesis was not supplied; do not reconstruct it.")
    for name, value in (("campaign.json", campaign), ("comparison.json", comparison), ("policy_replay.json", policies)):
        if args.input and (args.output_dir / name).resolve() == args.input.resolve():
            parser.error("Analysis output must not overwrite its raw input")
        write(args.output_dir / name, value)
    print(json.dumps(dict(status=campaign["status"], current_requests=len(rows), historical_populations=len(history),
                          historical_over_6s_observed=known_tail, output=str(args.output_dir))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
