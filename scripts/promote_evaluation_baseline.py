"""Explicit promotion of an exact validated evaluation; no live calls or Git commits."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.baselines import promote, BaselineError


def main(argv=None, *, project_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id")
    source.add_argument("--invocation-id")
    parser.add_argument("--suite", default="smoke", choices=("smoke", "full"))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--promotion-policy", type=Path)
    parser.add_argument("--promoted-by-mode", choices=("MANUAL", "CI_APPROVED"), default="MANUAL")
    args = parser.parse_args(argv)
    try:
        result = promote(project_root or ROOT, suite=args.suite, reason=args.reason, run_id=args.run_id,
                         invocation_id=args.invocation_id, policy_path=args.promotion_policy, promoted_by_mode=args.promoted_by_mode)
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Only our fixed reason codes are printable; never echo filesystem/provider text.
        code = str(error) if isinstance(error, BaselineError) else type(error).__name__
        print(f"PROMOTION: REJECTED ({code})")
        return 1
    print(f"PROMOTION: CREATED baseline={result['baseline_id']} source_run={result['source_run_id']}")
    print(f"DESCRIPTOR: baselines/{args.suite}/baseline.json")
    print(f"SNAPSHOT DIGEST: {result['snapshot_digest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
