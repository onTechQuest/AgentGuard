"""Validate YAML release requirements and evaluate captured quality metrics."""

from dataclasses import dataclass
import math
from pathlib import Path

import yaml

from src.agentguard.scorecard import AgentGuardScorecard


REQUIREMENTS = {
    "functional_accuracy": "minimum",
    "tool_accuracy": "minimum",
    "argument_accuracy": "minimum",
    "p95_latency_ms": "maximum",
    "average_tokens_per_run": "maximum",
    "failed_scenarios": "maximum",
    "answer_relevancy": "minimum",
    "correctness": "minimum",
    "hallucination_score": "minimum",
    "semantic_pass_rate": "minimum",
    "safety_pass_rate": "minimum",
    "prompt_injection_failures": "maximum",
    "unsupported_action_failures": "maximum",
    "data_protection_failures": "maximum",
    "tool_policy_failures": "maximum",
}

SEMANTIC_METRICS = {
    "answer_relevancy": "average_answer_relevancy",
    "correctness": "average_correctness",
    "hallucination_score": "average_hallucination_score",
    "semantic_pass_rate": "semantic_pass_rate",
}

SAFETY_METRICS = {
    "safety_pass_rate", "prompt_injection_failures", "unsupported_action_failures",
    "data_protection_failures", "tool_policy_failures",
}


@dataclass
class QualityGateResult:
    passed: bool
    checks: list[dict]
    failures: list[str]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_config(config: object) -> None:
    if not isinstance(config, dict):
        raise ValueError("Quality gate configuration must be a mapping.")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("Quality gate configuration requires version: 1.")
    if set(config) - {"version", "quality_gates"}:
        raise ValueError("Unknown quality gate configuration fields.")
    gates = config.get("quality_gates")
    if not isinstance(gates, dict):
        raise ValueError("quality_gates must be a mapping containing all required metrics.")
    unknown = set(gates) - REQUIREMENTS.keys()
    if unknown:
        raise ValueError(f"Unknown quality gate metrics: {sorted(unknown, key=str)!r}")
    for metric, comparison in REQUIREMENTS.items():
        # Additional gates are opt-in; every configured gate blocks on failure.
        if (metric in SEMANTIC_METRICS or metric in SAFETY_METRICS) and metric not in gates:
            continue
        rule = gates.get(metric)
        if not isinstance(rule, dict) or set(rule) != {comparison}:
            raise ValueError(f"{metric} requires exactly one {comparison!r} threshold.")
        threshold = rule[comparison]
        if not _is_number(threshold) or threshold < 0:
            raise ValueError(f"{metric}.{comparison} must be a finite, non-negative number.")
        if (metric.endswith("accuracy") or metric in SEMANTIC_METRICS or metric == "safety_pass_rate") and threshold > 1:
            raise ValueError(f"{metric}.{comparison} must be between 0 and 1.")
        if (metric == "failed_scenarios" or metric.endswith("_failures")) and threshold != int(threshold):
            raise ValueError(f"{metric}.maximum must be a whole number.")


def load_quality_gate_config(path: str | Path) -> dict:
    """Load and validate the versioned YAML configuration."""
    try:
        with Path(path).open(encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load quality gate configuration {str(path)!r}: {error}") from error
    _validate_config(config)
    return config


def evaluate_quality_gate(scorecard: AgentGuardScorecard, config: dict, *, latency_mode: str = "enforce") -> QualityGateResult:
    """Evaluate requirements; explicit report-only latency does not qualify it.

    Legacy callers retain enforcement by default. Correctness CLI modes defer
    latency qualification to repeated sampling; all other gates remain enforced.
    A report-only check has passed=None and enforced=False, never a false PASS.
    """
    _validate_config(config)
    if latency_mode not in {"enforce", "report_only"}:
        raise ValueError("Unsupported latency gate mode")
    checks = []
    failures = []
    for metric, requirement in REQUIREMENTS.items():
        if metric not in config["quality_gates"]:
            continue
        actual = getattr(scorecard, SEMANTIC_METRICS.get(metric, metric))
        threshold = config["quality_gates"][metric][requirement]
        comparison = ">=" if requirement == "minimum" else "<="
        if metric == "p95_latency_ms" and latency_mode == "report_only":
            checks.append({"metric": metric, "actual": actual, "threshold": threshold,
                           "comparison": comparison, "passed": None, "enforced": False,
                           "reason": "Latency qualification requires the repeated performance suite."})
            continue
        valid = _is_number(actual)
        passed = valid and (actual >= threshold if requirement == "minimum" else actual <= threshold)
        checks.append({
            "metric": metric, "actual": actual, "threshold": threshold,
            "comparison": comparison, "passed": passed,
        })
        if not passed:
            if actual is None:
                detail = " (metric unavailable)"
            else:
                detail = " (invalid metric)" if not valid else ""
            failures.append(f"{metric}: actual {actual!r}, required {comparison} {threshold!r}{detail}")
    return QualityGateResult(passed=not failures, checks=checks, failures=failures)
