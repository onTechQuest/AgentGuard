"""Compatibility adapter for existing offline/qualification consumers.

Production imports runtime_reliability directly; it never imports this module.
"""

from functools import wraps

from src.agent.runtime_reliability import StageBudgetPolicy, runtime_execution

QualificationBudgetPolicy = StageBudgetPolicy


def qualification_execution(function):
    runtime = runtime_execution(function)

    @wraps(function)
    def run(*args, **kwargs):
        policy = kwargs.pop("qualification_budget_policy")
        kwargs["runtime_reliability_policy"] = policy.runtime_policy()
        return runtime(*args, **kwargs)
    return run
