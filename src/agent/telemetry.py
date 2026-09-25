"""Local, non-authoritative production observations. Never records request payloads.

COMPLETE means usage was returned for every observed logical model call, not
that unobservable HTTP attempts are accounted for. No transport retries are
inferred from SDK request counts. Context tokens are reset on every exit path.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
import re
import time
from uuid import uuid4


class FailureCategory(str, Enum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    AUTHORIZATION_FAILURE = "AUTHORIZATION_FAILURE"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_ERROR = "TOOL_ERROR"
    PROJECTION_ERROR = "PROJECTION_ERROR"
    REQUIRED_OPERATION_INCOMPLETE = "REQUIRED_OPERATION_INCOMPLETE"
    PROHIBITED_OPERATION_ATTEMPT = "PROHIBITED_OPERATION_ATTEMPT"
    MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    UNKNOWN_EXTERNAL_FAILURE = "UNKNOWN_EXTERNAL_FAILURE"
    UNKNOWN_INTERNAL_FAILURE = "UNKNOWN_INTERNAL_FAILURE"


class UsageCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


def exception_category(error, component):
    """Map types, not exception messages/bodies; wrappers retain their cause."""
    if component == "projection":
        return FailureCategory.PROJECTION_ERROR
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__ or error.__context__
    for candidate in reversed(chain):
        names = {base.__name__ for base in type(candidate).__mro__}
        mapping = (
            ({"APITimeoutError", "ModelTimeoutError", "TimeoutError"}, FailureCategory.TIMEOUT),
            ({"RateLimitError"}, FailureCategory.RATE_LIMIT),
            ({"AuthenticationError"}, FailureCategory.AUTHENTICATION_FAILURE),
            ({"PermissionDeniedError"}, FailureCategory.AUTHORIZATION_FAILURE),
            ({"APIConnectionError", "ConnectionError"}, FailureCategory.NETWORK_ERROR),
            ({"APIStatusError"}, FailureCategory.PROVIDER_ERROR),
            ({"ValidationError", "ModelBehaviorError"}, FailureCategory.INVALID_MODEL_OUTPUT),
            ({"MaxTurnsExceeded"}, FailureCategory.MODEL_PROTOCOL_ERROR),
            ({"UserError", "OpenAIError"}, FailureCategory.CONFIGURATION_ERROR),
        )
        for types, category in mapping:
            if names & types:
                return category
        if component in {"primary_router", "recovery_planner"} and isinstance(candidate, ValueError):
            return FailureCategory.INVALID_MODEL_OUTPUT
    if component == "tool":
        return FailureCategory.TOOL_ERROR
    if chain and type(chain[0]).__name__ == "ExecutionFailure":
        trace = getattr(chain[0], "trace", None)
        if getattr(trace, "prohibited_attempts", None):
            return FailureCategory.PROHIBITED_OPERATION_ATTEMPT
        return FailureCategory.REQUIRED_OPERATION_INCOMPLETE
    if chain and type(chain[-1]).__module__.split(".")[0] in {"openai", "httpx", "httpx2", "agents"}:
        return FailureCategory.UNKNOWN_EXTERNAL_FAILURE
    return FailureCategory.UNKNOWN_INTERNAL_FAILURE


@dataclass
class ComponentSpan:
    component: str
    start_offset_ms: float
    status: str = "running"
    duration_ms: float | None = None
    configured_model: str | None = None
    resolved_model: str | None = None
    logical_model_calls: int = 0
    sdk_visible_requests: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_available: bool = False
    http_retry_count: int | None = None
    retry_source: str | None = None
    failure_category: FailureCategory | None = None
    sanitized_exception_type: str | None = None
    operation_id: str | None = None
    tool: str | None = None
    model_responses: int | None = None
    response_usage: list[dict] = field(default_factory=list)
    context_characters: dict = field(default_factory=dict)


@dataclass
class ProductionExecutionTelemetry:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    # Caller-supplied opaque label only; never populated with user input.
    external_label: str | None = None
    started_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_latency_ms: float | None = None
    terminal_status: str = "running"
    business_outcome: str | None = None
    terminal_failure_component: str | None = None
    terminal_failure_category: FailureCategory | None = None
    terminal_exception_type: str | None = None
    usage_completeness: UsageCompleteness = UsageCompleteness.UNAVAILABLE
    component_spans: list[ComponentSpan] = field(default_factory=list)
    planning_summary: dict = field(default_factory=dict)
    required_operation_summary: dict = field(default_factory=dict)
    observed_usage: dict = field(default_factory=dict)
    observation_incomplete: bool = False

    def snapshot(self):
        """JSON-safe allowlisted observations; no source plans or tool payloads."""
        return asdict(self)


_request = ContextVar("agentguard_telemetry", default=None)
_span = ContextVar("agentguard_component_span", default=None)
_started = ContextVar("agentguard_telemetry_started", default=None)
_failure = ContextVar("agentguard_first_failure", default=None)


def best_effort(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            record = _request.get()
            if record is not None:
                record.observation_incomplete = True
            return None
    return safe


@best_effort
def snapshot(record):
    return record.snapshot() if isinstance(record, ProductionExecutionTelemetry) else None


@best_effort
def _begin(component):
    record = _request.get()
    if record is None:
        return None
    span = ComponentSpan(component, (time.perf_counter() - _started.get()) * 1000)
    record.component_spans.append(span)
    return span


@best_effort
def fail(error=None, category=None, span=None):
    span = span or _span.get()
    if span is None:
        return
    if error is not None and span.logical_model_calls and not span.usage_available:
        candidate, seen = error, set()
        while candidate is not None and id(candidate) not in seen:
            seen.add(id(candidate))
            details = getattr(candidate, "run_data", None)
            if details is not None:
                model_result(details)
                break
            candidate = candidate.__cause__ or candidate.__context__
    span.status = "failed"
    span.failure_category = category or exception_category(error, span.component)
    if error is not None:
        name = type(error).__name__
        span.sanitized_exception_type = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", name) else "Exception"
    if _failure.get() is None:
        _failure.set(span)


@best_effort
def _end(span):
    if span is not None:
        if span.duration_ms is None:
            span.duration_ms = (time.perf_counter() - _started.get()) * 1000 - span.start_offset_ms
        if span.status == "running":
            span.status = "completed"


@contextmanager
def observe(component):
    span = _begin(component)
    token = _span.set(span)
    try:
        yield span
    except BaseException as error:
        fail(error, span=span)
        raise
    finally:
        _end(span)
        _span.reset(token)


@best_effort
def model_call(agent, *, default_resolution=True):
    span = _span.get()
    if span is None:
        return
    span.logical_model_calls += 1
    configured = agent.model
    span.configured_model = configured if isinstance(configured, str) else None
    if default_resolution:
        # Identity adapter only: does not instantiate a model or alter selection.
        from agents.models.default_models import get_default_model
        span.resolved_model = (configured if isinstance(configured, str) else
                               get_default_model() if configured is None else
                               getattr(configured, "model", None))
        if not isinstance(span.resolved_model, str):
            span.resolved_model = None
    from agents.agent_output import AgentOutputSchema
    import json
    span.context_characters = {
        "instructions": len(agent.instructions) if isinstance(agent.instructions, str) else None,
        "tool_definitions": len(json.dumps([
            {"name": tool.name, "description": tool.description, "parameters": tool.params_json_schema}
            for tool in agent.tools], ensure_ascii=False)) if agent.tools else 0,
        "output_schema": len(json.dumps(AgentOutputSchema(agent.output_type).json_schema())) if agent.output_type else 0,
    }


@best_effort
def usage(available):
    span = _span.get()
    if span is None or available is None:
        return
    for target, source in (("sdk_visible_requests", "requests"), ("input_tokens", "input_tokens"),
                           ("output_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        value = getattr(available, source, None)
        setattr(span, target, value if type(value) is int and value >= 0 else None)
    # SDK default-zero Usage without token evidence must not claim zero cost.
    span.usage_available = (all(getattr(span, name) is not None for name in
                               ("input_tokens", "output_tokens", "total_tokens"))
                            and (span.total_tokens > 0 or bool(getattr(available, "request_usage_entries", None))))
    if not span.usage_available:
        span.input_tokens = span.output_tokens = span.total_tokens = None


@best_effort
def model_result(result):
    usage(result.context_wrapper.usage)
    span = _span.get()
    responses = getattr(result, "raw_responses", None)
    if span is not None and isinstance(responses, list):
        span.model_responses = len(responses)
        for response in responses:
            counters = response.usage
            # Explicit allowlist: SDK responses themselves can contain customer data.
            entry = {key: getattr(counters, key, None) for key in
                     ("requests", "input_tokens", "output_tokens", "total_tokens")}
            for detail, keys in (("input_tokens_details", ("cached_tokens",)),
                                 ("output_tokens_details", ("reasoning_tokens",))):
                source = getattr(counters, detail, None)
                entry[detail] = {key: getattr(source, key, None) for key in keys}
            span.response_usage.append(entry)


@best_effort
def planning(result):
    record = _request.get()
    if record is not None:
        record.planning_summary = {"recovery_triggered": result.completeness_review_triggered,
                                   "recovery_count": result.recovery_count,
                                   "plan_source": result.plan_source}


@best_effort
def policy(result):
    record = _request.get()
    if record is not None:
        record.business_outcome = ("CLARIFICATION_REQUIRED" if result.needs_clarification else
                                   "NO_AUTHORIZED_OPERATIONS" if not result.grants else "AUTHORIZED")


@best_effort
def operations(trace):
    record = _request.get()
    if record is not None:
        required = [item for item in trace.executions if item.operation.mode == "required"]
        record.required_operation_summary = {
            "required": [item.call_id for item in required],
            "completed": [item.call_id for item in required if item.status == "completed"],
            "incomplete": [item.call_id for item in required if item.status != "completed"],
        }


@best_effort
def operation(item):
    span = _span.get()
    if span is not None:
        span.operation_id, span.tool = item.call_id, item.operation.tool
        span.duration_ms = item.latency_ms  # ExecutionTrace owns operation timing.
        if item.status == "failed" and span.failure_category is None:
            # Preserve a classified exception (for example TIMEOUT); the operation
            # summary is only a fallback for failures without an exception.
            fail(category=(FailureCategory.TOOL_NOT_FOUND if item.error == "missing_implementation"
                           else FailureCategory.TOOL_ERROR), span=span)


@best_effort
def _finish(record, started, error):
    record.total_latency_ms = (time.perf_counter() - started) * 1000
    record.terminal_status = "failed" if error is not None else "completed"
    observed = [s for s in record.component_spans if s.logical_model_calls or s.sdk_visible_requests is not None]
    known = [s for s in observed if s.usage_available]
    record.observed_usage = {
        key: sum(values) if values else None
        for key in ("sdk_visible_requests", "input_tokens", "output_tokens", "total_tokens")
        for values in [[getattr(span, key) for span in observed if getattr(span, key) is not None]]
    }
    record.usage_completeness = (UsageCompleteness.COMPLETE if observed and len(known) == len(observed)
                                 and not record.observation_incomplete else
                                 UsageCompleteness.PARTIAL if known else UsageCompleteness.UNAVAILABLE)
    if error is not None:
        # Innermost failure finishes first. Prefer its cause over a parent obligation failure.
        cause = _failure.get()
        record.terminal_failure_component = cause.component if cause else "request"
        record.terminal_failure_category = cause.failure_category if cause else exception_category(error, "request")
        record.terminal_exception_type = cause.sanitized_exception_type if cause else type(error).__name__


@best_effort
def _attach(target, record):
    target.production_telemetry = record


def observe_request(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Request creation is also optional; observation failures cannot reject work.
        try:
            record, started = ProductionExecutionTelemetry(), time.perf_counter()
            label = kwargs.get("request_label")
            record.external_label = label if isinstance(label, str) else None
        except Exception:
            record, started = None, None
        request_token = _request.set(record)
        start_token = _started.set(started)
        span_token = _span.set(None)
        failure_token = _failure.set(None)
        try:
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                if record is not None:
                    _finish(record, started, error)
                    _attach(error, record)
                raise
            if record is not None:
                _finish(record, started, None)
                _attach(result.context_wrapper, record)
            return result
        finally:
            _failure.reset(failure_token)
            _span.reset(span_token)
            _started.reset(start_token)
            _request.reset(request_token)
    return run
