"""Production-only reliability qualification; no judges or fault generation."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import version
import json
from math import isfinite
from pathlib import Path
import re
import subprocess
import time

from src.agent import telemetry
from src.agent.request_budget import RequestBudget
from src.agent.retry_policy import ModelRetryPolicy, classify_failure
from src.agent.support_agent import run_support_agent_detailed
from src.agentguard.performance import distribution
from src.agentguard.reliability_policy import evidence_from_attempt, label, shadow_retry


MODEL_COMPONENTS = ("primary_router", "recovery_planner", "synthesis")
LATENCY_COMPONENTS = (*MODEL_COMPONENTS, "policy_planning", "required_operations", "total_request")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
COMPONENTS = {*MODEL_COMPONENTS, "planning_completeness", "policy_resolution", "execution_plan",
              "required_execution", "tool", "projection", "response_acceptance", "request"}


def number(value):
    return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", value) else None


def stats(values):
    """Reuse nearest-rank performance statistics, suppress unsupported tails.

    Minimum N is one expected upper-tail observation: 2/10/20/100 for
    P50/P90/P95/P99. These are descriptive floors, not confidence guarantees.
    """
    values = list(values)
    result = distribution(v for v in values if v is not None)
    result["unavailable_count"] = len(values) - result["count"]
    insufficient = []
    for percentile, minimum in ((50, 2), (90, 10), (95, 20), (99, 100)):
        if result["count"] < minimum:
            result[f"p{percentile}"] = None
            insufficient.append(f"p{percentile}")
    result["insufficient_sample_percentiles"] = insufficient
    return result


def _sum_known(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _attempt(raw, component):
    result = {key: number(raw.get(key)) for key in (
        "attempt_number", "started_offset_ms", "duration_ms", "remaining_budget_before_ms",
        "remaining_budget_after_ms", "sdk_visible_requests", "transport_attempts_observed",
        "provider_status_code", "retry_after_ms", "retry_delay_ms",
    )}
    result.update(component=component,
                  status=choice(raw.get("status"), {"failed", "completed", "running", "not_started"}),
                  failure_category=choice(raw.get("failure_category"), set(telemetry.FailureCategory)),
                  delivery_certainty=choice(raw.get("delivery_certainty"),
                                            {"NOT_SENT", "KNOWN_FAILED", "SENT_OUTCOME_UNKNOWN"}),
                  lower_layer_retries_configured=choice_bool(raw.get("lower_layer_retries_configured")),
                  retry_performed=raw.get("retry_performed") is True,
                  retry_after_invalid=raw.get("retry_after_invalid") is True,
                  late_completion=raw.get("late_completion") is True,
                  result_accepted=choice_bool(raw.get("result_accepted")),
                  result_abandoned=raw.get("result_abandoned") is True,
                  usage_known=raw.get("usage_known") is True,
                  returned_usage={key: number(raw.get("returned_usage", {}).get(key)) for key in TOKEN_FIELDS})
    return result


def choice_bool(value):
    return value if type(value) is bool else None


def validate_enforcement(candidate, enforce, execute_retries):
    if type(enforce) is not bool:
        raise ValueError("Invalid candidate enforcement flag")
    if enforce and (execute_retries or candidate.qualification_budget_policy is None):
        raise ValueError("Budget enforcement requires a stage policy and disabled retries")


def measure_request(scenario, *, candidate, execute_retries=False, enforce_candidate_budget=False,
                    run=None, clock=time.perf_counter):
    """Execute once. Extract only measurements; never retain prompts or results."""
    run = run or run_support_agent_detailed
    validate_enforcement(candidate, enforce_candidate_budget, execute_retries)
    budget = RequestBudget(candidate.request_budget_ms if enforce_candidate_budget else None)
    budget_options = ({"qualification_budget_policy": candidate.qualification_budget_policy}
                      if enforce_candidate_budget else {})
    started = clock()
    evidence = None
    try:
        result = run(scenario["input"], request_budget=budget,
                     **budget_options,
                     retry_policy=candidate.retry_policy if execute_retries else ModelRetryPolicy.disabled())
    except Exception as error:
        raw = telemetry.snapshot(getattr(error, "production_telemetry", None)) or {}
        status = "failed"
        component = raw.get("terminal_failure_component", "request")
        evidence = classify_failure(error, component)
    else:
        raw = telemetry.snapshot(getattr(result.context_wrapper, "production_telemetry", None)) or {}
        status = "completed"
    elapsed = (clock() - started) * 1000
    row = sanitize_measurement(raw, status=status, elapsed=elapsed)
    row["candidate"] = candidate.name
    row["enforce_candidate_budget"] = enforce_candidate_budget
    if evidence is not None:
        row["failure_category"] = evidence.category.value
        # Default-disabled retries do not classify individual attempts. Enrich
        # only the terminal failed model attempt from the actual public error.
        failed = [a for a in row["attempts"] if a["status"] == "failed"
                  and a["component"] == raw.get("terminal_failure_component")]
        if failed:
            failed[-1].update(failure_category=evidence.category.value, delivery_certainty=evidence.delivery.value,
                              provider_status_code=evidence.status_code, retry_after_ms=evidence.retry_after_ms,
                              retry_after_invalid=evidence.guidance_invalid)
    return row


def sanitize_measurement(raw, *, status, elapsed):
    """Explicit schema projection, including nested attempts. No raw snapshots."""
    spans = [s for s in raw.get("component_spans", []) if s.get("component") in COMPONENTS]
    durations = {component: _sum_known(number(s.get("duration_ms")) for s in spans
                                     if s["component"] == component) for component in COMPONENTS}
    # planning_completeness includes recovery; subtract that nested duration.
    planning = durations["planning_completeness"]
    if planning is not None:
        planning = max(0, planning - (durations["recovery_planner"] or 0))
    latencies = {component: durations[component] for component in MODEL_COMPONENTS}
    latencies.update(policy_planning=_sum_known([planning, durations["policy_resolution"], durations["execution_plan"]]),
                     required_operations=durations["required_execution"],
                     total_request=number(raw.get("total_latency_ms")) if raw.get("total_latency_ms") is not None else elapsed)
    attempts = [_attempt(a, s["component"]) for s in spans for a in s.get("attempts", [])
                if s["component"] in MODEL_COMPONENTS]
    attempts.sort(key=lambda a: a["started_offset_ms"] or 0)
    required = raw.get("required_operation_summary", {}).get("required")
    recovery = choice_bool(raw.get("planning_summary", {}).get("recovery_triggered"))
    models = sorted({name for s in spans if (name := model_name(s.get("resolved_model"))) is not None})
    configured_models = sorted({name for s in spans if (name := model_name(s.get("configured_model"))) is not None})
    tokens = {component: {key: _sum_known(number(s.get(key)) for s in spans if s["component"] == component)
                          for key in TOKEN_FIELDS} for component in MODEL_COMPONENTS}
    tokens["production_total"] = {key: number(raw.get("observed_usage", {}).get(key)) for key in TOKEN_FIELDS}
    recovery_span = next((s for s in spans if s["component"] == "recovery_planner"), {})
    recovery_admission = raw.get("recovery_admission") or {}
    return {
        "status": status, "failure_category": choice(raw.get("terminal_failure_category"), set(telemetry.FailureCategory)),
        "failure_component": choice(raw.get("terminal_failure_component"), COMPONENTS),
        "latency_ms": latencies, "tokens": tokens, "models": models,
        "configured_models": configured_models, "attempts": attempts,
        "recovery_triggered": recovery, "recovery_executed": any(a["component"] == "recovery_planner" for a in attempts),
        "remaining_budget_before_recovery_ms": number(recovery_span.get("remaining_budget_before_ms")),
        "required_operation_count": len(required) if isinstance(required, list) else None,
        "usage_completeness": choice(raw.get("usage_completeness"), set(telemetry.UsageCompleteness)) or "UNAVAILABLE",
        "unknown_usage_attempt_count": sum(not a["usage_known"] for a in attempts),
        "observation_incomplete": not raw or raw.get("observation_incomplete") is True,
        "actual_extra_attempts": number(raw.get("retry_attempts_total")),
        "cancelled": raw.get("cancellation_requested") is True or raw.get("cancellation_observed") is True,
        "cancellation_requested": raw.get("cancellation_requested") is True,
        "cancellation_observed": raw.get("cancellation_observed") is True,
        "result_abandoned": raw.get("result_abandoned") is True,
        "request_deadline_ms": number(raw.get("deadline_budget_ms")),
        "request_deadline_exhausted": raw.get("deadline_exhausted") is True,
        "completed_operation_count": _operation_count(raw, "completed"),
        "incomplete_operation_count": _operation_count(raw, "incomplete"),
        "recovery_admission": {
            **{k: number(recovery_admission.get(k)) for k in
               ("remaining_budget_ms", "required_downstream_reserve_ms", "minimum_work_ms", "allowance_ms")},
            "admitted": choice_bool(recovery_admission.get("admitted")),
            "denial_reason": choice(recovery_admission.get("denial_reason"), set(telemetry.AdmissionDenial)),
        },
        "stage_budgets": [_stage_measurement(s) for s in spans if s["component"] in MODEL_COMPONENTS],
    }


def _operation_count(raw, state):
    items = raw.get("required_operation_summary", {}).get(state)
    return len(items) if isinstance(items, list) else None


def _stage_measurement(span):
    return {
        "component": span["component"],
        **{k: number(span.get(k)) for k in ("allocated_allowance_ms", "configured_stage_cap_ms",
           "required_downstream_reserve_ms", "remaining_budget_before_ms", "remaining_budget_after_ms")},
        "stage_admitted": choice_bool(span.get("stage_admitted")),
        "result_accepted": choice_bool(span.get("result_accepted")),
        "late_completion": span.get("late_completion") is True,
        "result_abandoned": span.get("result_abandoned") is True,
        "deadline_source": choice(span.get("timeout_deadline_source"),
                                  {"request_budget", "qualification_stage_within_request"}),
        "allowance_exceeded": (span.get("late_completion") is True
                               and span.get("configured_stage_cap_ms") is not None
                               and (number(span.get("duration_ms")) or 0) >= span["configured_stage_cap_ms"]),
    }


def summarize_enforcement(rows):
    rejected = [r for r in rows if r["failure_category"] == "DEADLINE_EXHAUSTED"]
    return {
        "completed_successes": sum(r["status"] == "completed" for r in rows),
        "deadline_rejections": len(rejected),
        "rejection_stages": dict(Counter(r["failure_component"] for r in rejected)),
        "late_completions": sum(s["late_completion"] for r in rows for s in r.get("stage_budgets", [])),
        "abandoned_results": sum(r.get("result_abandoned", False) for r in rows),
        "stage_allowance_exceedances": dict(Counter(s["component"] for r in rows
            for s in r.get("stage_budgets", []) if s["allowance_exceeded"])),
        "recovery_admissions": dict(Counter(str(r.get("recovery_admission", {}).get("admitted")) for r in rows)),
        "rejected_request_usage": {k: _sum_known(r["tokens"]["production_total"][k] for r in rejected) for k in TOKEN_FIELDS},
        "rejected_request_usage_completeness": dict(Counter(r["usage_completeness"] for r in rejected)),
        "rejected_termination_latency_ms": stats(r["latency_ms"]["total_request"] for r in rejected),
        "completed_operations_before_rejection": sum(r.get("completed_operation_count") or 0 for r in rejected),
        "cancellation_requested": sum(r.get("cancellation_requested", False) for r in rows),
        "cancellation_observed": sum(r.get("cancellation_observed", False) for r in rows),
    }


def shadow_observation(row, candidates):
    decisions, consumed = [], 0
    for attempt in row["attempts"]:
        if attempt["status"] == "failed" and attempt["failure_category"]:
            for candidate in candidates:
                end = (None if attempt["started_offset_ms"] is None or attempt["duration_ms"] is None
                       else attempt["started_offset_ms"] + attempt["duration_ms"])
                if candidate.request_budget_ms is not None and end is None:
                    decisions.append({"candidate": candidate.name, "component": attempt["component"],
                                      "shadow_retry_eligible": None, "shadow_retry_admitted": False,
                                      "shadow_retry_denial_reason": "BUDGET_EVIDENCE_UNAVAILABLE",
                                      "shadow_retry_delay_ms": None, "shadow_remaining_budget_ms": None})
                    continue
                remaining = None if candidate.request_budget_ms is None else max(0, candidate.request_budget_ms - end)
                decision = shadow_retry(candidate, evidence_from_attempt(attempt), component=attempt["component"],
                                        remaining_budget_ms=remaining, failed_attempt=attempt["attempt_number"] or 1,
                                        shared_allowance_used=consumed, cancelled=row["cancelled"])
                decision.update(component=attempt["component"], attempt_number=attempt["attempt_number"],
                                observed_retry_performed=attempt["retry_performed"])
                decisions.append(decision)
        consumed += int(attempt["retry_performed"])
    return decisions


def summarize_population(rows, candidates):
    deadlines = {}
    for candidate in candidates:
        values = [r["latency_ms"]["total_request"] for r in rows if r["latency_ms"]["total_request"] is not None]
        exceeded = None if candidate.request_budget_ms is None else sum(v >= candidate.request_budget_ms for v in values)
        deadlines[candidate.name] = {"deadline_ms": candidate.request_budget_ms, "observation_count": len(values),
                                    "exceedance_count": exceeded,
                                    "exceedance_rate": exceeded / len(values) if exceeded is not None and values else None}
    return {
        "count": len(rows), "latency_ms": {key: stats(r["latency_ms"][key] for r in rows) for key in LATENCY_COMPONENTS},
        "tokens": {component: {key: stats(r["tokens"][component][key] for r in rows) for key in TOKEN_FIELDS}
                   for component in (*MODEL_COMPONENTS, "production_total")},
        "unknown_usage_attempt_count": sum(r["unknown_usage_attempt_count"] for r in rows),
        "usage_completeness": dict(Counter(r["usage_completeness"] for r in rows)),
        "attempt_count": sum(len(r["attempts"]) for r in rows),
        "sdk_visible_requests": _sum_known(a["sdk_visible_requests"] for r in rows for a in r["attempts"]),
        "lower_layer_retries_configured": dict(Counter(
            "unknown" if a["lower_layer_retries_configured"] is None else str(a["lower_layer_retries_configured"]).lower()
            for r in rows for a in r["attempts"])),
        "actual_extra_attempts": sum(r["actual_extra_attempts"] or 0 for r in rows),
        "failure_categories": dict(Counter(r["failure_category"] or "UNKNOWN" for r in rows if r["status"] == "failed")),
        "deadline_analysis": deadlines,
    }


def summarize(rows, candidates):
    groups = defaultdict(list)
    for row in rows:
        recovery = {True: "recovery_triggered", False: "no_recovery", None: "recovery_unknown"}[row["recovery_triggered"]]
        count = row["required_operation_count"]
        operations = "unknown_operations" if count is None else "no_operations" if count == 0 else "single_operation" if count == 1 else "multiple_operations"
        outcome = "success" if row["status"] == "completed" else "controlled_failure" if row["source"] == "controlled" else "observed_failure"
        # Disjoint intersections are the comparison unit; no global pooled tail.
        groups[f"{row['dataset']}/{recovery}/{operations}/{outcome}"].append(row)
    probes = [r for r in rows if r["recovery_probe"]]
    recovered = [r for r in rows if r["recovery_executed"]]
    return {
        "population_counts": {key: len(value) for key, value in sorted(groups.items())},
        "populations": {key: summarize_population(value, candidates) for key, value in sorted(groups.items())},
        "recovery": {"probe_observations": len(probes), "probe_recovery_executions": sum(r["recovery_executed"] for r in probes),
                     "observed_executions": len(recovered),
                     "remaining_budget_before_ms": stats(r["remaining_budget_before_recovery_ms"] for r in recovered),
                     "measurement_gap": ("No recovery executions observed; recovery timing is unavailable."
                                         if not recovered else "Recovery samples are insufficient for tail qualification."
                                         if len(recovered) < 100 else None)},
        "shadow_counts": dict(Counter(f"{d['candidate']}/" + ("admitted" if d["shadow_retry_admitted"] else d["shadow_retry_denial_reason"])
                                      for row in rows for d in row["shadow_decisions"])),
    }


def git_commit(root):
    try:
        result = subprocess.run(["git", "-c", f"safe.directory={Path(root).as_posix()}", "rev-parse", "HEAD"],
                                cwd=root, capture_output=True, text=True, timeout=3, check=True)
        commit = result.stdout.strip()
        return commit if re.fullmatch(r"[0-9a-f]{40,64}", commit) else None
    except (OSError, subprocess.SubprocessError):
        return None


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def qualify(config, datasets, *, output, project_root, execute_retries=False, enforce_candidate_budget=False,
            measure=None, source="production"):
    """Sequential dataset passes; one request per row, never rerun for candidates."""
    if type(execute_retries) is not bool or source not in {"production", "controlled"}:
        raise ValueError("Invalid qualification execution mode")
    if execute_retries and not config.execution.retry_policy.enabled:
        raise ValueError("Controlled retries require an enabled execution candidate")
    validate_enforcement(config.execution, enforce_candidate_budget, execute_retries)
    output = Path(output).resolve()
    reports = (Path(project_root) / "reports").resolve()
    if not output.is_relative_to(reports) or output.suffix != ".json" or output.exists():
        raise ValueError("Choose a new JSON report under the project's gitignored reports directory")
    scenarios = [(kind, s) for kind in ("functional", "safety") for s in getattr(datasets, kind)]
    ids = [label(s["id"]) for _, s in scenarios]
    if not ids or len(ids) != len(set(ids)) or set(config.recovery_scenario_ids) - set(ids):
        raise ValueError("Recovery probes must reference selected scenarios; scenario IDs must be unique")
    measure = measure or measure_request
    report = {
        "schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(), "git_commit": git_commit(project_root),
        "suite": "reliability", "configuration": config.snapshot(), "execute_retries": execute_retries,
        "shadow_policy_enabled": True,
        "enforce_candidate_budget": enforce_candidate_budget,
        "effective_budget_policy": (config.execution.snapshot()["qualification_budget_policy"]
                                    if enforce_candidate_budget else None),
        "effective_retry_policy": (config.execution if execute_retries else
                                   type(config.execution)("effective_no_retry")).snapshot()["retry_policy"],
        "sdk_versions": {name: version(name) for name in ("openai-agents", "openai")},
        "retry_owner": "agentguard", "lower_layer_retry_configuration": "disabled_on_production_sdk_path",
        "evaluator_usage": {"judge_calls": 0, "tokens": 0}, "observations_requested": len(scenarios) * config.repetitions,
        "sampling": "Sequential dataset-order passes; no exclusions or whole-request retries.",
        "percentiles": "Nearest rank ceil(p*N/100), shared performance.distribution; descriptive sample floors 2/10/20/100.",
        "shadow_semantics": "Independent decisions at observed failure boundaries using actual prior allowance consumption; no success prediction.",
        "limitations": ["Observed success means runtime completion, not semantic or safety certification.",
                        "Failure durations may be censored by the executed deadline; candidate comparisons are descriptive.",
                        "No inferred provider billing, retry success probability, or production policy recommendation.",
                        "No transport observation is inferred from SDK usage. Small samples cannot establish stable tail latency."],
        "complete": False, "observations": [],
    }
    write_report(output, report)
    for repetition in range(1, config.repetitions + 1):
        for kind, scenario in scenarios:
            report["in_progress"] = {"scenario_id": scenario["id"], "dataset": kind, "repetition": repetition}
            write_report(output, report)
            options = {"enforce_candidate_budget": True} if enforce_candidate_budget else {}
            row = measure(scenario, candidate=config.execution, execute_retries=execute_retries, **options)
            row.update(scenario_id=scenario["id"], dataset=kind, repetition=repetition, source=source,
                       recovery_probe=scenario["id"] in config.recovery_scenario_ids)
            row["shadow_decisions"] = shadow_observation(row, config.candidates)
            report["observations"].append(row)
            report["in_progress"] = None
            write_report(output, report)
    report.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat(),
                  effective_models=sorted({m for r in report["observations"] for m in r["models"]}),
                  configured_models=sorted({m for r in report["observations"] for m in r["configured_models"]}),
                  summary=summarize(report["observations"], config.candidates),
                  enforcement_summary=summarize_enforcement(report["observations"]))
    write_report(output, report)
    return report


def print_summary(report):
    print("RELIABILITY QUALIFICATION (descriptive; no release decision)")
    print(f"Executions: {len(report['observations'])}; actual retries enabled: {report['execute_retries']}")
    print(f"Candidate deadline enforcement: {report.get('enforce_candidate_budget', False)}; "
          f"policy: {report.get('effective_budget_policy')}")
    if report.get("enforce_candidate_budget"):
        print(f"Observed enforcement: {report['enforcement_summary']}")
    for population, summary in report["summary"]["populations"].items():
        latency = summary["latency_ms"]["total_request"]
        print(f"{population}: N={summary['count']}, mean={latency.get('mean')}, max={latency.get('max')}, "
              f"P50={latency.get('p50')}, P90={latency.get('p90')}, P95={latency.get('p95')}, P99={latency.get('p99')}")
        print(f"  Insufficient percentile samples: {latency['insufficient_sample_percentiles']}")
        print(f"  Usage completeness: {summary['usage_completeness']}; unknown attempts: {summary['unknown_usage_attempt_count']}")
        print(f"  Attempts: {summary['attempt_count']}; extra attempts: {summary['actual_extra_attempts']}")
        for component in (*MODEL_COMPONENTS, "production_total"):
            tokens = summary["tokens"][component]["total_tokens"]
            print(f"  {component} tokens: N={tokens['count']}, mean={tokens.get('mean')}, max={tokens.get('max')}")
        for name, deadline in summary["deadline_analysis"].items():
            print(f"  {name} deadline: {deadline['deadline_ms']} ms; exceedances={deadline['exceedance_count']}, rate={deadline['exceedance_rate']}")
    print(f"Recovery executions: {report['summary']['recovery']['observed_executions']}")
    if report["summary"]["recovery"]["measurement_gap"]:
        print(report["summary"]["recovery"]["measurement_gap"])
    print(f"Shadow decisions: {report['summary']['shadow_counts']}; admission does not predict retry success.")
