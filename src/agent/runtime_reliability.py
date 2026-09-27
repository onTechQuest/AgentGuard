"""Production-owned policy; hierarchical maxima are not additive reservations."""

from dataclasses import asdict, dataclass, field
from functools import lru_cache, wraps
import json
from pathlib import Path
import re

from src.agent.request_budget import RequestBudget, _milliseconds
from src.agent.retry_policy import ModelRetryPolicy


BUDGET_FIELDS = ("request_deadline_ms", "router_allowance_ms", "recovery_reserve_ms", "synthesis_allowance_ms")
POLICY_PATH = Path(__file__).resolve().parents[2] / "config/runtime-reliability.json"


@dataclass(frozen=True)
class StageBudgetPolicy:
    request_deadline_ms: float
    router_allowance_ms: float
    recovery_reserve_ms: float
    synthesis_allowance_ms: float

    def __post_init__(self):
        for name in BUDGET_FIELDS:
            _milliseconds(getattr(self, name), name)

    def requirements(self, component):
        if self.request_deadline_ms is None:
            return {}
        if component == "primary_router":
            return {"cap_ms": self.router_allowance_ms}
        if component == "recovery_planner":
            return {"minimum_ms": self.recovery_reserve_ms,
                    "reserve_ms": self.synthesis_allowance_ms}
        if component == "synthesis":
            return {"cap_ms": self.synthesis_allowance_ms}
        return {}

    def runtime_policy(self):
        return RuntimeReliabilityPolicy(**{name: getattr(self, name) for name in BUDGET_FIELDS})


@dataclass(frozen=True)
class RuntimeReliabilityPolicy(StageBudgetPolicy):
    request_deadline_ms: float | None = None
    router_allowance_ms: float | None = None
    recovery_reserve_ms: float | None = None
    synthesis_allowance_ms: float | None = None
    model_retry_policy: ModelRetryPolicy = field(default_factory=ModelRetryPolicy.disabled)
    name: str = "explicit_custom"

    def __post_init__(self):
        if any(getattr(self, name) is not None for name in BUDGET_FIELDS):
            super().__post_init__()  # Reject partial/unintentionally unbounded policies.
        if not isinstance(self.model_retry_policy, ModelRetryPolicy):
            raise ValueError("Explicit ModelRetryPolicy required")
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", self.name):
            raise ValueError("Policy name must be a short identifier")

    @classmethod
    def unbounded(cls, *, model_retry_policy=None):
        """Intentional diagnostic/controlled-use override, never the default."""
        return cls(name="explicit_unbounded", model_retry_policy=model_retry_policy or ModelRetryPolicy.disabled())

    def snapshot(self):
        result = asdict(self)
        retry = result["model_retry_policy"]
        retry["eligible_failure_categories"] = sorted(retry["eligible_failure_categories"])
        retry["transient_http_statuses"] = sorted(retry["transient_http_statuses"])
        if retry["backoff"] is not None:
            retry["backoff"]["jitter"] = "configured" if self.model_retry_policy.backoff.jitter else None
        # Configuration is not transport observation. Arbitrary injected runners
        # retain their per-attempt unknown state rather than claiming suppression.
        result["lower_layer_retry_policy"] = {
            "agents_sdk_max_retries": 0, "openai_client_max_retries": 0,
            "scope": "default_production_sdk_path",
        }
        return result


@lru_cache(maxsize=1)
def default_runtime_policy():
    """Load checked-in v1 once per process. Invalid configuration fails closed."""
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy = RuntimeReliabilityPolicy(**{**raw, "model_retry_policy": ModelRetryPolicy(**raw["model_retry_policy"])})
    retry = policy.model_retry_policy
    if policy.request_deadline_ms is None or retry.enabled or retry.max_attempts != 1 or retry.shared_extra_attempts_per_request != 0:
        raise ValueError("Production v1 requires finite budgets and a single attempt with retries disabled")
    return policy


def runtime_execution(function):
    """Resolve policy before observation without resetting the request clock."""
    @wraps(function)
    def run(*args, **kwargs):
        policy = kwargs.get("runtime_reliability_policy")
        if policy is None:
            policy = default_runtime_policy()
        if not isinstance(policy, RuntimeReliabilityPolicy):
            raise ValueError("Invalid runtime reliability policy")
        retry = kwargs.get("retry_policy")
        if retry is not None and retry != policy.model_retry_policy:
            raise ValueError("Retry override conflicts with runtime reliability policy; supply an explicit controlled policy")
        if policy.request_deadline_ms is not None and kwargs.get("recovery_budget_policy") is not None:
            raise ValueError("Runtime policy owns recovery admission; conflicting recovery override")
        budget = kwargs.get("request_budget")
        if budget is None:
            budget = RequestBudget(policy.request_deadline_ms)
        elif policy.request_deadline_ms is not None:
            budget = budget.limit(policy.request_deadline_ms)
        kwargs.update(request_budget=budget, runtime_reliability_policy=policy,
                      retry_policy=policy.model_retry_policy)
        return function(*args, **kwargs)
    return run
