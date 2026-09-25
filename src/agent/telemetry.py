"""Local, non-authoritative production observations. Never records request payloads.

COMPLETE means usage was returned for every observed model attempt, not
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

from src.agent.request_budget import (
    AdmissionEvidence, BudgetAdmissionError, AdmissionDenial, CancellationEvidence,
    ReliabilityState, RequestBudget, RequestBudgetRejected, RequestDeadlineExceeded,
)


class FailureCategory(str, Enum):
    TIMEOUT = ReliabilityState.TIMEOUT.value
    DEADLINE_EXHAUSTED = ReliabilityState.DEADLINE_EXHAUSTED.value
    CANCELLED = ReliabilityState.CANCELLED.value
    RESULT_ABANDONED = ReliabilityState.RESULT_ABANDONED.value
    REMOTE_OUTCOME_UNKNOWN = ReliabilityState.REMOTE_OUTCOME_UNKNOWN.value
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
        if isinstance(candidate, RequestDeadlineExceeded):
            return FailureCategory.DEADLINE_EXHAUSTED
        if isinstance(candidate, RequestBudgetRejected) and candidate.evidence.denial_reason in {
            AdmissionDenial.CANCELLED, AdmissionDenial.CANCELLATION_REQUESTED,
        }:
            return FailureCategory.CANCELLED
        if isinstance(candidate, BudgetAdmissionError) and candidate.evidence.denial_reason == AdmissionDenial.DEADLINE_EXHAUSTED:
            return FailureCategory.DEADLINE_EXHAUSTED
        names = {base.__name__ for base in type(candidate).__mro__}
        mapping = (
            ({"CancelledError"}, FailureCategory.CANCELLED),
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
class AttemptTelemetry:
    logical_call_id: str
    component: str
    attempt_number: int
    started_offset_ms: float
    duration_ms: float | None = None
    remaining_budget_before_ms: float | None = None
    remaining_budget_after_ms: float | None = None
    retry_eligible: bool | None = None  # No eligibility policy evaluated yet.
    retry_performed: bool = False
    retry_reason: str | None = None
    retry_denial_reason: str | None = None
    retry_delay_ms: float | None = None
    status: str = "running"
    failure_category: FailureCategory | None = None
    exception_type: str | None = None
    sdk_visible_requests: int | None = None
    http_retry_count: int | None = None
    transport_attempts_observed: int | None = None
    lower_layer_retries_configured: bool | None = None
    returned_usage: dict = field(default_factory=dict)
    usage_known: bool = False
    usage_completeness: UsageCompleteness = UsageCompleteness.UNAVAILABLE
    late_completion: bool = False
    result_accepted: bool | None = None
    result_abandoned: bool = False
    delivery_certainty: str | None = None
    provider_status_code: int | None = None
    retry_after_ms: float | None = None
    retry_after_invalid: bool = False


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
    transport_attempts_observed: int | None = None
    lower_layer_retries_configured: bool | None = None
    retry_source: str | None = None
    failure_category: FailureCategory | None = None
    sanitized_exception_type: str | None = None
    operation_id: str | None = None
    tool: str | None = None
    model_responses: int | None = None
    response_usage: list[dict] = field(default_factory=list)
    context_characters: dict = field(default_factory=dict)
    remaining_budget_before_ms: float | None = None
    remaining_budget_after_ms: float | None = None
    allocated_allowance_ms: float | None = None
    timeout_deadline_source: str | None = None
    attempts: list[AttemptTelemetry] = field(default_factory=list)
    stage_admitted: bool | None = None
    admission_denial_reason: AdmissionDenial | None = None
    late_completion: bool = False
    result_accepted: bool | None = None
    result_abandoned: bool = False


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
    deadline_budget_ms: float | None = None
    deadline_monotonic: float | None = None
    deadline_exhausted: bool = False
    cancellation_requested: bool = False
    cancellation_observed: bool = False
    cancellation_evidence: CancellationEvidence | None = None
    result_abandoned: bool = False
    remote_outcome_unknown: bool = False
    unknown_usage_attempt_count: int = 0
    recovery_admission: AdmissionEvidence | None = None
    retry_policy_enabled: bool = False
    retry_allowance_initial: int = 0
    retry_allowance_consumed: int = 0
    retry_allowance_remaining: int = 0
    retry_attempts_total: int = 0
    retry_exhausted: bool = False

    def snapshot(self):
        """JSON-safe allowlisted observations; no source plans or tool payloads."""
        return asdict(self)


_request = ContextVar("agentguard_telemetry", default=None)
_span = ContextVar("agentguard_component_span", default=None)
_started = ContextVar("agentguard_telemetry_started", default=None)
_failure = ContextVar("agentguard_first_failure", default=None)
_budget = ContextVar("agentguard_request_budget", default=None)


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
    if _budget.get() is not None:
        span.remaining_budget_before_ms = _budget.get().remaining_ms()
    if component == "recovery_planner":
        # Unlimited recovery is unevaluated; finite admission updates this evidence.
        record.recovery_admission = AdmissionEvidence(span.remaining_budget_before_ms)
    return span


@best_effort
def fail(error=None, category=None, span=None):
    span = span or _span.get()
    if span is None:
        return
    if isinstance(error, RequestBudgetRejected) and error.completed and span.status == "failed":
        return  # Preserve an actual work failure; request rejection is recorded separately.
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
    if isinstance(error, RequestBudgetRejected) and error.component == span.component:
        span.status = "completed" if error.completed else "not_started"
    span.failure_category = category or exception_category(error, span.component)
    if error is not None:
        name = type(error).__name__
        span.sanitized_exception_type = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", name) else "Exception"
        from asyncio import CancelledError
        if isinstance(error, CancelledError) and _budget.get() is not None:
            _budget.get().observe_cancellation(CancellationEvidence.LOCAL_TASK_CANCELLED)
    if _failure.get() is None:
        _failure.set(span)


@best_effort
def _end(span):
    if span is not None:
        if span.duration_ms is None:
            span.duration_ms = (time.perf_counter() - _started.get()) * 1000 - span.start_offset_ms
        if span.status == "running":
            span.status = "completed"
        if _budget.get() is not None:
            span.remaining_budget_after_ms = _budget.get().remaining_ms()
        _end_attempt(span)


def _end_attempt(span):
    if not span.attempts:
        return
    attempt = span.attempts[-1]
    if attempt.status != "running":
        if attempt.status == "completed":
            attempt.late_completion = span.late_completion
            attempt.result_accepted = span.result_accepted
            attempt.result_abandoned = span.result_abandoned
            if span.status == "failed":
                # Local validation after a returned model result is still part of
                # the logical call, but it must never trigger transport replay.
                attempt.status = "failed"
                attempt.failure_category = span.failure_category
                attempt.exception_type = span.sanitized_exception_type
        return
    attempt.duration_ms = (time.perf_counter() - _started.get()) * 1000 - attempt.started_offset_ms
    attempt.status = "completed" if span.status == "running" else span.status
    # Result rejection is a request failure, not failure of completed model work.
    attempt.failure_category = span.failure_category if attempt.status == "failed" else None
    attempt.exception_type = span.sanitized_exception_type if attempt.status == "failed" else None
    attempt.late_completion = span.late_completion
    attempt.result_accepted = span.result_accepted
    attempt.result_abandoned = span.result_abandoned
    if _budget.get() is not None:
        attempt.remaining_budget_after_ms = _budget.get().remaining_ms()


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
def stage_admission(evidence):
    span = _span.get()
    if span is not None:
        span.remaining_budget_before_ms = evidence.remaining_budget_ms
        span.allocated_allowance_ms = evidence.allowance_ms
        span.timeout_deadline_source = "request_budget"
        span.stage_admitted = evidence.admitted
        span.admission_denial_reason = evidence.denial_reason
        if span.component == "recovery_planner" and _request.get() is not None:
            _request.get().recovery_admission = evidence


@best_effort
def stage_acceptance(evidence):
    span = _span.get()
    if span is not None:
        span.remaining_budget_after_ms = evidence.remaining_budget_ms
        span.result_accepted = evidence.admitted
        span.result_abandoned = not evidence.admitted
        span.late_completion = evidence.denial_reason == AdmissionDenial.DEADLINE_EXHAUSTED


@best_effort
def model_call(agent, *, default_resolution=True):
    span = _span.get()
    if span is None:
        return
    _end_attempt(span)
    attempt = AttemptTelemetry(uuid4().hex, span.component, 1,
                               (time.perf_counter() - _started.get()) * 1000)
    span.attempts.append(attempt)
    if _budget.get() is not None:
        attempt.remaining_budget_before_ms = _budget.get().remaining_ms()
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
def retry_configuration(*, lower_layer_retries_configured):
    """Configuration evidence only; never infer a transport count from it."""
    span = _span.get()
    if span is not None:
        span.lower_layer_retries_configured = lower_layer_retries_configured
        if span.attempts:
            span.attempts[-1].lower_layer_retries_configured = lower_layer_retries_configured


@best_effort
def retry_summary(state):
    record = _request.get()
    if record is not None:
        record.retry_policy_enabled = state.policy.enabled
        record.retry_allowance_initial = state.policy.shared_extra_attempts_per_request
        record.retry_allowance_consumed = state.consumed
        record.retry_allowance_remaining = state.remaining
        record.retry_attempts_total = state.consumed
        record.retry_exhausted = state.exhausted


def _close_current_attempt(status):
    span = _span.get()
    if span is None or not span.attempts:
        return None
    attempt = span.attempts[-1]
    attempt.status = status
    attempt.duration_ms = (time.perf_counter() - _started.get()) * 1000 - attempt.started_offset_ms
    if _budget.get() is not None:
        attempt.remaining_budget_after_ms = _budget.get().remaining_ms()
    return attempt


@best_effort
def attempt_failure(error, evidence):
    attempt = _close_current_attempt("failed")
    if attempt is not None:
        attempt.failure_category = evidence.category
        name = type(error).__name__
        attempt.exception_type = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", name) else "Exception"
        attempt.delivery_certainty = evidence.delivery
        attempt.provider_status_code = evidence.status_code
        attempt.retry_after_ms = evidence.retry_after_ms
        attempt.retry_after_invalid = evidence.guidance_invalid


@best_effort
def attempt_success():
    _close_current_attempt("completed")


@best_effort
def retry_decision(decision, *, performed=False):
    span = _span.get()
    if span is not None and span.attempts:
        attempt = span.attempts[-1]
        attempt.retry_eligible = decision.eligible
        attempt.retry_performed = performed
        attempt.retry_reason = attempt.failure_category
        attempt.retry_denial_reason = decision.denial_reason
        attempt.retry_delay_ms = decision.delay_ms
        if performed:
            span.retry_source = "agentguard"


@best_effort
def next_attempt():
    span = _span.get()
    if span is not None and span.attempts:
        previous = span.attempts[-1]
        attempt = AttemptTelemetry(previous.logical_call_id, span.component, previous.attempt_number + 1,
                                   (time.perf_counter() - _started.get()) * 1000)
        span.attempts.append(attempt)
        if _budget.get() is not None:
            attempt.remaining_budget_before_ms = _budget.get().remaining_ms()
        span.usage_available = False
        span.sdk_visible_requests = span.input_tokens = span.output_tokens = span.total_tokens = None


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
    if span.attempts and span.attempts[-1].status == "running":
        attempt = span.attempts[-1]
        attempt.sdk_visible_requests = span.sdk_visible_requests
        attempt.returned_usage = {name: getattr(span, name) for name in
                                  ("input_tokens", "output_tokens", "total_tokens")}
        attempt.usage_known = span.usage_available
        attempt.usage_completeness = (UsageCompleteness.COMPLETE if span.usage_available
                                      else UsageCompleteness.UNAVAILABLE)


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
    budget = _budget.get()
    if budget is not None:
        record.deadline_budget_ms = budget.original_budget_ms
        record.deadline_monotonic = budget.deadline_monotonic
        record.deadline_exhausted = budget.exhausted()
        record.cancellation_requested = budget.cancellation_requested
        record.cancellation_observed = budget.cancellation_observed
        record.cancellation_evidence = budget.cancellation_evidence
        record.result_abandoned = budget.result_abandoned
        record.remote_outcome_unknown = budget.remote_outcome_unknown
    record.unknown_usage_attempt_count = sum(not a.usage_known for s in record.component_spans for a in s.attempts)
    for span in record.component_spans:
        if len(span.attempts) > 1:
            for key in ("sdk_visible_requests", "input_tokens", "output_tokens", "total_tokens"):
                values = [(a.sdk_visible_requests if key == "sdk_visible_requests" else a.returned_usage.get(key))
                          for a in span.attempts]
                known_values = [v for v in values if v is not None]
                setattr(span, key, sum(known_values) if known_values else None)
            span.usage_available = all(a.usage_known for a in span.attempts)
    observed = [s for s in record.component_spans if s.logical_model_calls or s.sdk_visible_requests is not None]
    known = [s for s in observed if s.usage_available]
    record.observed_usage = {
        key: sum(values) if values else None
        for key in ("sdk_visible_requests", "input_tokens", "output_tokens", "total_tokens")
        for values in [[getattr(span, key) for span in observed if getattr(span, key) is not None]]
    }
    record.usage_completeness = (UsageCompleteness.COMPLETE if observed and len(known) == len(observed)
                                 and not record.observation_incomplete else
                                 UsageCompleteness.PARTIAL if any(s.total_tokens is not None for s in observed)
                                 else UsageCompleteness.UNAVAILABLE)
    if error is not None:
        # Innermost failure finishes first. Prefer its cause over a parent obligation failure.
        cause = _failure.get()
        record.terminal_failure_component = cause.component if cause else "request"
        record.terminal_failure_category = cause.failure_category if cause else exception_category(error, "request")
        record.terminal_exception_type = cause.sanitized_exception_type if cause else type(error).__name__
        if isinstance(error, RequestBudgetRejected):
            # An earlier work failure must not mask the terminal admission outcome.
            record.terminal_failure_component = error.component
            record.terminal_failure_category = exception_category(error, error.component)
            record.terminal_exception_type = type(error).__name__


@best_effort
def _attach(target, record):
    target.production_telemetry = record


def observe_request(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Request creation is also optional; observation failures cannot reject work.
        try:
            record, started = ProductionExecutionTelemetry(), time.perf_counter()
            budget = kwargs.get("request_budget")
            if budget is None:
                budget = RequestBudget()
            record.request_id = budget.request_id
            label = kwargs.get("request_label")
            record.external_label = label if isinstance(label, str) else None
        except Exception:
            record, started, budget = None, None, None
        request_token = _request.set(record)
        start_token = _started.set(started)
        span_token = _span.set(None)
        failure_token = _failure.set(None)
        budget_token = _budget.set(budget)
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
            _budget.reset(budget_token)
            _failure.reset(failure_token)
            _span.reset(span_token)
            _started.reset(start_token)
            _request.reset(request_token)
    return run
