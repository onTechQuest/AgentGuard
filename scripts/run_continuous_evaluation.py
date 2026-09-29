"""Run the existing evaluation once, then publish advisory baseline comparison."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.invocation import invocation_entry, current_invocation, setup_failure
from src.agentguard.continuous_comparison import compare, write_comparison


@invocation_entry(suite="smoke")
def main(argv=None, *, project_root=None, runner=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--trusted-baseline-dir", type=Path,
                        help="Trusted baselines directory, e.g. from a separate base-ref checkout; required in CI")
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--release-qualification", action="store_true")
    args = parser.parse_args(argv)
    root = Path(project_root or ROOT)
    receipt = current_invocation()
    if runner is None:
        from scripts.run_agentguard_eval import main as runner
    evaluation_args = ["--suite", args.suite]
    if args.release_qualification:
        evaluation_args.append("--release-qualification")
    terminal = None
    try:
        code = runner(evaluation_args)
    except BaseException as error:
        setup_failure(error)
        if isinstance(error, SystemExit):
            code = error.code if type(error.code) is int else 1
            terminal = error
        elif isinstance(error, KeyboardInterrupt) or type(error).__name__ == "CancelledError":
            code = 130
            terminal = error
        elif isinstance(error, Exception):
            code = 1
        else:
            raise
    try:
        result = compare(root, suite=args.suite, receipt=receipt.document, execution_code=code,
                         trusted_baseline_dir=args.trusted_baseline_dir,
                         ci=args.ci or os.environ.get("CI", "").lower() not in {"", "0", "false"})
        destination = write_comparison(root, result)
        receipt.save(baseline_id=result["baseline_id"], comparison_id=result["comparison_id"])
        print(f"BASELINE: {result['baseline_id'] or result['artifact_integrity']['baseline']}")
        print(f"COMPARISON: {result['statuses']['comparison']} (ADVISORY)")
        print(f"COMPARISON ARTIFACT: {destination / 'comparison.json'}")
    except Exception:
        print("COMPARISON: REPORTING_FAILED (ADVISORY)")
    # Comparison never overrides the existing runner's exit decision.
    if terminal is not None:
        raise terminal
    return code


if __name__ == "__main__":
    sys.exit(main())
