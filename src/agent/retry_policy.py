"""Explicit, disabled-by-default model retry decisions and request allowance."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import Enum
from functools import wraps
import math
import time

import httpx2
from openai import APIStatusError

from src.agent import telemetry
from src.agent.telemetry import FailureCategory as Category


class DeliveryCertainty(str, Enum):
    NOT_SENT = "NOT_SENT"
    SENT_OUTCOME_UNKNOWN = "SENT_OUTCOME_UNKNOWN"
    KNOWN_FAILED = "KNOWN_FAILED"


def milliseconds(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite non-negative milliseconds")


@dataclass(frozen=True)
class Backoff:
    initial_delay_ms: float
    multiplier: float = 1.0
    jitter: Callable[[float, int], float] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        milliseconds(self.initial_delay_ms, "initial_delay_ms")
        if type(self.multiplier) not in (int, float) or not math.isfinite(self.multiplier) or self.multiplier < 1:
            raise ValueError("multiplier must be finite and at least one")

    def delay_ms(self, failed_attempt: int, ceiling: float) -> float:
        try:
            delay = min(ceiling, self.initial_delay_ms * self.multiplier ** (failed_attempt - 1))
        except OverflowError:
            delay = ceiling
        if self.jitter is not None:
            delay = self.jitter(delay, failed_attempt)
            milliseconds(delay, "jitter result")
        return min(delay, ceiling)


@dataclass(frozen=True)
class ModelRetryPolicy:
    enabled: bool = False
    max_attempts: int = 1
    shared_extra_attempts_per_request: int = 0
    eligible_failure_categories: frozenset[Category] = frozenset({
        Category.NETWORK_ERROR, Category.RATE_LIMIT, Category.PROVIDER_ERROR, Category.TIMEOUT,
    })
    transient_http_statuses: frozenset[int] = frozenset({500, 502, 503, 504})
    backoff: Backoff | None = None
    maximum_retry_delay_ms: float | None = None
    minimum_attempt_budget_ms: float | None = None
    downstream_reserve_ms: float = 0
    retry_after_policy: str = "honor"  # "deny" declines retries with server guidance.

    @classmethod
    def disabled(cls):
        return cls()

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for name, minimum in (("max_attempts", 1), ("shared_extra_attempts_per_request", 0)):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        object.__setattr__(self, "eligible_failure_categories", frozenset(self.eligible_failure_categories))
        if any(category not in set(Category) for category in self.eligible_failure_categories):
            raise ValueError("eligible_failure_categories must contain registered failure categories")
        object.__setattr__(self, "transient_http_statuses", frozenset(self.transient_http_statuses))
        if any(type(code) is not int or not 500 <= code <= 599 for code in self.transient_http_statuses):
            raise ValueError("transient_http_statuses must contain only 5xx status codes")
        if self.retry_after_policy not in {"honor", "deny"}:
            raise ValueError("retry_after_policy must be honor or deny")
        milliseconds(self.downstream_reserve_ms, "downstream_reserve_ms")
        for name in ("maximum_retry_delay_ms", "minimum_attempt_budget_ms"):
            value = getattr(self, name)
            if value is not None:
                milliseconds(value, name)
        if self.enabled and (not isinstance(self.backoff, Backoff) or self.maximum_retry_delay_ms is None
                             or self.minimum_attempt_budget_ms is None or self.minimum_attempt_budget_ms <= 0):
            raise ValueError("Enabled retries require explicit backoff, maximum delay and positive useful-attempt budget")


def error_chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


@dataclass(frozen=True)
class FailureEvidence:
    category: Category
    delivery: DeliveryCertainty
    status_code: int | None = None
    retry_after_ms: float | None = None
    guidance_invalid: bool = False


def classify_failure(error, component, *, wall_clock=time.time) -> FailureEvidence:
    """Use typed transport causes/statuses, never text to infer safe delivery."""
    category = telemetry.exception_category(error, component)
    chain = list(error_chain(error))
    status_error = next((e for e in chain if isinstance(e, APIStatusError)), None)
    if status_error is not None:
        headers = status_error.response.headers
        raw_ms, raw_seconds = headers.get("retry-after-ms"), headers.get("retry-after")
        delay, invalid = None, False
        if raw_ms is not None or raw_seconds is not None:
            try:
                if raw_ms is not None:
                    delay = float(raw_ms)
                else:
                    try:
                        delay = float(raw_seconds) * 1000
                    except ValueError:
                        date = parsedate_to_datetime(raw_seconds)
                        if date.tzinfo is None:
                            raise ValueError("HTTP date must have a timezone")
                        delay = max(0.0, (date.timestamp() - wall_clock()) * 1000)
                milliseconds(delay, "Retry-After")
            except (ValueError, TypeError, OverflowError):
                delay, invalid = None, True
        return FailureEvidence(category, DeliveryCertainty.KNOWN_FAILED, status_error.status_code, delay, invalid)
    # Connect errors/timeouts precede HTTP transmission. Read/write/protocol failures,
    # and SDK connection errors without a typed transport cause, prove no such thing.
    # Prefer the outermost transport evidence. An earlier connect failure in an
    # exception's context cannot make a later read/write failure safe to replay.
    transport_error = next((e for e in chain if isinstance(e, httpx2.TransportError)), None)
    not_sent = isinstance(transport_error, (httpx2.ConnectError, httpx2.ConnectTimeout))
    delivery = DeliveryCertainty.NOT_SENT if not_sent else DeliveryCertainty.SENT_OUTCOME_UNKNOWN
    return FailureEvidence(category, delivery)


@dataclass(frozen=True)
class RetryDecision:
    eligible: bool
    admitted: bool
    denial_reason: str | None = None
    delay_ms: float | None = None


@dataclass
class RequestRetryState:
    policy: ModelRetryPolicy
    sleeper: Callable[[float], None] = time.sleep
    wall_clock: Callable[[], float] = time.time
    consumed: int = 0
    exhausted: bool = False

    @property
    def remaining(self):
        return self.policy.shared_extra_attempts_per_request - self.consumed

    def decide(self, evidence: FailureEvidence, failed_attempt: int) -> RetryDecision:
        p = self.policy
        safe = (evidence.category in {Category.NETWORK_ERROR, Category.TIMEOUT}
                and evidence.delivery == DeliveryCertainty.NOT_SENT or
                evidence.category == Category.RATE_LIMIT and evidence.status_code == 429 or
                evidence.category == Category.PROVIDER_ERROR and evidence.status_code in p.transient_http_statuses)
        eligible = safe and evidence.category in p.eligible_failure_categories
        if not p.enabled:
            return RetryDecision(eligible, False, "POLICY_DISABLED")
        if not eligible:
            reason = "DELIVERY_UNKNOWN" if evidence.category in {Category.NETWORK_ERROR, Category.TIMEOUT} else "FAILURE_NOT_ELIGIBLE"
            return RetryDecision(False, False, reason)
        if failed_attempt >= p.max_attempts:
            self.exhausted = True
            return RetryDecision(True, False, "MAX_ATTEMPTS_EXHAUSTED")
        if self.remaining <= 0:
            self.exhausted = True
            return RetryDecision(True, False, "SHARED_ALLOWANCE_EXHAUSTED")
        if evidence.guidance_invalid:
            return RetryDecision(True, False, "INVALID_RETRY_AFTER")
        if evidence.retry_after_ms is not None:
            if p.retry_after_policy == "deny":
                return RetryDecision(True, False, "RETRY_AFTER_POLICY_DENIED")
            if evidence.retry_after_ms > p.maximum_retry_delay_ms:
                return RetryDecision(True, False, "RETRY_AFTER_EXCEEDS_DELAY_LIMIT", evidence.retry_after_ms)
        delay = max(p.backoff.delay_ms(failed_attempt, p.maximum_retry_delay_ms), evidence.retry_after_ms or 0)
        return RetryDecision(True, True, delay_ms=delay)


_state = ContextVar("agentguard_request_retries", default=None)


def current_retry_state():
    return _state.get()


def request_retries(function):
    @wraps(function)
    def run(*args, **kwargs):
        policy = kwargs.get("retry_policy") or ModelRetryPolicy.disabled()
        state = RequestRetryState(policy, kwargs.get("retry_sleeper") or time.sleep,
                                  kwargs.get("retry_wall_clock") or time.time)
        token = _state.set(state)
        try:
            return function(*args, **kwargs)
        finally:
            telemetry.retry_summary(state)
            _state.reset(token)
    return run
