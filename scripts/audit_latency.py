"""Measure repeated functional smoke production executions without judge calls."""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
from statistics import median
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_token_usage import profile_scenario
from src.agentguard.datasets import load_dataset
from src.agentguard.quality_gate import load_quality_gate_config
from src.agentguard.performance import distribution, qualify_performance


def summarize(observations, threshold):
    finished = [row for row in observations if row["status"] != "running"]
    completed = [row for row in finished if row["status"] == "completed"]
    result = {
        "attempts_finished": len(finished), "completed": len(completed),
        "failed": len(finished) - len(completed),
        "all_attempt_latency_ms": distribution([row["latency_ms"] for row in finished]),
        "latency_ms": distribution([row["latency_ms"] for row in completed]),
        "tokens": distribution([row["production_usage"]["total_tokens"] for row in completed
                                if row["production_usage"]["total_tokens"] is not None]),
        "by_scenario": {}, "outliers": [],
    }
    for scenario_id in dict.fromkeys(row["scenario_id"] for row in observations):
        rows = [row for row in completed if row["scenario_id"] == scenario_id]
        result["by_scenario"][scenario_id] = {
            "latency_ms": distribution([row["latency_ms"] for row in rows]),
            "tokens": distribution([row["production_usage"]["total_tokens"] for row in rows
                                    if row["production_usage"]["total_tokens"] is not None]),
            "failed": sum(row["status"] == "failed" and row["scenario_id"] == scenario_id for row in finished),
        }
    for row in finished:
        if row["latency_ms"] <= threshold:
            continue
        peers = [peer for peer in completed if peer["scenario_id"] == row["scenario_id"]
                 and peer["latency_ms"] <= threshold]
        usage = row.get("production_usage", {})
        components = row.get("components", [])
        visible = [component.get("runner_visible_failed_attempts") for component in components]
        tool_timings = row.get("tool_timings", [])
        peer_tokens = [peer["production_usage"]["total_tokens"] for peer in peers
                       if peer["production_usage"]["total_tokens"] is not None]
        result["outliers"].append({
            "scenario_id": row["scenario_id"], "repetition": row["repetition"],
            "status": row["status"], "latency_ms": row["latency_ms"],
            "component_latency_ms": {c["component"]: c["latency_ms"] for c in components},
            "tool_invocation_sum_ms": sum(event["latency_ms"] for event in tool_timings),
            "tool_failures": sum(event["status"] == "failed" for event in tool_timings),
            "request_count": usage.get("requests"), "total_tokens": usage.get("total_tokens"),
            "peer_request_counts": sorted({peer["production_usage"]["requests"] for peer in peers
                                            if peer["production_usage"]["requests"] is not None}),
            "peer_median_tokens": median(peer_tokens) if peer_tokens else None,
            "same_trajectory_as_normal_peers": all(row.get("tool_calls") == peer["tool_calls"] for peer in peers) if peers else None,
            "sdk_visible_failed_attempts": sum(visible) if visible and all(v is not None for v in visible) else None,
            "http_retries": "unknown", "provider_delay_cause": "unknown",
        })
    result["exceedance_count"] = len(result["outliers"])
    result["exceedance_rate"] = len(result["outliers"]) / len(finished) if finished else None
    return result


def save_report(path, report):
    report["summary"] = summarize(report["observations"], report["latency_threshold_ms"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None, *, project_root=None):
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/latency_audit_smoke.json")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--qualify", action="store_true", help="Require balanced sampling and enforce the YAML P95 gate")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("Repetitions must be positive")
    if args.qualify and args.repetitions < 5:
        parser.error("Performance qualification requires at least five repetitions")
    if args.output.exists():
        parser.error("Output exists; select a new path to preserve previous evidence")
    scenarios = load_dataset(root / "evals/datasets/functional.json", dataset_type="functional", suite="smoke")
    if args.qualify and len(scenarios) * args.repetitions < 40:
        parser.error("Performance qualification requires at least 40 observations")
    config = load_quality_gate_config(root / "config/quality-gates.yaml")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "suite": "functional smoke",
        "sdk_versions": {name: version(name) for name in ("openai-agents", "openai")},
        "repetitions": args.repetitions, "observations_requested": len(scenarios) * args.repetitions,
        "sampling": "sequential passes in dataset order; no application retries or warmup exclusions",
        "percentiles": "nearest rank: ceil(percentile * count / 100); median separately interpolates the middle pair",
        "latency_threshold_ms": config["quality_gates"]["p95_latency_ms"]["maximum"],
        "complete": False, "observations": [],
        "limitations": [
            "Mandatory tools execute before synthesis. Tool timing includes projection; nested projection timing is not additive.",
            "Tool timing is reused from ExecutionTrace and includes implementation lookup, business execution and projection.",
            "Provider/network/queue time cannot be separated inside a model request; transport retries are unknown.",
            "Latency includes small diagnostic overhead. SDK usage may omit tokens consumed on failed requests.",
            "Five samples per scenario: nearest-rank P95 and P99 equal maximum, not stable tail estimates.",
            "Completed latency and failed attempt durations are both retained; primary latency distribution uses completed executions.",
        ],
    }
    save_report(args.output, report)
    for repetition in range(1, args.repetitions + 1):
        for scenario in scenarios:
            row = {"scenario_id": scenario["id"], "repetition": repetition,
                   "started_at": datetime.now(timezone.utc).isoformat(), "status": "running"}
            report["observations"].append(row)
            save_report(args.output, report)
            print(f"Pass {repetition}/{args.repetitions}: {scenario['id']}", flush=True)
            diagnostics = {}
            started = time.perf_counter()
            try:
                row.update(profile_scenario(scenario, measure_tools=True, diagnostics=diagnostics))
                row["status"] = "completed"
            except Exception as error:
                row.update(diagnostics)
                row.update(status="failed", error_type=type(error).__name__,
                           latency_ms=(time.perf_counter() - started) * 1000,
                           http_retry_count=None, retry_tokens=None)
            save_report(args.output, report)
            print(f"  {row['status']}: {row['latency_ms']:.0f} ms", flush=True)
    report["complete"] = True
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["execution_counts"] = dict(Counter(row["scenario_id"] for row in report["observations"]))
    if args.qualify:
        qualification = qualify_performance(report["observations"], [scenario["id"] for scenario in scenarios],
                                            args.repetitions, report["latency_threshold_ms"])
        report["qualification"] = asdict(qualification)
    save_report(args.output, report)
    if args.qualify:
        stats = report["summary"]["all_attempt_latency_ms"]
        print("\nPERFORMANCE QUALIFICATION")
        print(f"N: {stats['count']}")
        print(f"Failed Executions: {report['summary']['failed']}")
        for key in ("mean", "p50", "p90", "p95", "p99", "min", "max"):
            print(f"{key.upper()}: {stats[key]:.2f} ms")
        print("Percentiles: nearest rank; every execution is retained.")
    else:
        print(json.dumps(report["summary"]["latency_ms"], indent=2), flush=True)
    print(f"Observations >{report['latency_threshold_ms']} ms: {len(report['summary']['outliers'])}", flush=True)
    print(f"Exceedance Rate: {report['summary']['exceedance_rate']:.1%}", flush=True)
    if args.qualify:
        for row in report["summary"]["outliers"]:
            print(f"  {row['scenario_id']} repetition {row['repetition']}: {row['latency_ms']:.2f} ms")
        print(f"P95 Latency {'PASS' if qualification.passed else 'FAIL'}: "
              f"{qualification.p95_latency_ms} ms; required <= {qualification.threshold_ms} ms")
        for failure in qualification.failures:
            print(f"  - {failure}")
        print(f"FINAL DECISION: {'PASS' if qualification.passed else 'FAIL'}")
        return 0 if qualification.passed else 1
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
