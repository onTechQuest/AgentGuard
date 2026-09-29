"""Evaluation-only, allowlisted terminal evidence. Never stores exception text."""
from functools import wraps
import re

from src.agentguard.lineage import current_run


def retain_failure(scenario, error=None, *, record=None, stage="execution"):
    try:
        _retain_failure(scenario, error, record=record, stage=stage)
    except Exception:
        # Malformed/unreadable telemetry must not replace the terminal exception
        # or discard its minimal evidence. No runtime objects are modified.
        _retain_failure(scenario, error, stage=stage, use_telemetry=False)


def _retain_failure(scenario, error=None, *, record=None, stage="execution", use_telemetry=True):
    run = current_run()
    if run is None:
        return
    attempt = next((r for r in reversed(run.executed) if r["scenario_id"] == scenario["id"]), None)
    if attempt is None:
        attempt = run.observe(scenario["id"], completed=False)
    if "failure_evidence" in attempt:
        return
    from src.agent import telemetry
    from src.agentguard.reliability import COMPONENTS, MODEL_COMPONENTS, TOKEN_FIELDS, number, choice, choice_bool
    from src.agentguard.safety_incidents import Redactor

    raw = None
    cause, seen = error, set()
    while use_telemetry and cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        attached = getattr(cause, "production_telemetry", None)
        raw = attached if isinstance(attached, dict) else telemetry.snapshot(attached)
        if raw is not None:
            break
        cause = cause.__cause__ or cause.__context__
    if use_telemetry and raw is None and record is not None:
        raw = getattr(record, "production_telemetry", None)
    available = isinstance(raw, dict)
    raw = raw if available else {}
    redactor = Redactor([raw, scenario])

    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
            return None
        return value if redactor.clean(value) == value else None

    raw_spans = raw.get("component_spans")
    spans = [s for s in (raw_spans if isinstance(raw_spans, list) else []) if isinstance(s, dict)
             and s.get("component") in COMPONENTS]

    def aggregate_bool(values):
        values = [choice_bool(v) for v in values]
        return True if True in values else (False if values and all(v is False for v in values) else None)

    component = choice(raw.get("terminal_failure_component"), COMPONENTS)
    if component is None and isinstance(error, telemetry.RequestBudgetRejected):
        component = choice(error.component, COMPONENTS)
    category = choice(raw.get("terminal_failure_category"), set(telemetry.FailureCategory))
    # A scoring exception must not inherit the production request's success/failure category.
    if error is not None and (category is None or stage != "execution"):
        category = telemetry.exception_category(error, component or "request").value
    latencies = {}
    for name in (*MODEL_COMPONENTS, "required_operations"):
        source = "required_execution" if name == "required_operations" else name
        values = [number(s.get("duration_ms")) for s in spans if s["component"] == source]
        latencies[name] = sum(v for v in values if v is not None) if any(v is not None for v in values) else None
    usage = raw.get("observed_usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = {k: number(usage.get(k)) for k in TOKEN_FIELDS}
    if record is not None:
        tokens = {k: value if value is not None else number(getattr(record, k, None)) for k, value in tokens.items()}
    usage_completeness = choice(raw.get("usage_completeness"), set(telemetry.UsageCompleteness))
    if usage_completeness is None:
        usage_completeness = "UNKNOWN" if any(v is not None for v in tokens.values()) else "UNAVAILABLE"
    latency = number(raw.get("total_latency_ms"))
    if latency is None and record is not None:
        latency = number(getattr(record, "latency_ms", None))
    evidence = dict(
        **run.reference, scenario_id=scenario["id"], completed=False,
        evaluation_stage=stage, telemetry_available=available,
        request_id=identifier(raw.get("request_id")),
        exception_type=identifier(type(error).__name__) if error is not None else identifier(raw.get("terminal_exception_type")),
        terminal_exception_type=identifier(raw.get("terminal_exception_type")),
        failure_category=category or "UNKNOWN", failure_component=component,
        terminal_status=choice(raw.get("terminal_status"), {"failed", "completed", "cancelled"}),
        total_latency_ms=latency,
        request_deadline_ms=number(raw.get("deadline_budget_ms")),
        deadline_exhausted=choice_bool(raw.get("deadline_exhausted")),
        deadline_exhaustion_stage=component if raw.get("deadline_exhausted") is True or category == "DEADLINE_EXHAUSTED" else None,
        late_completion=aggregate_bool([s.get("late_completion") for s in spans]),
        result_abandoned=choice_bool(raw.get("result_abandoned")),
        remote_outcome_unknown=choice_bool(raw.get("remote_outcome_unknown")),
        retry_policy_enabled=choice_bool(raw.get("retry_policy_enabled")),
        retry_attempts_total=number(raw.get("retry_attempts_total")),
        lower_layer_retries_configured=aggregate_bool([s.get("lower_layer_retries_configured") for s in spans
                                                      if s["component"] in MODEL_COMPONENTS]),
        component_latency_ms=latencies,
        stage_budgets=[dict(
            component=s["component"], duration_ms=number(s.get("duration_ms")),
            remaining_budget_before_ms=number(s.get("remaining_budget_before_ms")),
            configured_stage_cap_ms=number(s.get("configured_stage_cap_ms")),
            allocated_allowance_ms=number(s.get("allocated_allowance_ms")),
            stage_admitted=choice_bool(s.get("stage_admitted")),
            result_accepted=choice_bool(s.get("result_accepted")),
            late_completion=choice_bool(s.get("late_completion")),
            result_abandoned=choice_bool(s.get("result_abandoned")),
        ) for s in spans if s["component"] in MODEL_COMPONENTS],
        production_tokens=tokens,
        usage_completeness=usage_completeness,
        observed_model_identities=[dict(component=s["component"], model=identifier(s.get("resolved_model")))
                                   for s in spans if s["component"] in MODEL_COMPONENTS and identifier(s.get("resolved_model"))],
    )
    from src.agentguard.lineage_adapters import plain
    attempt.update(completed=False, failure_evidence=plain(evidence))
    run.capture(scenario, attempt=attempt, record=record, failure=plain(evidence))


def capture_scenario_failures(function):
    @wraps(function)
    def wrapped(scenario, *args, **kwargs):
        try:
            return function(scenario, *args, **kwargs)
        except BaseException as error:
            retain_failure(scenario, error)
            raise
    return wrapped
