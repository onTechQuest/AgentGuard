"""Offline timing replay through the same admission/acceptance boundaries.

No production executor is imported or invoked. Archived reports expose aggregate
stage durations and model-attempt offsets, not every local stage start. Router
starts at zero; recovery/synthesis use their attempt offsets with full stage
durations. Local operation work is placed immediately before synthesis. This
is an explicit timing reconstruction, suitable for equivalent-input checks,
not a prediction of cancellation, cost savings, or unseen boundary behavior.
"""

from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from src.agent.qualification_budget import qualification_execution
from src.agent.request_budget import RequestBudget, RequestBudgetRejected
from src.agent.request_execution import request_execution, stage


class ReplayClock:
    def __init__(self):
        self.ms = 0.0

    def __call__(self):
        return self.ms / 1000

    def advance_to(self, value):
        self.ms = max(self.ms, value)


def replay_observation(row, policy):
    """Replay a complete successful, single-attempt observation without I/O."""
    if row["status"] != "completed" or row["actual_extra_attempts"] != 0:
        raise ValueError("Replay requires completed no-retry observations")
    latency = row["latency_ms"]
    offsets = {a["component"]: a["started_offset_ms"] for a in row["attempts"]}
    if len(offsets) != len(row["attempts"]):
        raise ValueError("Replay requires one attempt per logical component")
    clock = ReplayClock()
    budget = RequestBudget(policy.request_deadline_ms, clock=clock)
    visited, recovery, completed_operations = [], None, 0

    def model(component, start):
        nonlocal recovery
        clock.advance_to(start)
        if component == "recovery_planner":
            recovery = asdict(budget.admission(**policy.requirements(component)))
        with stage(component):
            visited.append(component)
            clock.ms += latency[component]

    @qualification_execution
    @request_execution
    def trajectory(**kwargs):
        nonlocal completed_operations
        model("primary_router", 0)
        if row["recovery_executed"]:
            model("recovery_planner", offsets["recovery_planner"])
        # Preserve known aggregate tool time; exact per-operation offsets were
        # not saved. No payload, tool or SDK call occurs in this replay.
        clock.advance_to(offsets["synthesis"] - (latency["required_operations"] or 0))
        with stage("tool"):
            clock.ms += latency["required_operations"] or 0
            completed_operations = row["required_operation_count"]
        model("synthesis", offsets["synthesis"])
        clock.advance_to(latency["total_request"])
        return SimpleNamespace(context_wrapper=SimpleNamespace(context=None))

    rejection = None
    try:
        trajectory(request_budget=budget, qualification_budget_policy=policy)
    except RequestBudgetRejected as error:
        rejection = {"component": error.component, "phase": error.phase,
                     "late_completion": error.completed, "reason": error.evidence.denial_reason}
    return {
        "accepted": rejection is None, "rejection": rejection,
        "visited_model_stages": visited, "recovery_admission": recovery,
        "replayed_completed_operation_count": completed_operations,
        "replayed_termination_ms": clock.ms, "result_abandoned": budget.result_abandoned,
        "cancellation_requested": budget.cancellation_requested,
        "cancellation_observed": budget.cancellation_observed,
        "retrospective_exceedances": {
            "primary_router": latency["primary_router"] >= policy.router_allowance_ms,
            "synthesis": latency["synthesis"] >= policy.synthesis_allowance_ms,
            "request": latency["total_request"] >= policy.request_deadline_ms,
        },
        "actual_live_calls": 0, "actual_extra_retry_attempts": 0,
    }


def replay_reports(paths, candidates):
    """Preserve each source/population, including the selected tail-probe cohort."""
    results, sources = [], []
    for path in map(Path, paths):
        content = path.read_bytes()
        report = json.loads(content)
        if not report["complete"]:
            raise ValueError("Cannot replay an incomplete qualification report")
        sources.append({"path": path.as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
        for row in report["observations"]:
            population = (f"{row['dataset']}/" + ("recovery_triggered" if row["recovery_executed"]
                          else f"{row['required_operation_count']}_operations"))
            for candidate in candidates:
                results.append({"report": path.name, "population": population,
                                "scenario_id": row["scenario_id"], "repetition": row["repetition"],
                                "candidate": candidate.name,
                                "observed_production_tokens": row["tokens"]["production_total"],
                                **replay_observation(row, candidate.qualification_budget_policy)})
    grouped = defaultdict(list)
    for row in results:
        grouped[(row["candidate"], row["report"], row["population"])].append(row)
    return {
        "milestone": "13C.4F", "mode": "offline_timing_replay", "sources": sources,
        "candidates": [c.snapshot() for c in candidates],
        "actual_live_calls": 0, "actual_extra_retry_attempts": 0,
        "limitations": [
            "Stage starts reconstructed from attempt offsets plus full stage durations; exact local boundaries unavailable.",
            "Required operations retain aggregate timing; individual operation start times cannot be recovered.",
            "Retrospective downstream exceedances remain descriptive after an earlier replay rejection.",
            "Recorded full-request tokens are observations, not counterfactual savings or billing estimates.",
            "Selected tail probe remains separate by source; a single recovery does not qualify a tail distribution.",
        ],
        "populations": [{"candidate": c, "source": s, "population": p, "count": len(rows),
                         "accepted": sum(r["accepted"] for r in rows),
                         "rejected": sum(not r["accepted"] for r in rows)}
                        for (c, s, p), rows in sorted(grouped.items())],
        "candidate_counts": {c.name: dict(Counter("accepted" if r["accepted"] else "rejected"
                                  for r in results if r["candidate"] == c.name)) for c in candidates},
        "observations": results,
    }
