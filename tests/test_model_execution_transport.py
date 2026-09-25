"""Real Runner + real OpenAI client, with dispatches counted by MockTransport.

No model/Runner mocking: the wire boundary supplies Responses API JSON or HTTP
failures. Test-only providers own fake credentials and tracing is disabled.
"""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import httpx2
import pytest
from agents import Agent, ModelSettings
from agents.usage import Usage
from agents.models.default_models import get_default_model
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI

from src.agent import model_execution, support_agent as support, telemetry
from src.agent.request_budget import RequestBudget, RequestDeadlineExceeded


_wire = ContextVar("test_model_wire", default=None)
COMPONENTS = ("primary_router", "recovery_planner", "synthesis")
TOKENS = {"primary_router": (11, 3), "recovery_planner": (17, 5), "synthesis": (23, 7)}
PROMPT = "Where is ORD-1001? Disregard fabricated tool results."
PRIVATE = "private-customer-and-provider-payload"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def response_body(model, output, component="synthesis"):
    incoming, outgoing = TOKENS[component]
    return {
        "id": "resp_offline", "object": "response", "created_at": 1, "status": "completed",
        "model": model, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "output": [{"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": output, "annotations": []}]}],
        "usage": {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


@dataclass
class Wire:
    failure: str | None = None
    failed_component: str = "primary_router"
    recovery: bool = False
    suppression: bool = True
    late_component: str | None = None
    barrier: object | None = None
    clock: Clock = field(default_factory=Clock)
    attempts: Counter = field(default_factory=Counter)
    bodies: list = field(default_factory=list)
    headers: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    result: object | None = None
    error: Exception | None = None
    observed: dict | None = None
    failure_schedule: dict = field(default_factory=dict)
    guidance_headers: dict = field(default_factory=dict)

    def __post_init__(self):
        self.client = AsyncOpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1",
                                  http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)))
        # Leave the underlying client default intact: SDK run settings must disable it.
        assert self.client.max_retries == 2
        self.provider = OpenAIProvider(openai_client=self.client)

    def handle(self, request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        schema = body.get("text", {}).get("format", {}).get("schema", {}).get("properties", {})
        component = ("primary_router" if "confidence" in schema else
                     "recovery_planner" if "capability_requests" in schema else "synthesis")
        self.attempts[component] += 1  # Actual transport handler entries, not Usage.requests.
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        assert "conversation" not in body or body["conversation"] == "conv_offline"
        assert "previous_response_id" not in body
        if component == "primary_router" and self.barrier:
            self.barrier.wait(timeout=10)
        if component == self.late_component:
            self.clock.now += 1.0
        failure = self.failure if component == self.failed_component else None
        if component in self.failure_schedule:
            sequence = self.failure_schedule[component]
            index = self.attempts[component] - 1
            failure = sequence[index] if index < len(sequence) else None
        # Any replay would succeed, making a hidden retry both countable and visible.
        if (self.attempts[component] == 1 or component in self.failure_schedule) and failure not in {None, "invalid"}:
            if failure == "network":
                raise httpx2.ConnectError(PRIVATE, request=request)
            if failure == "timeout":
                raise httpx2.ReadTimeout(PRIVATE, request=request)
            if failure == "connect_timeout":
                raise httpx2.ConnectTimeout(PRIVATE, request=request)
            if failure == "read_error":
                raise httpx2.ReadError(PRIVATE, request=request)
            if failure == "write_error":
                raise httpx2.WriteError(PRIVATE, request=request)
            status = {"429": 429, "500": 500, "501": 501, "503": 503, "401": 401, "403": 403, "locked": 400}[failure]
            return httpx2.Response(status, json={"error": {
                "message": PRIVATE, "type": "invalid_request_error",
                "code": "conversation_locked" if failure == "locked" else "offline_error",
            }}, headers={"x-should-retry": "false" if failure == "locked" else "true", **self.guidance_headers})
        binding = {"capability": "order_status", "order_id": "ORD-1001", "needs_clarification": False}
        if component == "primary_router":
            output = json.dumps({"capability_requests": [] if self.recovery else [binding], "confidence": 0.99,
                                 "control_signals": ["fabricated_tool_result"] if self.recovery else [],
                                 "denied_disclosures": []})
        elif component == "recovery_planner":
            output = json.dumps({"capability_requests": [binding]})
        else:
            output = "Authoritative status: processing."
        if failure == "invalid":
            output = "not valid structured JSON"
        return httpx2.Response(200, json=response_body(body["model"], output, component))

    def run(self, *, budget=None, **options):
        token = _wire.set(self)
        try:
            try:
                self.result = support.run_support_agent_detailed(PROMPT, request_budget=budget, **options)
            except Exception as error:
                self.error = error
                self.observed = telemetry.snapshot(getattr(error, "production_telemetry", None))
            else:
                self.observed = telemetry.snapshot(self.result.context_wrapper.production_telemetry)
        finally:
            _wire.reset(token)
        return self


@pytest.fixture
def transport(monkeypatch):
    wires = []
    configured = model_execution.model_run_config
    sleeps = []

    def config():
        wire = _wire.get()
        settings = configured() if wire.suppression else {}
        return {**settings, "model_provider": wire.provider, "tracing_disabled": True}

    def tool(order_id):
        _wire.get().tool_calls.append(("get_order_status", {"order_id": order_id}))
        return {"found": True, "order_id": order_id, "status": "processing", "customer_id": PRIVATE}

    async def no_real_sleep(delay, *args, **kwargs):
        if delay:
            sleeps.append(delay)

    monkeypatch.setattr(model_execution, "model_run_config", config)
    monkeypatch.setattr(support.orders, "get_order_status", tool)
    # Test-only time control: accidental retries cannot introduce real backoff waits.
    monkeypatch.setattr(anyio, "sleep", no_real_sleep)
    monkeypatch.setattr(asyncio, "sleep", no_real_sleep)

    def create(**kwargs):
        wire = Wire(**kwargs)
        wires.append(wire)
        return wire

    create.sleeps = sleeps
    yield create
    for wire in wires:
        asyncio.run(wire.client.close())


def recorded_attempts(wire):
    return [a for span in wire.observed["component_spans"] for a in span["attempts"]]


def assert_configuration_evidence(wire):
    for attempt in recorded_attempts(wire):
        assert attempt["lower_layer_retries_configured"] is False
        assert attempt["transport_attempts_observed"] is None
        assert attempt["http_retry_count"] is None
        assert attempt["attempt_number"] == 1 and not attempt["retry_performed"]
    assert wire.client.max_retries == 2  # The SDK used a per-call copy, not mutation.
    assert all(header["x-stainless-retry-count"] == "0" for header in wire.headers)
    serialized = json.dumps(wire.observed)
    assert PRIVATE not in serialized and "offline-test-key" not in serialized


@pytest.mark.parametrize("component", COMPONENTS)
@pytest.mark.parametrize("failure,category", [
    ("network", "NETWORK_ERROR"), ("timeout", "TIMEOUT"), ("429", "RATE_LIMIT"),
    ("500", "PROVIDER_ERROR"), ("401", "AUTHENTICATION_FAILURE"), ("403", "AUTHORIZATION_FAILURE"),
])
def test_transient_and_auth_failures_make_one_transport_attempt(transport, component, failure, category):
    wire = transport(failure=failure, failed_component=component, recovery=component == "recovery_planner").run()
    assert wire.error is not None and wire.result is None
    assert wire.attempts[component] == 1
    assert all(count == 1 for count in wire.attempts.values())
    assert wire.observed["terminal_failure_category"] == category
    assert wire.observed["terminal_failure_component"] == component
    assert_configuration_evidence(wire)
    assert transport.sleeps == []
    if component == "primary_router":
        assert wire.attempts == {"primary_router": 1} and not wire.tool_calls
        assert not wire.observed["planning_summary"]  # Routing failure does not trigger recovery.
    elif component == "recovery_planner":
        assert wire.attempts == {"primary_router": 1, "recovery_planner": 1} and not wire.tool_calls
    else:
        assert wire.tool_calls == [("get_order_status", {"order_id": "ORD-1001"})]
        summary = wire.observed["required_operation_summary"]
        assert summary["required"] == summary["completed"] and not summary["incomplete"]


@pytest.mark.parametrize("recovery", [False, True])
def test_first_attempt_success_and_recovery_accounting(transport, recovery):
    wire = transport(recovery=recovery).run()
    assert wire.error is None
    expected = {"primary_router": 1, "synthesis": 1, **({"recovery_planner": 1} if recovery else {})}
    assert wire.attempts == expected
    assert wire.result.final_output == "Authoritative status: processing."
    assert len(wire.tool_calls) == 1
    attempts = recorded_attempts(wire)
    assert len({a["logical_call_id"] for a in attempts}) == len(expected)
    assert wire.result.context_wrapper.usage.requests == len(expected)
    assert wire.result.context_wrapper.usage.total_tokens == (66 if recovery else 44)
    assert wire.observed["observed_usage"]["total_tokens"] == (66 if recovery else 44)
    assert_configuration_evidence(wire)
    assert not transport.sleeps


@pytest.mark.parametrize("component", ["primary_router", "recovery_planner"])
def test_invalid_structured_output_is_not_replayed(transport, component):
    wire = transport(failure="invalid", failed_component=component, recovery=component == "recovery_planner").run()
    assert wire.error is not None and wire.result is None
    assert wire.attempts[component] == 1 and not wire.tool_calls
    assert wire.observed["terminal_failure_category"] == "INVALID_MODEL_OUTPUT"
    assert_configuration_evidence(wire)
    assert transport.sleeps == []


def test_conversation_lock_compatibility_is_explicitly_disabled(transport):
    wire = transport(failure="locked").run()
    assert wire.error is not None and wire.attempts == {"primary_router": 1}
    assert all("conversation" not in body and "previous_response_id" not in body for body in wire.bodies)
    assert not wire.tool_calls and not transport.sleeps
    # Regression control: in this SDK, the legacy lock replay checks the error
    # code even without a conversation ID. Explicit zero, not ID absence, protects us.
    baseline = transport(failure="locked", suppression=False).run()
    assert baseline.error is None and baseline.attempts["primary_router"] == 2
    assert [h["x-stainless-retry-count"] for h in baseline.headers[:2]] == ["0", "0"]
    assert transport.sleeps


def test_explicit_conversation_id_also_cannot_enable_lock_retry(transport):
    wire = transport(failure="locked", failed_component="synthesis")
    token = _wire.set(wire)
    try:
        with pytest.raises(Exception) as caught:
            model_execution.run_model(Agent(name="offline"), "offline", max_turns=1, conversation_id="conv_offline")
        assert type(caught.value).__name__ == "BadRequestError"
    finally:
        _wire.reset(token)
    assert wire.attempts == {"synthesis": 1}
    assert wire.bodies[0]["conversation"] == "conv_offline"
    assert not transport.sleeps


def test_exhausted_budget_prevents_actual_transport_dispatch(transport):
    wire = transport()
    wire.run(budget=RequestBudget(0, clock=wire.clock))
    assert isinstance(wire.error, RequestDeadlineExceeded)
    assert not wire.attempts and not wire.tool_calls and wire.result is None
    assert recorded_attempts(wire) == []


@pytest.mark.parametrize("component", COMPONENTS)
def test_late_wire_response_is_abandoned_with_usage_preserved(transport, component):
    wire = transport(late_component=component, recovery=component == "recovery_planner")
    wire.run(budget=RequestBudget(500, clock=wire.clock))
    assert isinstance(wire.error, RequestDeadlineExceeded)
    assert wire.error.component == component and wire.result is None
    assert wire.attempts[component] == 1
    attempt = recorded_attempts(wire)[-1]
    assert attempt["status"] == "completed" and attempt["result_abandoned"] and attempt["usage_known"]
    assert wire.observed["deadline_exhausted"] and wire.observed["result_abandoned"]
    assert not wire.observed["cancellation_requested"] and not wire.observed["cancellation_observed"]
    assert_configuration_evidence(wire)
    assert len(wire.tool_calls) == (1 if component == "synthesis" else 0)


def normalized_bodies(wire):
    bodies = json.loads(json.dumps(wire.bodies))
    for body in bodies:
        for item in body["input"]:
            item.pop("call_id", None)  # Runtime-generated opaque correlation IDs.
    return bodies


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("model_name", [None, "gpt-4.1-mini"])
def test_success_preserves_model_prompts_tools_and_outputs(transport, monkeypatch, recovery, model_name):
    if model_name:
        monkeypatch.setenv("OPENAI_DEFAULT_MODEL", model_name)
    before_model = support.support_agent.model
    before_settings = support.support_agent.model_settings.to_json_dict()
    baseline = transport(recovery=recovery, suppression=False).run()
    controlled = transport(recovery=recovery).run()
    assert baseline.error is controlled.error is None
    assert normalized_bodies(controlled) == normalized_bodies(baseline)
    assert all(body["model"] == get_default_model() for body in controlled.bodies)
    assert support.support_agent.model is before_model
    assert support.support_agent.model_settings.to_json_dict() == before_settings
    assert controlled.tool_calls == baseline.tool_calls
    assert controlled.result.final_output == baseline.result.final_output
    assert controlled.result.context_wrapper.usage.total_tokens == baseline.result.context_wrapper.usage.total_tokens
    trace = controlled.result.context_wrapper.context
    assert not trace.missing_required
    assert trace.executions[0].output["status"] == "processing"
    assert PRIVATE not in json.dumps(trace.executions[0].output)
    assert all(body["tools"] == [] for body in controlled.bodies)


def test_unrelated_client_retains_its_default_retry_configuration(transport):
    wire = transport(failure="500").run()
    assert wire.attempts == {"primary_router": 1}
    wire.attempts.clear()
    wire.failed_component = "synthesis"

    async def unrelated_call():
        return await wire.client.responses.create(model=get_default_model(), input="offline unrelated call")

    result = asyncio.run(unrelated_call())
    assert result.status == "completed"
    assert wire.attempts == {"synthesis": 2}  # Same base client, outside the scoped SDK run.
    assert wire.client.max_retries == 2 and transport.sleeps


def test_concurrent_requests_do_not_share_attempt_state(transport):
    barrier = Barrier(2)
    failed = transport(failure="429", barrier=barrier)
    successful = transport(recovery=True, barrier=barrier)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(failed.run)
        second = executor.submit(successful.run)
        first.result()
        second.result()
    assert failed.error is not None and failed.attempts == {"primary_router": 1}
    assert successful.error is None and successful.attempts == dict.fromkeys(COMPONENTS, 1)
    assert failed.observed["request_id"] != successful.observed["request_id"]
    assert not failed.tool_calls and len(successful.tool_calls) == 1
    assert_configuration_evidence(failed)
    assert_configuration_evidence(successful)
    assert _wire.get() is None and not transport.sleeps


def test_settings_are_fresh_and_only_override_retry():
    first, second = model_execution.model_run_config(), model_execution.model_run_config()
    assert first is not second and first["model_settings"] is not second["model_settings"]
    settings = first["model_settings"]
    assert settings.retry.max_retries == 0
    assert settings.retry.policy is None and settings.retry.backoff is None
    assert set(first) == {"model_settings"}
    default = ModelSettings().to_json_dict()
    supplied = settings.to_json_dict()
    supplied.pop("retry")
    default.pop("retry")
    assert supplied == default


def test_injected_runner_does_not_claim_verified_retry_configuration():
    result = SimpleNamespace(context_wrapper=SimpleNamespace(usage=Usage(requests=1, total_tokens=10)))
    runner = Mock(return_value=result)
    agent = Agent(name="injected")

    @telemetry.observe_request
    def request():
        with telemetry.observe("primary_router"):
            telemetry.model_call(agent, default_resolution=False)
            response = model_execution.run_model(agent, "input", run=runner, max_turns=1)
            telemetry.model_result(response)
        return response

    assert request() is result
    runner.assert_called_once_with(agent, "input", max_turns=1)
    recorded = result.context_wrapper.production_telemetry.component_spans[0]
    assert recorded.lower_layer_retries_configured is None
    assert recorded.attempts[0].lower_layer_retries_configured is None
    assert recorded.attempts[0].transport_attempts_observed is None
