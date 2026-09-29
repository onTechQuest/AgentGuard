"""Compare two local evaluation run directories without executing evaluations."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.lineage import load_run
from src.agentguard.comparability import compare_runs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(compare_runs(load_run(args.before), load_run(args.after)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
