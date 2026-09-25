"""Diagnostic only: profile functional smoke production runs without eval judges.

Observes the existing Runner entry point without altering arguments, models,
prompts, tools, retries, or the execute_scenario path. Output is gitignored.
"""

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
from statistics import fmean
import sys
import time
from unittest.mock import patch
from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import Runner
from agents.agent_output import AgentOutputSchema
from agents.models.default_models import get_default_model
from src.agentguard.datasets import load_dataset
from src.agentguard.evaluation_record import execute_scenario

USAGE_FIELDS = ("requests", "input_tokens", "output_tokens", "total_tokens")


def usage_snapshot(usage):
    """Copy immediately: the production wrapper later adds router usage in place."""
    return {name: getattr(usage, name, None) for name in USAGE_FIELDS}


def component_snapshot(agent, result, latency_ms):
    usage = usage_snapshot(result.context_wrapper.usage)
    responses = result.raw_responses
    return {
        "component": ("router" if agent.name == "Capability Router" else
                      "planning_recovery" if agent.name == "Planning Completeness Reviewer" else
                      "agent" if agent.name == "AgentGuard Support Agent" else "other"),
        "run_invocations": 1,
        "model": agent.model if isinstance(agent.model, str) else
                 get_default_model() if agent.model is None else type(agent.model).__name__,
        "latency_ms": latency_ms,
        **usage,
        "model_responses": len(responses),
        "runner_visible_failed_attempts": (
            max(0, usage["requests"] - len(responses)) if usage["requests"] is not None else None
        ),
        "response_usage": [TypeAdapter(type(response.usage)).dump_python(response.usage, mode="json")
                           for response in responses],
        "context_characters": {
            "instructions": len(agent.instructions) if isinstance(agent.instructions, str) else None,
            "tool_definitions": len(json.dumps([
                {"name": tool.name, "description": tool.description,
                 "parameters": tool.params_json_schema} for tool in agent.tools
            ], ensure_ascii=False)) if agent.tools else 0,
            "output_schema": len(json.dumps(AgentOutputSchema(agent.output_type).json_schema()))
                             if agent.output_type else 0,
        },
    }


def profile_scenario(scenario, *, measure_tools=False, diagnostics=None):
    components = []
    tool_timings = []
    if diagnostics is not None:
        diagnostics.update(components=components, tool_timings=tool_timings)
    original_run = Runner.run_sync

    def timed_tool(tool):
        original_invoke = tool.on_invoke_tool

        async def invoke(*args, **kwargs):
            started = time.perf_counter()
            event = {"tool": tool.name, "status": "completed"}
            try:
                return await original_invoke(*args, **kwargs)
            except BaseException as error:
                event.update(status="failed", error_type=type(error).__name__)
                raise
            finally:
                event["latency_ms"] = (time.perf_counter() - started) * 1000
                tool_timings.append(event)

        return invoke

    def observe(agent, *args, **kwargs):
        with ExitStack() as stack:
            if measure_tools:
                for tool in agent.tools:
                    stack.enter_context(patch.object(tool, "on_invoke_tool", timed_tool(tool)))
            started = time.perf_counter()
            try:
                result = original_run(agent, *args, **kwargs)
            except BaseException as error:
                components.append({"component": agent.name, "status": "failed",
                                   "latency_ms": (time.perf_counter() - started) * 1000,
                                   "error_type": type(error).__name__})
                raise
            elapsed = (time.perf_counter() - started) * 1000
        components.append(component_snapshot(agent, result, elapsed))
        return result

    # Single-threaded diagnostic scope only; requests are forwarded unchanged.
    with patch.object(Runner, "run_sync", side_effect=observe):
        record = execute_scenario(scenario)
    if measure_tools and record.execution is not None:
        # Mandatory reads now execute before the answer Runner. Keep their
        # measured runtime latency alongside any legacy SDK tool timings.
        tool_timings[:0] = [
            {"tool": item["operation"]["tool"], "status": item["status"],
             "latency_ms": item["latency_ms"], "source": "runtime",
             "error_type": item["error_type"]}
            for item in record.execution["operations"] if item["latency_ms"] is not None
        ]
    if record.execution_error is not None:
        if diagnostics is not None:
            diagnostics["evaluation_record"] = asdict(record)
        # Qualification must not count a captured execution failure as a
        # successful low-latency observation (for example, no answer model ran).
        raise RuntimeError("Production execution failed")
    totals = {
        name: sum(component[name] for component in components)
        if all(component[name] is not None for component in components) else None
        for name in USAGE_FIELDS
    }
    record_usage = {"requests": record.request_count, "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens, "total_tokens": record.total_tokens}
    if totals != record_usage:
        raise ValueError("Observed component usage does not reconcile with EvaluationRecord")
    return {
        "scenario_id": record.scenario_id,
        "latency_ms": record.latency_ms,
        "production_usage": record_usage,
        "components": components,
        "tool_timings": tool_timings if measure_tools else None,
        "tool_calls": record.tool_calls,
        # Preserve the captured execution for offline checks without another run.
        "evaluation_record": asdict(record),
        "tool_output_characters": len(json.dumps(record.tool_outputs, ensure_ascii=False)),
        "application_reruns": 0,
        "http_retry_count": None,
        "retry_tokens": None,
        "usage_reconciled": True,
        "evaluation_usage": {"model_calls": 0, "total_tokens": 0},
    }


def save_report(path, report):
    report["scenarios"].sort(key=lambda row: row["production_usage"]["total_tokens"] or 0, reverse=True)
    rows = report["scenarios"]
    report["scenarios_completed"] = len(rows)
    report["average_tokens_per_run"] = fmean(row["production_usage"]["total_tokens"] for row in rows) if rows else None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/token_audit_smoke.json")
    parser.add_argument("--resume", action="store_true", help="Retain saved rows and execute only missing rows.")
    parser.add_argument("--exclude-scenario", action="append", default=[], help="Leave a scenario unexecuted.")
    args = parser.parse_args(argv)
    if args.output.exists() and not args.resume:
        parser.error("Output already exists; choose another path to avoid overwriting prior evidence.")
    scenarios = load_dataset(PROJECT_ROOT / "evals/datasets/functional.json", dataset_type="functional", suite="smoke")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "suite": "functional smoke",
        "sdk_versions": {package: version(package) for package in ("openai-agents", "openai")},
        "scenarios_requested": len(scenarios),
        "complete": False,
        "limitations": [
            "Fresh run; cannot reconstruct the historical aggregate.",
            "SDK usage, not transport instrumentation; HTTP retries and their tokens unavailable.",
            "SDK zero token defaults cannot distinguish missing provider usage from measured zero.",
            "Character counts describe context size, not exact token attribution within a request.",
            "Latency includes the small overhead of diagnostic snapshots.",
        ],
        "scenarios": [],
    }
    if args.resume:
        report = json.loads(args.output.read_text(encoding="utf-8"))
    save_report(args.output, report)
    completed = {row["scenario_id"] for row in report["scenarios"]}
    for scenario in scenarios:
        if scenario["id"] in completed or scenario["id"] in args.exclude_scenario:
            continue
        print(f"Profiling {scenario['id']} (one production execution)", flush=True)
        try:
            row = profile_scenario(scenario)
        except Exception as error:
            # Never serialize exception text: provider errors may contain credentials.
            report["error"] = {"scenario_id": scenario["id"], "type": type(error).__name__}
            save_report(args.output, report)
            print(f"Stopped: {type(error).__name__}. No scenario retried.", flush=True)
            return 1
        report["scenarios"].append(row)
        save_report(args.output, report)
        print(f"  production tokens={row['production_usage']['total_tokens']}; "
              f"requests={row['production_usage']['requests']}", flush=True)
    report["complete"] = len(report["scenarios"]) == len(scenarios)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_report(args.output, report)
    print(f"Saved {len(report['scenarios'])} scenarios to {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
