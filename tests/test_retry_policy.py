"""Pure retry decisions: no network, no wall-clock waiting."""

from dataclasses import replace

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, RateLimitError

from src.agent.retry_policy import (
    Backoff, DeliveryCertainty as Delivery, FailureEvidence, ModelRetryPolicy,
    RequestRetryState, classify_failure,
)
from src.agent.telemetry import FailureCategory as Category


def policy(**overrides):
    return replace(ModelRetryPolicy(
        enabled=True, max_attempts=2, shared_extra_attempts_per_request=1,
        backoff=Backoff(10), maximum_retry_delay_ms=100,
        minimum_attempt_budget_ms=20, downstream_reserve_ms=10,
    ), **overrides)


def test_default_policy_has_no_extra_attempts():
    p = ModelRetryPolicy.disabled()
    assert not p.enabled and p.max_attempts == 1 and p.shared_extra_attempts_per_request == 0
    decision = RequestRetryState(p).decide(FailureEvidence(Category.NETWORK_ERROR, Delivery.NOT_SENT), 1)
    assert not decision.admitted and decision.denial_reason == "POLICY_DISABLED"


@pytest.mark.parametrize("change", [
    {"max_attempts": 0}, {"max_attempts": True}, {"shared_extra_attempts_per_request": -1},
    {"maximum_retry_delay_ms": -1}, {"minimum_attempt_budget_ms": 0},
    {"minimum_attempt_budget_ms": None}, {"backoff": None}, {"downstream_reserve_ms": float("nan")},
    {"transient_http_statuses": {429}}, {"retry_after_policy": "truncate"}, {"enabled": 1},
    {"eligible_failure_categories": {"typo"}},
])
def test_invalid_policy_rejected(change):
    with pytest.raises(ValueError):
        policy(**change)


@pytest.mark.parametrize("category", [
    Category.AUTHENTICATION_FAILURE, Category.AUTHORIZATION_FAILURE, Category.CONFIGURATION_ERROR,
    Category.INVALID_MODEL_OUTPUT, Category.MODEL_PROTOCOL_ERROR, Category.DEADLINE_EXHAUSTED,
    Category.CANCELLED, Category.PROHIBITED_OPERATION_ATTEMPT, Category.UNKNOWN_EXTERNAL_FAILURE,
])
def test_ineligible_categories_cannot_be_enabled_by_allowlist(category):
    state = RequestRetryState(policy(eligible_failure_categories=frozenset({category})))
    assert not state.decide(FailureEvidence(category, Delivery.NOT_SENT), 1).admitted


@pytest.mark.parametrize("cause,delivery", [
    (httpx2.ConnectError, Delivery.NOT_SENT), (httpx2.ConnectTimeout, Delivery.NOT_SENT),
    (httpx2.ReadError, Delivery.SENT_OUTCOME_UNKNOWN), (httpx2.WriteError, Delivery.SENT_OUTCOME_UNKNOWN),
    (httpx2.ReadTimeout, Delivery.SENT_OUTCOME_UNKNOWN), (httpx2.WriteTimeout, Delivery.SENT_OUTCOME_UNKNOWN),
    (httpx2.RemoteProtocolError, Delivery.SENT_OUTCOME_UNKNOWN), (None, Delivery.SENT_OUTCOME_UNKNOWN),
])
def test_typed_delivery_evidence(cause, delivery):
    request = httpx2.Request("POST", "https://offline.invalid")
    error = APIConnectionError(request=request)
    if cause:
        error.__cause__ = cause("private", request=request)
    evidence = classify_failure(error, "primary_router")
    assert evidence.delivery == delivery
    assert RequestRetryState(policy()).decide(evidence, 1).admitted == (delivery == Delivery.NOT_SENT)


def test_read_failure_cannot_inherit_not_sent_from_prior_context():
    request = httpx2.Request("POST", "https://offline.invalid")
    prior = httpx2.ConnectError("private", request=request)
    latest = httpx2.ReadError("private", request=request)
    latest.__context__ = prior
    error = APIConnectionError(request=request)
    error.__cause__ = latest
    evidence = classify_failure(error, "synthesis")
    assert evidence.delivery == Delivery.SENT_OUTCOME_UNKNOWN
    assert not RequestRetryState(policy()).decide(evidence, 1).admitted


@pytest.mark.parametrize("headers,delay,invalid", [
    ({"retry-after": "0.025"}, 25, False), ({"retry-after-ms": "40"}, 40, False),
    ({"retry-after": "Thu, 01 Jan 1970 00:00:02 GMT"}, 1000, False),
    ({"retry-after": "invalid"}, None, True), ({"retry-after": "-1"}, None, True),
    ({"retry-after-ms": "nan"}, None, True), ({}, None, False),
])
def test_retry_after_parsing(headers, delay, invalid):
    response = httpx2.Response(429, headers=headers, request=httpx2.Request("POST", "https://offline.invalid"))
    evidence = classify_failure(RateLimitError("private", response=response, body=None), "synthesis", wall_clock=lambda: 1)
    assert evidence.delivery == Delivery.KNOWN_FAILED and evidence.status_code == 429
    assert evidence.retry_after_ms == delay and evidence.guidance_invalid == invalid


def test_backoff_jitter_is_injected_and_bounded():
    seen = []
    def jitter(delay, attempt):
        seen.append((delay, attempt))
        return delay + 5
    backoff = Backoff(10, multiplier=2, jitter=jitter)
    assert [backoff.delay_ms(i, 30) for i in (1, 2, 3)] == [15, 25, 30]
    assert seen == [(10, 1), (20, 2), (30, 3)]
    with pytest.raises(ValueError):
        Backoff(10, jitter=lambda *_: -1).delay_ms(1, 30)


@pytest.mark.parametrize("guidance,changes,reason", [
    (200, {}, "RETRY_AFTER_EXCEEDS_DELAY_LIMIT"),
    (20, {"retry_after_policy": "deny"}, "RETRY_AFTER_POLICY_DENIED"),
])
def test_guidance_never_shortened(guidance, changes, reason):
    evidence = FailureEvidence(Category.RATE_LIMIT, Delivery.KNOWN_FAILED, 429, guidance)
    decision = RequestRetryState(policy(**changes)).decide(evidence, 1)
    assert not decision.admitted and decision.denial_reason == reason


def test_only_explicit_transient_statuses_and_categories_are_eligible():
    for status, admitted in ((500, True), (503, True), (501, False), (400, False)):
        response = httpx2.Response(status, request=httpx2.Request("POST", "https://offline.invalid"))
        evidence = classify_failure(APIStatusError("private", response=response, body=None), "synthesis")
        assert RequestRetryState(policy()).decide(evidence, 1).admitted == admitted
    assert not RequestRetryState(policy(eligible_failure_categories=frozenset())).decide(evidence, 1).admitted
