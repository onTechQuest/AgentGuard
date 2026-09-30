"""Opt-in, content-free projections of existing evidence. No execution or clocks."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from pathlib import Path

from src.agentguard.metric_registry import METRICS, REGISTRY_VERSION
from src.agentguard.safety_incidents import Redactor
from src.agentguard.synthesis_qualification import number, flag, identity
from src.agentguard.tool_policy import TOOL_REGISTRY
from src.agent.telemetry import FailureCategory

FAILURES = {category.value for category in FailureCategory}


class NullObserver:
    def emit_span(self, event): pass
    def emit_metric(self, event): pass
    def emit_evaluation(self, event): pass


_observer = ContextVar("observability", default=NullObserver())


@contextmanager
def observe(observer):
    """Opt in around an existing evaluation invocation; never invokes it itself."""
    token = _observer.set(observer)
    try:
        yield observer
    finally:
        _observer.reset(token)


def project(run_id, observation, *, failure=None, telemetry=None, invocation_id=None, quality_decision=None):
    """Return version 1 events; missing evidence is never reconstructed from intent."""
    raw, failure = telemetry or {}, failure or {}
    op = observation.get("operational", {})
    redactor = Redactor([raw, failure, observation])
    def safe(value):
        value = identity(value)
        return value if value and redactor.clean(value) == value else None
    request_id = safe(raw.get("request_id") or op.get("request_id") or failure.get("request_id"))
    scenario_id = safe(observation.get("scenario_id"))
    repetition = observation.get("repetition", 1)
    repetition = repetition if type(repetition) is int and repetition > 0 else 1
    trace = sha256(json.dumps([run_id, scenario_id, repetition, request_id]).encode()).hexdigest()[:32]
    def sid(label): return sha256((trace + ":" + label).encode()).hexdigest()[:16]
    common = dict(schema_version=1, timestamp=None, run_id=safe(run_id), invocation_id=safe(invocation_id),
                  request_id=request_id, scenario_id=scenario_id, repetition=repetition, trace_id=trace)
    # Only a validated source timestamp, never a new clock or manifest creation time.
    from datetime import datetime
    try:
        stamp = raw.get("started_at_utc")
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is not None:
            common["timestamp"] = parsed.isoformat()
    except (TypeError, ValueError):
        pass
    events = []
    def span(label, component, status, duration, attributes):
        event = dict(common, event_type="SPAN", span_id=sid(label), parent_span_id=None if label == "request" else sid("request"),
                     name="agentguard." + label.split(":")[0], component=component, status=status,
                     duration_ms=number(duration), attributes=attributes)
        events.append(event)
        return event
    terminal = raw.get("terminal_status") or failure.get("terminal_status")
    status = {"completed": "OK", "failed": "ERROR", "cancelled": "ERROR"}.get(terminal, "UNAVAILABLE")
    if status == "UNAVAILABLE" and observation.get("completed") is True: status = "OK"
    if failure: status = "ERROR"
    category = raw.get("terminal_failure_category") or failure.get("failure_category") or op.get("terminal_category")
    category = category if category in FAILURES else None
    root = span("request", "request", status, raw.get("total_latency_ms", op.get("total_latency_ms", failure.get("total_latency_ms"))),
                dict(failure_category=category, terminal_status=terminal if terminal in {"completed", "failed", "cancelled"} else None,
                     result_abandoned=flag(raw.get("result_abandoned", failure.get("result_abandoned"))),
                     retry_count=number(raw.get("retry_attempts_total", op.get("retry_count", failure.get("retry_attempts_total"))))))
    components = {"primary_router": "router", "recovery_planner": "recovery", "required_execution": "tool", "synthesis": "synthesis"}
    sources = raw.get("component_spans") or failure.get("stage_budgets") or []
    sources = [s for s in sources if s.get("component") in components]
    # Legacy observations retain aggregate timings, not tool identities or statuses.
    for component, field in (("primary_router", "router_latency_ms"), ("recovery_planner", "recovery_latency_ms"),
                             ("required_execution", "required_operation_latency_ms"), ("synthesis", "synthesis_latency_ms")):
        if any(s.get("component") == component for s in sources): continue
        key = "required_operations" if component == "required_execution" else component
        duration = op.get(field, failure.get("component_latency_ms", {}).get(key))
        if component == "recovery_planner" and duration is None: continue
        sources.append(dict(component=component, duration_ms=duration))
    sources.sort(key=lambda s: list(components).index(s["component"]))
    tools = {t.name for t in TOOL_REGISTRY}
    for index, source in enumerate(sources):
        component = source["component"]
        label = components[component]
        tool = source.get("tool") if source.get("tool") in tools else None
        if label == "tool": label += "." + (tool or "UNKNOWN")
        state = {"completed": "OK", "failed": "ERROR", "cancelled": "ERROR"}.get(source.get("status"), "UNAVAILABLE")
        if source.get("result_accepted") is True: state = "OK"
        failed_component = failure.get("failure_component") or raw.get("terminal_failure_component")
        if failed_component == component or source.get("result_abandoned") is True: state = "ERROR"
        span(label + ":" + str(index), component, state, source.get("duration_ms"),
             dict(model=safe(source.get("resolved_model")), tool_name=tool,
                  input_tokens=number(source.get("input_tokens")), output_tokens=number(source.get("output_tokens")),
                  total_tokens=number(source.get("total_tokens")), failure_category=source.get("failure_category") if source.get("failure_category") in FAILURES else (category if state == "ERROR" else None),
                  result_abandoned=flag(source.get("result_abandoned")),
                  timing_scope="component" if source.get("status") else "retained_component_aggregate"))
    det = observation.get("deterministic", {})
    semantic = {name: dict(score=number(observation.get("semantic", {}).get(name, {}).get("score")),
                          available=flag(observation.get("semantic", {}).get(name, {}).get("evaluator_available")))
                for name in ("answer_relevancy", "correctness", "faithfulness")}
    evaluation = {k: flag(det.get(k)) for k in ("functional_pass", "tool_pass", "argument_pass", "factual_grounding_pass")}
    evaluation.update(safety_pass=flag(observation.get("safety", {}).get("final_pass")), semantic=semantic,
                      quality_decision=quality_decision if quality_decision in {"PASS", "FAIL"} else None,
                      quality_decision_scope="run", evaluation_population=observation.get("population") if observation.get("population") in {"functional", "safety"} else None)
    known = [v for k, v in evaluation.items() if k.endswith("_pass") and v is not None]
    available = bool(known) or any(v["score"] is not None for v in semantic.values())
    evspan = span("evaluation", "evaluation", "OK" if available else "UNAVAILABLE", None, {})
    events.append(dict(common, event_type="EVALUATION", span_id=evspan["span_id"], parent_span_id=root["span_id"],
                       status=evspan["status"], attributes=evaluation))
    def metric(name, value, unit, owner, **attributes):
        if number(value) is None: return
        events.append(dict(common, event_type="METRIC", span_id=owner["span_id"], parent_span_id=owner["parent_span_id"],
                           name=name, value=value, unit=unit, attributes=attributes))
    for event in list(events):
        if event["event_type"] != "SPAN" or event["component"] == "evaluation": continue
        label = components.get(event["component"], "request")
        metric("agentguard." + label + ".latency_ms", event["duration_ms"], "ms", event)
    metric("agentguard.tokens.total", raw.get("observed_usage", {}).get("total_tokens", op.get("production_tokens", failure.get("production_tokens", {}).get("total_tokens"))), "tokens", root)
    metric("agentguard.retry.count", root["attributes"]["retry_count"], "attempts", root)
    for definition in METRICS:
        if definition["family"] == "OPERATIONAL" or definition["population"] != observation.get("population"): continue
        value = observation
        for part in definition["observation_path"].split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if type(value) is bool: value = int(value)
        metric("agentguard." + definition["metric_id"], value, definition["unit"], evspan,
               metric_registry_version=REGISTRY_VERSION, metric_version=definition["version"], scope="observation", authority="DESCRIPTIVE_ONLY")
    return events


def dispatch(observer, events):
    """Exporter failures cannot affect evaluation results or exceptions."""
    for event in events:
        try:
            getattr(observer, "emit_" + event["event_type"].lower())(event)
        except Exception:
            pass


class JsonObserver:
    """Accepts projected contract events, never raw telemetry/payloads."""
    def __init__(self, root):
        self.root, self.events = Path(root), {}

    def _emit(self, event):
        validate_event(event)
        key = event["trace_id"]
        rows = self.events.setdefault(key, [])
        if event not in rows: rows.append(event)
        directory = self.root / "reports" / "observability" / event["run_id"]
        directory.mkdir(parents=True, exist_ok=True)
        label = event["request_id"] or event["scenario_id"] or key
        # Preserve correlation IDs verbatim; only filesystem names may fall back.
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", label): label = key
        if event["repetition"] > 1: label += "-" + str(event["repetition"])
        target = directory / (label + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(schema_version=1, events=rows), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)

    emit_span = emit_metric = emit_evaluation = _emit


def validate_event(event):
    """Reject non-contract input at the file boundary, without echoing content."""
    import re
    common = set("schema_version timestamp run_id invocation_id request_id scenario_id repetition trace_id span_id parent_span_id event_type attributes".split())
    extras = {"SPAN": {"name", "component", "status", "duration_ms"}, "METRIC": {"name", "value", "unit"}, "EVALUATION": {"status"}}
    kind = event.get("event_type")
    if kind not in extras or set(event) != common | extras[kind] or event["schema_version"] != 1:
        raise ValueError("Invalid observability contract")
    for key, length in (("trace_id", 32), ("span_id", 16), ("parent_span_id", 16)):
        value = event[key]
        if value is None and key == "parent_span_id": continue
        if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{" + str(length) + "}", value):
            raise ValueError("Invalid observability ID")
    for key in ("run_id", "request_id", "invocation_id", "scenario_id"):
        if event[key] is not None and identity(event[key]) != event[key]: raise ValueError("Invalid correlation ID")
    if event["run_id"] is None or type(event["repetition"]) is not int or event["repetition"] < 1:
        raise ValueError("Invalid run identity")
    allowed = {
        "SPAN": set("failure_category terminal_status result_abandoned retry_count model tool_name input_tokens output_tokens total_tokens timing_scope".split()),
        "METRIC": set("metric_registry_version metric_version scope authority".split()),
        "EVALUATION": set("functional_pass tool_pass argument_pass factual_grounding_pass safety_pass semantic quality_decision quality_decision_scope evaluation_population".split()),
    }
    if not isinstance(event["attributes"], dict) or set(event["attributes"]) - allowed[kind]:
        raise ValueError("Non-allowlisted observability attributes")
    enums = dict(failure_category=FAILURES, terminal_status={"completed", "failed", "cancelled"},
                 tool_name={t.name for t in TOOL_REGISTRY}, timing_scope={"component", "retained_component_aggregate"},
                 scope={"observation"}, authority={"DESCRIPTIVE_ONLY"}, quality_decision={"PASS", "FAIL"},
                 quality_decision_scope={"run"}, evaluation_population={"functional", "safety"})
    bools = {"result_abandoned", "functional_pass", "tool_pass", "argument_pass", "factual_grounding_pass", "safety_pass"}
    numbers = {"retry_count", "input_tokens", "output_tokens", "total_tokens", "metric_registry_version", "metric_version"}
    for key, value in event["attributes"].items():
        if value is None: continue
        valid = False
        if key in enums: valid = isinstance(value, str) and value in enums[key]
        elif key in bools: valid = type(value) is bool
        elif key in numbers: valid = number(value) is not None
        elif key == "model": valid = identity(value) == value
        elif key == "semantic":
            valid = isinstance(value, dict) and set(value) == {"answer_relevancy", "correctness", "faithfulness"}
            if valid:
                valid = all(isinstance(v, dict) and set(v) == {"score", "available"}
                            and (v["score"] is None or number(v["score"]) is not None)
                            and (v["available"] is None or type(v["available"]) is bool) for v in value.values())
        if not valid: raise ValueError("Invalid observability attribute")
    if kind in {"SPAN", "EVALUATION"} and event["status"] not in {"OK", "ERROR", "UNAVAILABLE"}:
        raise ValueError("Invalid observability status")
    if kind == "SPAN":
        names = {"agentguard." + name for name in ("request", "router", "recovery", "synthesis", "evaluation", "tool.UNKNOWN")}
        names.update("agentguard.tool." + t.name for t in TOOL_REGISTRY)
        if event["name"] not in names or event["component"] not in {"request", "primary_router", "recovery_planner", "required_execution", "synthesis", "evaluation"}:
            raise ValueError("Invalid span component")
        if event["duration_ms"] is not None and number(event["duration_ms"]) is None: raise ValueError("Invalid duration")
    if kind == "METRIC":
        names = {"agentguard." + d["metric_id"] for d in METRICS}
        names.update("agentguard." + name for name in ("request.latency_ms", "router.latency_ms", "recovery.latency_ms", "synthesis.latency_ms", "tool.latency_ms", "tokens.total", "retry.count"))
        if event["name"] not in names or number(event["value"]) is None or event["unit"] not in {"ms", "tokens", "attempts", "ratio"}:
            raise ValueError("Invalid metric")
    if event["timestamp"] is not None:
        from datetime import datetime
        if datetime.fromisoformat(event["timestamp"]).tzinfo is None: raise ValueError("Invalid timestamp")
    # Events originate from project(); never accept additional raw fields.
    if Redactor([event]).clean(event) != event: raise ValueError("Unsafe observability content")


class ConsoleObserver:
    def emit_span(self, event):
        duration = "UNAVAILABLE" if event["duration_ms"] is None else f'{event["duration_ms"]:.2f}ms'
        print(f'{"  " if event["parent_span_id"] else ""}{event["name"]:<42} {duration:>14} {event["status"]}')
    def emit_metric(self, event):
        print(f'  {event["name"]} = {event["value"]} {event["unit"]}')
    def emit_evaluation(self, event):
        print("  evaluation: " + json.dumps(event["attributes"], sort_keys=True))


def otel_span(event):
    """Dependency-free OTel concepts, not an OTLP wire payload or SDK exporter."""
    if event["event_type"] != "SPAN": raise ValueError("Expected span")
    attributes = {"agentguard." + k: v for k, v in event["attributes"].items() if v is not None}
    attributes.update({"agentguard." + k: event[k] for k in ("run_id", "invocation_id", "request_id", "scenario_id") if event[k] is not None})
    if event["attributes"].get("model"): attributes["gen_ai.response.model"] = event["attributes"]["model"]
    return dict(trace_id=event["trace_id"], span_id=event["span_id"], parent_span_id=event["parent_span_id"],
                name=event["name"], status={"OK": "OK", "ERROR": "ERROR", "UNAVAILABLE": "UNSET"}[event["status"]],
                duration_ms=event["duration_ms"], attributes=attributes)


def capture(run, index, record=None, telemetry=None):
    if type(_observer.get()) is NullObserver: return
    try:
        raw = telemetry or getattr(record, "production_telemetry", None)
        if not raw: return
        events = project(run.manifest.run_id, run.observations[index], telemetry=raw)
        cache = getattr(run, "_observability_spans", {})
        cache[index] = [e for e in events if (e["event_type"] == "SPAN" and e["component"] != "evaluation")
                        or (e["event_type"] == "METRIC" and e["attributes"].get("scope") != "observation")]
        run._observability_spans = cache
    except Exception:
        pass


def finish(run):
    observer = _observer.get()
    if type(observer) is NullObserver: return
    try:
        from src.agentguard.invocation import current_invocation
        invocation = current_invocation()
        invocation_id = invocation.document["invocation_id"] if invocation else None
        for index, observation in enumerate(run.observations):
            passed = getattr(run, "aggregate", {}).get("quality_result", {}).get("passed")
            decision = "PASS" if passed is True else "FAIL" if passed is False else None
            events = project(run.manifest.run_id, observation, failure=run.executed[index].get("failure_evidence"), invocation_id=invocation_id, quality_decision=decision)
            cached = getattr(run, "_observability_spans", {}).get(index)
            if cached:
                for e in cached: e["invocation_id"] = invocation_id
                events = cached + [e for e in events if e["event_type"] == "EVALUATION" or e.get("component") == "evaluation"
                                   or (e["event_type"] == "METRIC" and e["attributes"].get("scope") == "observation")]
            dispatch(observer, events)
    except Exception:
        pass
