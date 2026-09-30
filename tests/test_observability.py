import json
from types import SimpleNamespace

import pytest

from src.agentguard import observability as ob
from src.agentguard.metric_registry import METRICS


def evidence():
    observation = dict(scenario_id="example_001", repetition=1, population="functional",
                       operational=dict(request_id="request-123", total_latency_ms=100, production_tokens=20, retry_count=0),
                       deterministic=dict(functional_pass=True, tool_pass=True, argument_pass=False, factual_grounding_pass=True),
                       semantic=dict(correctness=dict(score=4, evaluator_available=True)))
    telemetry = dict(request_id="request-123", started_at_utc="2026-01-01T00:00:00+00:00", total_latency_ms=100,
                     terminal_status="completed", observed_usage=dict(total_tokens=20), retry_attempts_total=0,
                     component_spans=[dict(component=c, status="completed", duration_ms=d, tool=t, resolved_model="model-1")
                                      for c, d, t in (("primary_router", 20, None), ("required_execution", 1, "get_order_status"), ("synthesis", 70, None))])
    return observation, telemetry


def test_success_hierarchy_correlation_and_metrics():
    observation, telemetry = evidence()
    events = ob.project("run-1", observation, telemetry=telemetry, invocation_id="invocation-1")
    spans = [e for e in events if e["event_type"] == "SPAN"]
    assert [s["name"] for s in spans] == ["agentguard.request", "agentguard.router", "agentguard.tool.get_order_status", "agentguard.synthesis", "agentguard.evaluation"]
    assert all(s["status"] == "OK" for s in spans)
    assert all(s["parent_span_id"] == spans[0]["span_id"] for s in spans[1:])
    assert spans[0]["duration_ms"] == 100  # Not the component sum.
    assert spans[-1]["duration_ms"] is None
    assert all(e["request_id"] == "request-123" and e["invocation_id"] == "invocation-1" for e in events)
    assert len({e["trace_id"] for e in events}) == 1
    assert all(len(e["trace_id"]) == 32 and len(e["span_id"]) == 16 for e in events)
    metrics = {e["name"]: e for e in events if e["event_type"] == "METRIC"}
    assert metrics["agentguard.tokens.total"]["value"] == 20
    assert metrics["agentguard.retry.count"]["value"] == 0
    for definition in METRICS:
        metric = metrics.get("agentguard." + definition["metric_id"])
        if metric:
            assert metric["unit"] == definition["unit"]
            assert metric["attributes"]["metric_version"] == definition["version"]
    evaluation = next(e for e in events if e["event_type"] == "EVALUATION")
    assert evaluation["attributes"]["argument_pass"] is False
    assert evaluation["attributes"]["semantic"]["correctness"]["score"] == 4
    assert evaluation["attributes"]["quality_decision"] is None


def test_recovery_and_repetition():
    observation, telemetry = evidence()
    telemetry["component_spans"].append(dict(component="recovery_planner", status="completed", duration_ms=5))
    first = ob.project("run-1", observation, telemetry=telemetry)
    observation["repetition"] = 2
    second = ob.project("run-1", observation, telemetry=telemetry)
    assert any(e.get("name") == "agentguard.recovery" for e in first)
    assert first[0]["trace_id"] != second[0]["trace_id"]


def test_deadline_retained_and_missing_evidence():
    failure = dict(request_id="request-123", failure_category="DEADLINE_EXHAUSTED", failure_component="synthesis",
                   terminal_status="failed", result_abandoned=True, total_latency_ms=19000,
                   stage_budgets=[dict(component="primary_router", duration_ms=1200, result_accepted=True),
                                  dict(component="synthesis", duration_ms=17800, result_abandoned=True)],
                   component_latency_ms=dict(required_operations=1))
    events = ob.project("run-1", dict(scenario_id="example_001"), failure=failure)
    spans = {e["name"]: e for e in events if e["event_type"] == "SPAN"}
    assert spans["agentguard.request"]["status"] == "ERROR"
    assert spans["agentguard.router"]["status"] == "OK"
    assert spans["agentguard.synthesis"]["status"] == "ERROR"
    assert spans["agentguard.synthesis"]["attributes"]["failure_category"] == "DEADLINE_EXHAUSTED"
    assert spans["agentguard.synthesis"]["attributes"]["result_abandoned"] is True
    assert spans["agentguard.tool.UNKNOWN"]["status"] == "UNAVAILABLE"
    assert spans["agentguard.evaluation"]["status"] == "UNAVAILABLE"
    assert all(e["timestamp"] is None for e in events)
    empty = ob.project("run-1", dict(scenario_id="legacy"))
    assert all(e["duration_ms"] is None and e["status"] == "UNAVAILABLE" for e in empty if e["event_type"] == "SPAN")
    assert not any(e["event_type"] == "METRIC" for e in empty)


@pytest.mark.parametrize("field", ["api_key", "password", "headers", "environment", "prompt", "response", "customer_id", "tool_payload", "exception_message"])
def test_private_content_excluded(tmp_path, field):
    observation, telemetry = evidence()
    sentinel = "PRIVATE_SENTINEL_" + field
    telemetry[field] = sentinel
    telemetry["component_spans"][0][field] = sentinel
    observation[field] = sentinel
    events = ob.project("run-1", observation, telemetry=telemetry)
    observer = ob.JsonObserver(tmp_path)
    ob.dispatch(observer, events)
    document = (tmp_path / "reports/observability/run-1/request-123.json").read_text()
    assert sentinel not in document
    assert json.loads(document)["events"] == events


def test_redaction_of_known_secret_identity():
    observation, telemetry = evidence()
    telemetry["password"] = "private-model"
    telemetry["component_spans"][0]["resolved_model"] = "private-model"
    telemetry["component_spans"][1]["tool"] = "customer-private-tool"
    serialized = json.dumps(ob.project("run-1", observation, telemetry=telemetry))
    assert "private-model" not in serialized
    assert "customer-private-tool" not in serialized


def test_otel_deterministic_and_unknown_status():
    event = ob.project("run-1", dict(scenario_id="legacy"))[0]
    assert ob.otel_span(event) == ob.otel_span(event)
    assert ob.otel_span(event)["status"] == "UNSET"
    assert ob.otel_span(event)["duration_ms"] is None


def test_null_and_export_failures_do_not_touch_execution(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Projection must not execute")
    monkeypatch.setattr(ob, "project", forbidden)
    ob.capture(None, 0)
    ob.finish(None)
    class Broken:
        def emit_span(self, event): raise RuntimeError("private error")
    ob.dispatch(Broken(), [dict(event_type="SPAN")])


def test_opt_in_captures_once_and_final_evaluation(tmp_path):
    observation, telemetry = evidence()
    run = SimpleNamespace(manifest=SimpleNamespace(run_id="run-1"), observations=[observation], executed=[{}])
    with ob.observe(ob.JsonObserver(tmp_path)):
        ob.capture(run, 0, telemetry=telemetry)
        ob.capture(run, 0, telemetry=telemetry)
        ob.finish(run)
    events = json.loads((tmp_path / "reports/observability/run-1/request-123.json").read_text())["events"]
    assert len([e for e in events if e.get("name") == "agentguard.tool.get_order_status"]) == 1
    assert len([e for e in events if e["event_type"] == "EVALUATION"]) == 1
    assert all("component_spans" not in e for e in events)


@pytest.mark.parametrize("mutation", ["extra", "attribute", "semantic", "path"])
def test_json_rejects_non_contract_input(tmp_path, mutation):
    observation, telemetry = evidence()
    events = ob.project("run-1", observation, telemetry=telemetry)
    event = next(e for e in events if e["event_type"] == "EVALUATION")
    if mutation == "extra": event["raw_prompt"] = "PRIVATE_SENTINEL"
    if mutation == "attribute": event["attributes"]["headers"] = "PRIVATE_SENTINEL"
    if mutation == "semantic": event["attributes"]["semantic"]["correctness"]["payload"] = "PRIVATE_SENTINEL"
    if mutation == "path": event["run_id"] = "../../outside"
    with pytest.raises(ValueError): ob.JsonObserver(tmp_path).emit_evaluation(event)
    assert not list(tmp_path.rglob("*.json"))


def test_legacy_inspector_and_validation(monkeypatch, tmp_path):
    from scripts import inspect_observability as cli
    run_id = "a" * 32
    loaded = dict(scenarios=[dict(scenario_id="legacy")], results=dict(executions=[dict(scenario_id="legacy", completed=True)]))
    monkeypatch.setattr(cli, "load_run", lambda path: loaded)
    events = cli.inspect(tmp_path, run_id, "legacy")
    assert events[0]["request_id"] is None
    assert events[0]["status"] == "UNAVAILABLE"
    with pytest.raises(ValueError): cli.inspect(tmp_path, "../outside", "legacy")
    with pytest.raises(ValueError): cli.inspect(tmp_path, run_id, "wrong")


def test_quality_decision_is_existing_run_scoped():
    observation, _ = evidence()
    events = ob.project("run-1", observation, quality_decision="FAIL")
    evaluation = next(e for e in events if e["event_type"] == "EVALUATION")
    assert evaluation["attributes"]["quality_decision"] == "FAIL"
    assert evaluation["attributes"]["quality_decision_scope"] == "run"


def test_registered_tool_and_model_match_current_telemetry():
    observation, telemetry = evidence()
    events = ob.project("run-1", observation, telemetry=telemetry)
    router = next(e for e in events if e.get("component") == "primary_router")
    assert router["attributes"]["model"] == "model-1"
    assert ob.otel_span(router)["attributes"]["gen_ai.response.model"] == "model-1"
    for event in events: ob.validate_event(event)
