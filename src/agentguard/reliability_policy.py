"""Qualification-only configuration and pure shadow retry decisions."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re

from src.agent.request_budget import RequestBudget, RecoveryBudgetPolicy
from src.agent.retry_policy import (
    Backoff, DeliveryCertainty, FailureEvidence, ModelRetryPolicy,
    RequestRetryState, milliseconds,
)
from src.agent.telemetry import FailureCategory


def label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,95}", value):
        raise ValueError("Qualification labels must be short identifiers")
    return value


def _keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Unsupported qualification configuration fields")


@dataclass(frozen=True)
class PolicyCandidate:
    name: str
    request_budget_ms: float | None = None
    retry_policy: ModelRetryPolicy = field(default_factory=ModelRetryPolicy.disabled)
    recovery_budget_policy: RecoveryBudgetPolicy = field(default_factory=RecoveryBudgetPolicy)

    def __post_init__(self):
        label(self.name)
        if self.request_budget_ms is not None:
            milliseconds(self.request_budget_ms, "request_budget_ms")
        if self.retry_policy.backoff is not None and self.retry_policy.backoff.jitter is not None:
            raise ValueError("Qualification candidates require reproducible backoff without callable jitter")

    def snapshot(self):
        result = asdict(self)
        p = result["retry_policy"]
        p["eligible_failure_categories"] = sorted(p["eligible_failure_categories"])
        p["transient_http_statuses"] = sorted(p["transient_http_statuses"])
        if p["backoff"] is not None:
            p["backoff"].pop("jitter")
        return result


@dataclass(frozen=True)
class QualificationConfig:
    dataset_suite: str
    repetitions: int
    candidates: tuple[PolicyCandidate, ...]
    execution_candidate: str
    recovery_scenario_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if self.dataset_suite not in {"smoke", "full"}:
            raise ValueError("Qualification dataset_suite must be smoke or full")
        if type(self.repetitions) is not int or self.repetitions < 1:
            raise ValueError("Qualification repetitions must be positive")
        names = [c.name for c in self.candidates]
        if not names or len(names) != len(set(names)) or self.execution_candidate not in names:
            raise ValueError("Unique candidates and a registered execution_candidate are required")
        for scenario_id in self.recovery_scenario_ids:
            label(scenario_id)
        if len(set(self.recovery_scenario_ids)) != len(self.recovery_scenario_ids):
            raise ValueError("Duplicate recovery scenario selection")

    @property
    def execution(self):
        return next(c for c in self.candidates if c.name == self.execution_candidate)

    def snapshot(self):
        return {"dataset_suite": self.dataset_suite, "repetitions": self.repetitions,
                "execution_candidate": self.execution_candidate,
                "recovery_scenario_ids": list(self.recovery_scenario_ids),
                "candidates": [c.snapshot() for c in self.candidates]}


def load_qualification_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    _keys(data, {"dataset_suite", "repetitions", "candidates", "execution_candidate", "recovery_scenario_ids"})
    candidates = []
    for raw in data["candidates"]:
        _keys(raw, {"name", "request_budget_ms", "retry_policy", "recovery_budget_policy"})
        retry = dict(raw.get("retry_policy", {}))
        _keys(retry, ModelRetryPolicy.__dataclass_fields__)
        if retry.get("backoff") is not None:
            _keys(retry["backoff"], {"initial_delay_ms", "multiplier"})
            retry["backoff"] = Backoff(**retry["backoff"])
        recovery = raw.get("recovery_budget_policy", {})
        _keys(recovery, RecoveryBudgetPolicy.__dataclass_fields__)
        candidates.append(PolicyCandidate(raw["name"], raw.get("request_budget_ms"),
                                          ModelRetryPolicy(**retry), RecoveryBudgetPolicy(**recovery)))
    return QualificationConfig(data["dataset_suite"], data["repetitions"], tuple(candidates),
                               data["execution_candidate"], tuple(data.get("recovery_scenario_ids", [])))


def shadow_retry(candidate, evidence, *, component, remaining_budget_ms,
                 failed_attempt=1, shared_allowance_used=0, cancelled=False):
    """One counterfactual at an observed boundary. No sleeping or dispatching.

    Prior allowance use is actual evidence, not assumed hypothetical successes.
    remaining_budget_ms=None explicitly means unbounded, not unknown.
    """
    p = candidate.retry_policy
    if type(shared_allowance_used) is not int or shared_allowance_used < 0:
        raise ValueError("Invalid observed shared allowance")
    if type(failed_attempt) is not int or failed_attempt < 1:
        raise ValueError("Invalid observed attempt number")
    state = RequestRetryState(p, consumed=shared_allowance_used)
    decision = state.decide(evidence, failed_attempt)
    reserve, useful = p.downstream_reserve_ms, p.minimum_attempt_budget_ms or 0
    if component == "recovery_planner":
        requirements = candidate.recovery_budget_policy.requirements()
        reserve = max(reserve, requirements["reserve_ms"])
        useful = max(useful, requirements["minimum_ms"])
    budget = RequestBudget(remaining_budget_ms, clock=lambda: 0)
    if cancelled:
        budget.request_cancellation()
    admitted, reason = decision.admitted, decision.denial_reason
    if admitted:
        admission = budget.admission(minimum_ms=decision.delay_ms + useful, reserve_ms=reserve)
        admitted = admission.admitted
        if not admitted:
            reason = ("CANCELLATION_REQUESTED" if cancelled else "RETRY_AFTER_EXCEEDS_BUDGET"
                      if evidence.retry_after_ms is not None else "INSUFFICIENT_RETRY_BUDGET")
    after_delay = (None if remaining_budget_ms is None else
                   max(0, remaining_budget_ms - (decision.delay_ms or 0)) if admitted else remaining_budget_ms)
    return {
        "candidate": candidate.name, "shadow_retry_eligible": decision.eligible,
        "shadow_retry_admitted": admitted, "shadow_retry_denial_reason": reason,
        "shadow_retry_delay_ms": decision.delay_ms, "shadow_remaining_budget_ms": after_delay,
        "available_budget_ms": remaining_budget_ms, "downstream_reserve_ms": reserve,
        "downstream_reserve_remaining_ms": (reserve if after_delay is None else min(reserve, after_delay)),
        "minimum_attempt_budget_ms": useful,
        "shared_allowance_remaining": max(0, state.remaining),
        "failure_category": evidence.category.value, "delivery_certainty": evidence.delivery.value,
        "retry_after_ms": evidence.retry_after_ms,
        "shadow_dispatch_count": 0, "retry_success_estimate": None,
    }


def evidence_from_attempt(attempt):
    return FailureEvidence(
        FailureCategory(attempt["failure_category"]),
        DeliveryCertainty(attempt.get("delivery_certainty") or "SENT_OUTCOME_UNKNOWN"),
        attempt.get("provider_status_code"), attempt.get("retry_after_ms"),
        attempt.get("retry_after_invalid", False),
    )
