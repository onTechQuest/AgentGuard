"""Authoritative synchronous admission, independent of best-effort observations.

Only explicitly finite budgets are enforced. No preemption, retries, provider
timeouts, or cancellation API calls occur here. A late result is rejected after
work returns, retaining its actual completion evidence.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from src.agent import telemetry
from src.agent.request_budget import (
    AdmissionDenial, AdmissionEvidence, RequestBudgetRejected, RequestDeadlineExceeded,
)


_active_budget = ContextVar("agentguard_execution_budget", default=None)
_recovery_policy = ContextVar("agentguard_recovery_budget_policy", default=None)


def _reject(component, evidence, *, completed=False):
    error_type = (RequestDeadlineExceeded if evidence.denial_reason in {
        AdmissionDenial.DEADLINE_EXHAUSTED, AdmissionDenial.INSUFFICIENT_ALLOWANCE,
    } else RequestBudgetRejected)
    raise error_type(component, evidence, completed=completed)


def admit(component):
    """Reusable check, including dispatch after potentially expensive preparation."""
    budget = _active_budget.get()
    if budget is None or budget.deadline_monotonic is None:
        return
    requirements = {}
    if component == "recovery_planner" and _recovery_policy.get() is not None:
        requirements = _recovery_policy.get().requirements()
    evidence = budget.admission(**requirements)
    telemetry.stage_admission(evidence)
    if not evidence.admitted:
        _reject(component, evidence)


def retry_admission(component, *, delay_ms, minimum_ms, reserve_ms):
    """Retry delay and useful work must fit without spending downstream reserves."""
    recovery = _recovery_policy.get() if component == "recovery_planner" else None
    if recovery is not None:
        requirements = recovery.requirements()
        minimum_ms = max(minimum_ms, requirements["minimum_ms"])
        reserve_ms = max(reserve_ms, requirements["reserve_ms"])
    budget = _active_budget.get()
    if budget is None:
        return AdmissionEvidence(None, reserve_ms, delay_ms + minimum_ms, None, True)
    return budget.admission(minimum_ms=delay_ms + minimum_ms, reserve_ms=reserve_ms)


@contextmanager
def stage(component, *, operation=None):
    """Check admission before work and acceptance after successful completion.

    The body owns its business outcome. In particular, completed tools are not
    marked failed when the acceptance check subsequently rejects their result.
    Telemetry can fail independently without changing either decision.
    """
    budget = _active_budget.get()
    with telemetry.observe(component):
        if operation is not None:
            telemetry.operation(operation)
        admit(component)
        yield
        if budget is not None and budget.deadline_monotonic is not None:
            evidence = budget.admission()
            telemetry.stage_acceptance(evidence)
            if not evidence.admitted:
                budget.abandon_result()
                _reject(component, evidence, completed=True)


def request_execution(function):
    """Scope explicit budgets independently of optional telemetry construction."""
    @wraps(function)
    def run(*args, **kwargs):
        budget = kwargs.get("request_budget")
        budget_token = _active_budget.set(budget)
        policy_token = _recovery_policy.set(kwargs.get("recovery_budget_policy"))
        try:
            result = function(*args, **kwargs)
            if budget is not None and budget.deadline_monotonic is not None:
                try:
                    # Last acceptance boundary includes local post-synthesis work.
                    with stage("response_acceptance"):
                        pass
                except RequestBudgetRejected as error:
                    budget.abandon_result()
                    error.trace = getattr(result.context_wrapper, "context", None)
                    raise
            return result
        finally:
            _recovery_policy.reset(policy_token)
            _active_budget.reset(budget_token)
    return run
