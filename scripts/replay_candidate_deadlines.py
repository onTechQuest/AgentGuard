"""Replay recorded timings only. This command cannot execute a production run."""

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.agentguard.deadline_replay import replay_reports
from src.agentguard.reliability_policy import load_qualification_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-config", type=Path, required=True)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    target = args.output.resolve()
    if not target.is_relative_to(PROJECT_ROOT / "reports") or target.suffix != ".json":
        parser.error("Output must be a new JSON file under reports/")
    config = load_qualification_config(args.qualification_config)
    if any(c.qualification_budget_policy is None for c in config.candidates):
        parser.error("Replay requires stage policies for all candidates")
    report = replay_reports(args.reports, config.candidates)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps(report["candidate_counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
