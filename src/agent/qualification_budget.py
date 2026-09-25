"""Explicit experimental stage budgets; never selected by production defaults.

Caps are hierarchical maxima, not additive promises. Recovery needs its minimum
useful allowance plus a full synthesis reserve at admission. Its result must
return before that reserve is spent. Synthesis may use the smaller of its cap
and the remaining request budget. These synchronous boundaries do not cancel
provider work or add transport timeouts.
"""

from dataclasses import dataclass
from functools import wraps

from src.agent.request_budget import RequestBudget, _milliseconds


@dataclass(frozen=True)
class QualificationBudgetPolicy:
    request_deadline_ms: float
    router_allowance_ms: float
    recovery_reserve_ms: float
    synthesis_allowance_ms: float

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            _milliseconds(getattr(self, name), name)

    def requirements(self, component):
        if component == "primary_router":
            return {"cap_ms": self.router_allowance_ms}
        if component == "recovery_planner":
            return {"minimum_ms": self.recovery_reserve_ms,
                    "reserve_ms": self.synthesis_allowance_ms}
        if component == "synthesis":
            return {"cap_ms": self.synthesis_allowance_ms}
        return {}


def qualification_execution(function):
    """Create the explicit budget before telemetry starts, with no global state."""
    @wraps(function)
    def run(*args, **kwargs):
        policy = kwargs.get("qualification_budget_policy")
        if policy is not None:
            if not isinstance(policy, QualificationBudgetPolicy):
                raise ValueError("Invalid qualification budget policy")
            retry = kwargs.get("retry_policy")
            if retry is not None and retry.enabled:
                raise ValueError("Deadline qualification does not enable retries")
            if kwargs.get("recovery_budget_policy") is not None:
                raise ValueError("Use one explicit qualification recovery policy")
            budget = kwargs.get("request_budget")
            if budget is None:
                budget = RequestBudget(policy.request_deadline_ms)
            elif budget.original_budget_ms is None or budget.original_budget_ms > policy.request_deadline_ms:
                budget = budget.limit(policy.request_deadline_ms)
            kwargs["request_budget"] = budget
        return function(*args, **kwargs)
    return run
