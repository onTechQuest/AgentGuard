"""Compare two retained completed runs without running any evaluation."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agentguard.continuous_comparison import compare_model_prompt_runs, write_model_prompt_comparison
from src.agentguard.lineage import read_document


def render(report):
    def value(item):
        return "UNAVAILABLE" if item is None else format(item, ".4g")
    print("MODEL / PROMPT REGRESSION")
    for side in ("baseline", "candidate"):
        variant = report["variant_identity"][side]
        print(f'\n{side.upper()} {report[side + "_label"] or ""}\n  Run: {report[side + "_run_id"]}')
        print("  Models: " + ", ".join(role + "=" + model["resolved"] for role, model in variant["production_models"].items()))
        print(f'  Prompt FP: {variant["prompt_fingerprint"]}\n  Gate: {report[side + "_gate_decision"]}')
        for check in report["gate_context"][side]["checks"]:
            if check["passed"] is False:
                print(f'  Failed: {check["metric"]}: {value(check["actual"])} {check["comparison"]} {value(check["threshold"])}')
    print("\nCHANGE\n  " + report["variant_identity"]["change_type"])
    print("\nMETRICS (descriptive; no performance qualification)")
    unavailable = 0
    for row in report["metric_comparisons"]:
        if row["baseline_value"] is None and row["candidate_value"] is None:
            unavailable += 1
            continue
        print(f'  {row["population"]}/{row["metric_name"]}: {value(row["baseline_value"])} -> {value(row["candidate_value"])} {row["unit"]}  {row["comparison_status"]}'
              + (f' ({row["reason"]})' if row["reason"] else ""))
    if unavailable: print(f"  {unavailable} metric/population rows lack values; see artifact for reasons.")
    for name in ("new_failures", "resolved_failures", "unchanged_failures"):
        print("\n" + name.replace("_", " ").upper())
        rows = report["scenario_regressions"][name]
        if not rows: print("  none" if report["scenario_regressions"]["available"] else "  unavailable")
        for row in rows: print(f'  {row["scenario_id"]} repetition={row["repetition"]} {row["metric_name"]}')
    print("\nCOMPARABILITY")
    for dimension in ("quality", "semantic", "safety", "operational"):
        print(f'  {dimension}: {report["comparability"][dimension]}')
    print("  Limitations: " + (", ".join(report["comparability"]["limitations"]) or "none"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", required=True)
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--baseline-label")
    parser.add_argument("--candidate-label")
    args = parser.parse_args()
    try:
        report = compare_model_prompt_runs(ROOT, baseline_run_id=args.baseline_run, candidate_run_id=args.candidate_run,
                                           baseline_label=args.baseline_label, candidate_label=args.candidate_label)
        path = write_model_prompt_comparison(ROOT, report)
        render(read_document(path))
        print("\nArtifact: " + str(path))
    except (ValueError, OSError, KeyError, TypeError):
        parser.exit(2, "Unable to compare validated completed runs; check IDs, completion, integrity, and safe labels.\n")


if __name__ == "__main__": main()
