"""Read-only synthesis-tail analysis. Imports no runtime, SDK, or evaluator."""
from collections import Counter, defaultdict
from math import isfinite
import re

from src.agentguard.performance import distribution

CANDIDATES = {"S6": 6000, "S8": 8000, "S10": 10000, "S12": 12000, "REMAINING": None}
STAGES = ("primary_router", "recovery_planner", "required_operations", "synthesis")
PROVIDER_FAILURES = {"PROVIDER_ERROR", "RATE_LIMIT", "NETWORK_ERROR", "TIMEOUT",
                     "AUTHENTICATION_FAILURE", "AUTHORIZATION_FAILURE", "UNKNOWN_EXTERNAL_FAILURE"}


def number(value):
    return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None


def flag(value):
    return value if type(value) is bool else None


def identity(value):
    # Only opaque telemetry identities; never copy messages or arbitrary text.
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
            and not re.search(r"(?:sk|rk|pk)-|Bearer|secret|password", value, re.I)):
        return value
    return None


def normalize(row):
    """Project current audit telemetry, 14A-R evidence, or historical measurements."""
    raw = row.get("production_telemetry") or {}
    failure = row.get("failure_evidence") or {}
    spans = raw.get("component_spans") or []
    budgets = spans or failure.get("stage_budgets") or row.get("stage_budgets") or []
    synthesis = [s for s in budgets if s.get("component") == "synthesis"]
    synth = synthesis[0] if len(synthesis) == 1 else {}
    latencies = {}
    legacy = row.get("latency_ms") if isinstance(row.get("latency_ms"), dict) else {}
    for component in STAGES:
        source = "required_execution" if component == "required_operations" else component
        values = [number(s.get("duration_ms")) for s in spans if s.get("component") == source]
        latencies[component] = (sum(values) if values and all(v is not None for v in values)
                                else number(failure.get("component_latency_ms", {}).get(component, legacy.get(component))))
    historical_synthesis_kind = None
    for c in row.get("components", []):
        if c.get("component") == "router" and latencies["primary_router"] is None:
            latencies["primary_router"] = number(c.get("latency_ms"))
        if c.get("component") == "agent" and latencies["synthesis"] is None:
            # Old agent timing can include tool loops; never relabel those as synthesis.
            if c.get("context_characters", {}).get("tool_definitions") == 0 and c.get("model_responses") == 1:
                latencies["synthesis"] = number(c.get("latency_ms"))
                historical_synthesis_kind = "legacy_tool_free_single_response_agent"
            else:
                historical_synthesis_kind = "legacy_agent_includes_or_may_include_tool_loop_excluded"
    total = number(raw.get("total_latency_ms"))
    if total is None:
        total = number(failure.get("total_latency_ms", legacy.get("total_request", row.get("latency_ms"))))
    tokens = raw.get("observed_usage") or failure.get("production_tokens") or row.get("tokens", {}).get("production_total") or row.get("production_usage") or {}
    models = [identity(s.get("resolved_model")) for s in spans]
    models += [identity(m.get("model")) for m in failure.get("observed_model_identities", [])]
    models += [identity(m) for m in row.get("models", [])]
    # Historical component identities are observed profiler metadata, not inferred defaults.
    models += [identity(c.get("model")) for c in row.get("components", [])]
    completed = row.get("status") == "completed" or row.get("completed") is True
    result = dict(
        scenario_id=identity(row.get("scenario_id")), request_id=identity(raw.get("request_id", failure.get("request_id"))),
        repetition=row.get("repetition") if type(row.get("repetition")) is int else None,
        run_id=identity(row.get("run_id", failure.get("run_id"))),
        manifest_digest=identity(row.get("manifest_digest", failure.get("manifest_digest"))),
        status="completed" if completed else "incomplete",
        component_latency_ms=latencies, total_request_ms=total,
        stage_budgets=[dict(component=identity(s.get("component")),
                            configured_stage_cap_ms=number(s.get("configured_stage_cap_ms")),
                            allocated_allowance_ms=number(s.get("allocated_allowance_ms"))) for s in budgets],
        remaining_request_budget_before_synthesis_ms=number(synth.get("remaining_budget_before_ms")),
        configured_synthesis_allowance_ms=number(synth.get("configured_stage_cap_ms")),
        allocated_synthesis_allowance_ms=number(synth.get("allocated_allowance_ms")),
        request_deadline_ms=number(raw.get("deadline_budget_ms", failure.get("request_deadline_ms", row.get("request_deadline_ms")))),
        overall_deadline_exhausted=flag(raw.get("deadline_exhausted", failure.get("deadline_exhausted", row.get("request_deadline_exhausted")))),
        late_completion=flag(synth.get("late_completion", failure.get("late_completion"))),
        result_abandoned=flag(raw.get("result_abandoned", failure.get("result_abandoned", row.get("result_abandoned")))),
        synthesis_result_accepted=flag(synth.get("result_accepted")),
        failure_component=identity(raw.get("terminal_failure_component", failure.get("failure_component", row.get("failure_component")))),
        failure_category=identity(raw.get("terminal_failure_category", failure.get("failure_category", row.get("failure_category")))),
        retry_attempts_total=number(raw.get("retry_attempts_total", failure.get("retry_attempts_total", row.get("actual_extra_attempts")))),
        lower_layer_retries_configured=flag(failure.get("lower_layer_retries_configured")),
        production_tokens={k: number(tokens.get(k)) for k in ("input_tokens", "output_tokens", "total_tokens")},
        usage_completeness=identity(raw.get("usage_completeness", failure.get("usage_completeness", row.get("usage_completeness")))),
        model_identities=sorted(set(m for m in models if m)), historical_synthesis_kind=historical_synthesis_kind,
    )
    configured = [flag(s.get("lower_layer_retries_configured")) for s in spans
                  if s.get("component") in {"primary_router", "recovery_planner", "synthesis"}]
    if configured:
        result["lower_layer_retries_configured"] = True if True in configured else False if all(v is False for v in configured) else None
    result["classification"] = classify(result)
    return result


def classify(row):
    if row.get("status") == "completed":
        return None
    total, deadline = number(row.get("total_request_ms")), number(row.get("request_deadline_ms"))
    if row.get("overall_deadline_exhausted") is True or (total is not None and deadline is not None and total >= deadline):
        return "OVERALL_REQUEST_DEADLINE"
    component = row.get("failure_component")
    duration = row.get("component_latency_ms", {}).get(component)
    caps = [s.get("configured_stage_cap_ms") if s.get("configured_stage_cap_ms") is not None else s.get("allocated_allowance_ms")
            for s in row.get("stage_budgets", []) if s.get("component") == component]
    cap = number(caps[0]) if len(caps) == 1 else None
    if cap is None and component == "synthesis":
        cap = number(row.get("configured_synthesis_allowance_ms"))
    if (component in STAGES and row.get("failure_category") == "DEADLINE_EXHAUSTED"
            and row.get("overall_deadline_exhausted") is False and total is not None and deadline is not None
            and total < deadline and duration is not None and cap is not None and duration >= cap):
        return "STAGE_ALLOWANCE_EXCEEDED"
    if row.get("failure_category") in PROVIDER_FAILURES:
        return "PROVIDER_FAILURE"
    retries = number(row.get("retry_attempts_total"))
    if retries is not None and retries > 0:
        return "RETRY_AMPLIFICATION"
    return "UNKNOWN"


def summarize(rows):
    result = {"request_count": len(rows), "distributions_ms": {}, "exceedances": {},
              "failure_classes": dict(Counter(r["classification"] for r in rows if r["classification"])),
              "retry_evidence": dict(known_zero=sum(r.get("retry_attempts_total") == 0 for r in rows),
                  extra_attempts_observed=sum((r.get("retry_attempts_total") or 0) > 0 for r in rows),
                  unknown=sum(r.get("retry_attempts_total") is None for r in rows))}
    for component in ("primary_router", "synthesis", "total_request"):
        values = [r["total_request_ms"] if component == "total_request" else r["component_latency_ms"][component] for r in rows]
        known = [v for v in values if v is not None]
        stats = distribution(known)
        # Existing nearest-rank convention; no meaningful P99 estimate below N=100.
        stats["p99"] = stats.get("p99") if len(known) >= 100 and component != "total_request" else None
        stats["missing"] = len(values) - len(known)
        result["distributions_ms"][component] = stats
        limits = (6000, 8000, 10000, 12000) if component == "synthesis" else (7500, 10000, 15000, 20000) if component == "total_request" else ()
        result["exceedances"][component] = {str(limit): sum(v > limit for v in known) for limit in limits}
    result["limitations"] = ["Descriptive observed durations, including failed/censored attempts; not uncensored provider latency.",
        "N=40 cannot establish a stable tail or statistical certainty; P99 suppressed below N=100.",
        "Missing durations excluded from percentiles with explicit counts; never replaced by zero."]
    return result


def replay(rows):
    candidates = {}
    for name, cap in CANDIDATES.items():
        counts = Counter()
        accepted_latencies = []
        decisions = []
        for row in rows:
            duration = number(row["component_latency_ms"].get("synthesis"))
            total = number(row.get("total_request_ms"))
            remaining = number(row.get("remaining_request_budget_before_synthesis_ms"))
            returned = (row.get("status") == "completed" or row.get("synthesis_result_accepted") is True
                        or (row.get("late_completion") is True and row.get("failure_category") == "DEADLINE_EXHAUSTED"
                            and row.get("failure_component") == "synthesis"))
            eligible_failure = row.get("classification") in {None, "STAGE_ALLOWANCE_EXCEEDED", "OVERALL_REQUEST_DEADLINE"}
            if (not returned or not eligible_failure or row.get("retry_attempts_total") != 0
                    or row.get("lower_layer_retries_configured") is not False
                    or row.get("request_deadline_ms") != 20000
                    or duration is None or total is None or remaining is None or remaining > 20000):
                decision = "unknown_or_not_replayable"
            elif row.get("overall_deadline_exhausted") is True or total >= 20000 or duration >= remaining:
                decision = "overall_deadline_rejected"
            elif cap is not None and duration >= cap:
                decision = "stage_cap_rejected"
            else:
                decision = "accepted"
                accepted_latencies.append(total)
            counts[decision] += 1
            decisions.append(dict(scenario_id=row["scenario_id"], request_id=row["request_id"], repetition=row["repetition"], decision=decision))
        evaluated = sum(counts[k] for k in ("accepted", "stage_cap_rejected", "overall_deadline_rejected"))
        candidates[name] = dict(synthesis_cap_ms=cap, request_deadline_ms=20000,
            accepted_results=counts["accepted"], stage_cap_rejected_results=counts["stage_cap_rejected"],
            overall_deadline_rejected_results=counts["overall_deadline_rejected"],
            late_result_count=counts["stage_cap_rejected"] + counts["overall_deadline_rejected"],
            unknown_or_not_replayable=counts["unknown_or_not_replayable"],
            acceptance_rate=counts["accepted"] / len(rows) if rows and evaluated == len(rows) else None,
            evaluable_acceptance_rate=counts["accepted"] / evaluated if evaluated else None,
            max_accepted_total_latency_ms=max(accepted_latencies) if accepted_latencies else None, decisions=decisions)
    return dict(candidates=candidates, limitations=[
        "Timing-only counterfactual on observed returned results, not a forecast or semantic/safety certification.",
        "Keep measured trajectories/durations fixed; no inferred completions for provider errors, timeouts or missing telemetry.",
        "Require known zero retries, lower-layer retries disabled, and measured remaining budget; never infer remaining budget from total minus synthesis.",
        "Equality exhausts a budget. Overall-deadline rejection takes precedence over stage-cap rejection.",
        "Final local acceptance overhead for abandoned responses may be missing; apparent acceptance is timing eligibility only."])


def historical_populations(named_reports):
    populations = []
    for name, report in named_reports:
        groups = defaultdict(list)
        for row in report.get("observations", []):
            # Keep candidates, controlled fixtures, and functional/safety populations apart.
            key = (row.get("dataset", "functional" if report.get("suite") == "functional smoke" else "UNKNOWN"),
                   row.get("source", "legacy_unspecified"), row.get("candidate", "UNKNOWN"))
            groups[key].append(normalize(row))
        for (dataset, source, candidate), rows in groups.items():
            populations.append(dict(artifact=name, dataset=dataset, source=source, candidate=candidate,
                scenario_ids=sorted(set(r["scenario_id"] for r in rows if r["scenario_id"])),
                repetitions=report.get("repetitions"), summary=summarize(rows),
                model_identities=sorted({model for r in rows for model in r["model_identities"]}),
                sdk_versions={k: identity(v) for k, v in report.get("sdk_versions", {}).items()
                              if k in {"openai", "openai-agents", "python", "httpx", "httpx2"}},
                policy_evidence=dict(execute_retries=flag(report.get("execute_retries")),
                                     enforce_candidate_budget=flag(report.get("enforce_candidate_budget"))),
                known_synthesis_tails=[dict(scenario_id=r["scenario_id"], synthesis_ms=r["component_latency_ms"]["synthesis"],
                                           total_request_ms=r["total_request_ms"])
                    for r in sorted((r for r in rows if r["component_latency_ms"]["synthesis"] is not None),
                                    key=lambda r: r["component_latency_ms"]["synthesis"], reverse=True)[:3]],
                comparison="Descriptive only; population, SDK, policy and lineage equivalence must be established before causal comparison.",
                legacy_stage_mapping=sorted(set(r["historical_synthesis_kind"] for r in rows if r["historical_synthesis_kind"]))))
    return populations
