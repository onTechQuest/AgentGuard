"""Mocked production paths: telemetry must never control the outcome."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from agents.usage import Usage
from openai import APIConnectionError, APITimeoutError, RateLimitError, AuthenticationError, PermissionDeniedError
from openai.types.responses import ResponseFunctionToolCall

from src.agent import telemetry as t
from src.agent import support_agent as support
from src.agent import execution_plan as execution
from src.agent.capability_router import CapabilityRoutingError
from src.agent.planning_completeness import PlanningCompletenessError
from src.agentguard.evaluation_record import execute_scenario


SECRET = "private-prompt-customer-key"


def counters():
    return Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)


def response(output, context=None):
    usage = counters()
    return SimpleNamespace(final_output=output, new_items=[],
                           context_wrapper=SimpleNamespace(usage=usage, context=context),
                           raw_responses=[SimpleNamespace(usage=usage)])


def binding(order_id="ORD-1001"):
    return {"capability": "order_status", "order_id": order_id, "needs_clarification": False}


@pytest.fixture
def path(monkeypatch):
    state = SimpleNamespace(recovery=False, router_error=None, recovery_error=None, synthesis_error=None,
                            invalid_router=False, invalid_recovery=False, no_work=False, clarification=False,
                            missing_usage=False, prohibit=False, barrier=None, synth_inputs=[])
    business = Mock(return_value={"order_id": "ORD-1001", "status": "shipped", "found": True,
                                  "customer_id": SECRET})
    monkeypatch.setattr(support.orders, "get_order_status", business)

    def run(agent, message, **kwargs):
        if agent.name == "Capability Router":
            if state.barrier:
                state.barrier.wait(timeout=10)
            if state.router_error:
                raise state.router_error
            order_id = json.loads(message)["extracted_entities"]["order_ids"][0]
            output = {"capability_requests": [] if state.recovery or state.no_work else [binding(order_id)],
                      "confidence": 0.1 if state.clarification else 0.99,
                      "control_signals": ["fabricated_tool_result"] if state.recovery else [],
                      "denied_disclosures": []}
            return response({"invalid": SECRET} if state.invalid_router else output)
        if agent.name == "Planning Completeness Reviewer":
            if state.recovery_error:
                raise state.recovery_error
            return response({"invalid": SECRET} if state.invalid_recovery else {"capability_requests": [binding()]})
        assert agent.name == "AgentGuard Support Agent"
        assert agent.tools == [] and kwargs["max_turns"] == 1
        state.synth_inputs.append(message)
        trace = kwargs["context"]
        assert not trace.missing_required
        if state.synthesis_error:
            raise state.synthesis_error
        result = response("grounded answer", trace)
        if state.missing_usage:
            result.context_wrapper.usage = Usage(requests=1)
            result.raw_responses = []
        if state.prohibit:
            attempt = ResponseFunctionToolCall(type="function_call", name="get_order_status", arguments="{}", call_id="fake")
            asyncio.run(kwargs["hooks"].on_llm_end(result.context_wrapper, agent, SimpleNamespace(output=[attempt])))
        return result

    runner = Mock(side_effect=run)
    monkeypatch.setattr(support.Runner, "run_sync", runner)
    state.business, state.runner = business, runner
    return state


def run_record():
    return execute_scenario({"id": "offline", "input": "Where is ORD-1001? " + SECRET})


def spans(record):
    return {s["component"]: s for s in record["component_spans"]}


def assert_private(record):
    text = json.dumps(record)
    for value in (SECRET, "ORD-1001", "grounded answer", "customer_id", "capability_requests"):
        assert value not in text


def test_success_accounting_and_behavior(path):
    record = run_record()
    observed = record.production_telemetry
    phases = spans(observed)
    assert {"primary_router", "planning_completeness", "policy_resolution", "execution_plan", "tool", "projection", "synthesis"} <= phases.keys()
    assert "recovery_planner" not in phases
    assert observed["terminal_status"] == "completed"
    assert observed["usage_completeness"] == "COMPLETE"
    assert datetime.fromisoformat(observed["started_at_utc"]).utcoffset().total_seconds() == 0
    assert observed["total_latency_ms"] >= phases["synthesis"]["duration_ms"] >= 0
    assert all(s["start_offset_ms"] >= 0 and s["duration_ms"] >= 0 for s in phases.values())
    assert sum(s["total_tokens"] or 0 for s in phases.values()) == record.total_tokens == 30
    assert sum(s["logical_model_calls"] for s in phases.values()) == record.request_count == 2
    assert phases["tool"]["duration_ms"] == record.execution["operations"][0]["latency_ms"]
    assert observed["required_operation_summary"]["required"] == observed["required_operation_summary"]["completed"]
    assert record.final_output == "grounded answer"
    assert record.tool_calls == [{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}]
    path.business.assert_called_once_with(order_id="ORD-1001")
    assert SECRET not in json.dumps(record.tool_outputs)
    assert phases["synthesis"]["configured_model"] is None
    assert isinstance(phases["synthesis"]["resolved_model"], str)
    assert all(s["http_retry_count"] is None and s["retry_source"] is None for s in phases.values())
    assert_private(observed)
    assert t._request.get() is None and t._span.get() is None and t._failure.get() is None


def test_recovery_is_a_distinct_call_and_usage_is_added_once(path):
    path.recovery = True
    record = run_record()
    observed = record.production_telemetry
    phases = spans(observed)
    assert observed["planning_summary"] == {"recovery_triggered": True, "recovery_count": 1, "plan_source": "recovered"}
    assert phases["recovery_planner"]["logical_model_calls"] == 1
    assert phases["recovery_planner"]["http_retry_count"] is None
    assert sum(s["total_tokens"] or 0 for s in phases.values()) == record.total_tokens == 45
    assert path.runner.call_count == 3
    assert_private(observed)


def connection_error():
    return APIConnectionError(message=SECRET, request=httpx2.Request("POST", "https://example.test"))


@pytest.mark.parametrize("failure,component,category,exception", [
    ("router", "primary_router", "NETWORK_ERROR", CapabilityRoutingError),
    ("router_invalid", "primary_router", "INVALID_MODEL_OUTPUT", CapabilityRoutingError),
    ("recovery", "recovery_planner", "NETWORK_ERROR", PlanningCompletenessError),
    ("recovery_invalid", "recovery_planner", "INVALID_MODEL_OUTPUT", PlanningCompletenessError),
    ("policy", "policy_resolution", "UNKNOWN_INTERNAL_FAILURE", ValueError),
    ("plan", "execution_plan", "UNKNOWN_INTERNAL_FAILURE", ValueError),
    ("missing_tool", "tool", "TOOL_NOT_FOUND", execution.ExecutionFailure),
    ("tool", "tool", "TOOL_ERROR", execution.ExecutionFailure),
    ("projection", "projection", "PROJECTION_ERROR", execution.ExecutionFailure),
    ("unresolved", "required_execution", "REQUIRED_OPERATION_INCOMPLETE", execution.ExecutionFailure),
    ("synthesis", "synthesis", "NETWORK_ERROR", APIConnectionError),
    ("prohibited", "synthesis", "PROHIBITED_OPERATION_ATTEMPT", execution.ExecutionFailure),
])
def test_failure_survives_without_changing_exceptions(path, monkeypatch, failure, component, category, exception):
    if failure == "router":
        path.router_error = connection_error()
    elif failure == "router_invalid":
        path.invalid_router = True
    elif failure.startswith("recovery"):
        path.recovery = True
        if failure == "recovery":
            path.recovery_error = connection_error()
        else:
            path.invalid_recovery = True
    elif failure in {"policy", "plan"}:
        monkeypatch.setattr(support, "resolve_request_policy" if failure == "policy" else "build_execution_plan",
                            Mock(side_effect=ValueError(SECRET)))
    elif failure == "missing_tool":
        monkeypatch.setattr(support.orders, "get_order_status", None)
    elif failure == "tool":
        path.business.side_effect = RuntimeError(SECRET)
    elif failure == "projection":
        monkeypatch.setattr(execution, "project_tool_result", Mock(side_effect=ValueError(SECRET)))
    elif failure == "unresolved":
        def unresolved(trace, resolver):
            raise execution.ExecutionFailure(trace, "Required operation unresolved")
        monkeypatch.setattr(support, "execute_required", unresolved)
    elif failure == "synthesis":
        path.synthesis_error = connection_error()
    else:
        path.prohibit = True
    with pytest.raises(exception) as caught:
        support.run_support_agent_detailed("Where is ORD-1001? " + SECRET)
    observed = t.snapshot(caught.value.production_telemetry)
    assert observed["terminal_status"] == "failed"
    assert observed["terminal_failure_component"] == component
    assert observed["terminal_failure_category"] == category
    assert observed["total_latency_ms"] >= 0
    if failure in {"router", "recovery", "synthesis"}:
        assert observed["usage_completeness"] == ("UNAVAILABLE" if failure == "router" else "PARTIAL")
    if failure == "router_invalid":
        assert spans(observed)["primary_router"]["total_tokens"] == 15
    if failure == "prohibited":
        assert spans(observed)["synthesis"]["total_tokens"] == 15
    assert_private(observed)
    assert t._request.get() is None and t._span.get() is None and t._failure.get() is None


def test_structured_failure_evaluation_record_retains_telemetry(path):
    path.business.side_effect = RuntimeError(SECRET)
    record = run_record()
    assert record.execution_error and record.final_output == ""
    assert record.production_telemetry["terminal_failure_category"] == "TOOL_ERROR"
    assert record.production_telemetry["required_operation_summary"]["incomplete"]


def test_missing_usage_stays_unknown(path):
    path.missing_usage = True
    record = run_record()
    observed = record.production_telemetry
    synthesis = spans(observed)["synthesis"]
    assert observed["usage_completeness"] == "PARTIAL"
    assert synthesis["sdk_visible_requests"] == 1
    assert synthesis["total_tokens"] is None and synthesis["input_tokens"] is None
    assert synthesis["usage_available"] is False
    assert synthesis["http_retry_count"] is None


@pytest.mark.parametrize("clarification", [False, True])
def test_no_grants_are_valid_business_outcomes(path, clarification):
    path.no_work = True
    path.clarification = clarification
    record = run_record()
    observed = record.production_telemetry
    assert observed["terminal_status"] == "completed"
    assert observed["terminal_failure_category"] is None
    assert observed["business_outcome"] == ("CLARIFICATION_REQUIRED" if clarification else "NO_AUTHORIZED_OPERATIONS")
    path.business.assert_not_called()


def test_concurrent_requests_are_isolated(path):
    path.barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: support.run_support_agent_detailed("Where is ORD-1001?"), range(2)))
    traces = [r.context_wrapper.production_telemetry for r in records]
    assert traces[0].request_id != traces[1].request_id
    assert not {id(s) for s in traces[0].component_spans} & {id(s) for s in traces[1].component_spans}
    ids = [set(t.snapshot(r)["required_operation_summary"]["completed"]) for r in traces]
    assert len(ids[0]) == len(ids[1]) == 1 and not ids[0] & ids[1]
    assert all(sum(s.total_tokens or 0 for s in r.component_spans) == 30 for r in traces)


@pytest.mark.parametrize("broken", ["creation", "span", "serialization"])
def test_observer_failures_do_not_break_production(path, monkeypatch, broken):
    fail = Mock(side_effect=RuntimeError(SECRET))
    if broken == "creation":
        monkeypatch.setattr(t, "ProductionExecutionTelemetry", fail)
    elif broken == "span":
        monkeypatch.setattr(t, "ComponentSpan", fail)
    else:
        monkeypatch.setattr(t.ProductionExecutionTelemetry, "snapshot", fail)
    record = run_record()
    assert record.final_output == "grounded answer" and record.total_tokens == 30
    path.business.assert_called_once()
    assert path.runner.call_count == 2
    assert t._request.get() is None


@pytest.mark.parametrize("kind,expected", [
    (APITimeoutError, "TIMEOUT"), (APIConnectionError, "NETWORK_ERROR"),
    (RateLimitError, "RATE_LIMIT"), (AuthenticationError, "AUTHENTICATION_FAILURE"),
    (PermissionDeniedError, "AUTHORIZATION_FAILURE"),
])
def test_provider_failure_mapping_uses_types_not_messages(kind, expected):
    request = httpx2.Request("POST", "https://example.test")
    if kind in {APITimeoutError, APIConnectionError}:
        error = kind(request=request)
    else:
        error = kind(SECRET, response=httpx2.Response(429, request=request), body={"secret": SECRET})
    assert t.exception_category(error, "synthesis") == expected


def test_injected_model_identity_is_not_invented():
    from agents import Agent
    @t.observe_request
    def call():
        with t.observe("primary_router"):
            t.model_call(Agent(name="injected", model="configured"), default_resolution=False)
            t.model_result(response("unused"))
        return response("unused")
    result = call()
    phase = result.context_wrapper.production_telemetry.component_spans[0]
    assert phase.configured_model == "configured" and phase.resolved_model is None


def test_sdk_failure_retains_public_run_data_usage(path):
    from agents.exceptions import ModelBehaviorError
    error = ModelBehaviorError(SECRET)
    error.run_data = response("private response")
    path.synthesis_error = error
    with pytest.raises(ModelBehaviorError) as caught:
        support.run_support_agent_detailed("Where is ORD-1001?")
    observed = t.snapshot(caught.value.production_telemetry)
    assert spans(observed)["synthesis"]["total_tokens"] == 15
    assert observed["observed_usage"]["total_tokens"] == 30
    assert observed["terminal_failure_category"] == "INVALID_MODEL_OUTPUT"
    assert_private(observed)


def test_monotonic_timing_does_not_depend_on_wall_clock(path, monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2000, 1, 1, tzinfo=tz)
    monkeypatch.setattr(t, "datetime", Clock)
    observed = run_record().production_telemetry
    assert observed["started_at_utc"].startswith("2000-01-01")
    assert 0 <= observed["total_latency_ms"] < 10000


def test_nested_request_restores_outer_context(path):
    @t.observe_request
    def outer():
        outer_id = t._request.get().request_id
        inner = support.run_support_agent_detailed("Where is ORD-1001?")
        assert inner.context_wrapper.production_telemetry.request_id != outer_id
        assert t._request.get().request_id == outer_id
        with t.observe("outer_finished"):
            pass
        return response("unused")
    result = outer()
    assert [s.component for s in result.context_wrapper.production_telemetry.component_spans] == ["outer_finished"]
    assert t._request.get() is None


def test_profiler_retains_failed_request_observations(path):
    from scripts.audit_token_usage import profile_scenario
    path.synthesis_error = connection_error()
    diagnostic = {}
    with pytest.raises(APIConnectionError):
        profile_scenario({"id": "offline", "input": "Where is ORD-1001?"}, measure_tools=True, diagnostics=diagnostic)
    assert diagnostic["production_telemetry"]["observed_usage"]["total_tokens"] == 15
    assert [s["component"] for s in diagnostic["components"]] == ["router", "agent"]
    assert diagnostic["tool_timings"][0]["status"] == "completed"
    assert path.runner.call_count == 2


def test_router_wrapper_keeps_available_sdk_failure_usage(path):
    from agents.exceptions import ModelBehaviorError
    error = ModelBehaviorError(SECRET)
    error.run_data = response("private response")
    path.router_error = error
    with pytest.raises(CapabilityRoutingError) as caught:
        support.run_support_agent_detailed("Where is ORD-1001?")
    observed = t.snapshot(caught.value.production_telemetry)
    assert spans(observed)["primary_router"]["total_tokens"] == 15
    assert observed["terminal_failure_category"] == "INVALID_MODEL_OUTPUT"
    assert_private(observed)


def test_optional_opaque_label_does_not_change_model_inputs(path):
    result = support.run_support_agent_detailed("Where is ORD-1001?", request_label="request-42")
    assert result.context_wrapper.production_telemetry.external_label == "request-42"
    assert "request-42" not in json.dumps(path.synth_inputs)


def test_synthesis_failure_raises_same_exception_instance(path):
    error = connection_error()
    path.synthesis_error = error
    with pytest.raises(APIConnectionError) as caught:
        support.run_support_agent_detailed("Where is ORD-1001?")
    assert caught.value is error


def test_disabled_observations_preserve_failure_identity(path, monkeypatch):
    error = connection_error()
    path.synthesis_error = error
    monkeypatch.setattr(t, "ComponentSpan", Mock(side_effect=RuntimeError("observer failed")))
    with pytest.raises(APIConnectionError) as caught:
        support.run_support_agent_detailed("Where is ORD-1001?")
    assert caught.value is error
    assert error.production_telemetry.observation_incomplete
    assert error.production_telemetry.terminal_status == "failed"


def test_token_diagnostic_persists_sanitized_failure_telemetry(path, monkeypatch, tmp_path):
    from scripts import audit_token_usage as audit
    path.synthesis_error = connection_error()
    monkeypatch.setattr(audit, "load_dataset", Mock(return_value=[{"id": "offline", "input": "Where is ORD-1001?"}]))
    report = tmp_path / "audit.json"
    assert audit.main(["--output", str(report)]) == 1
    saved = json.loads(report.read_text())
    assert saved["failure_telemetry"]["terminal_failure_component"] == "synthesis"
    assert saved["failure_telemetry"]["observed_usage"]["total_tokens"] == 15
    assert SECRET not in report.read_text()
