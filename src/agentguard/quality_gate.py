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
        rule = gates.get(metric)
        if not isinstance(rule, dict) or set(rule) != {comparison}:
            raise ValueError(f"{metric} requires exactly one {comparison!r} threshold.")
        threshold = rule[comparison]
        if not _is_number(threshold) or threshold < 0:
            raise ValueError(f"{metric}.{comparison} must be a finite, non-negative number.")
        if metric.endswith("accuracy") and threshold > 1:
            raise ValueError(f"{metric}.{comparison} must be between 0 and 1.")
        if metric == "failed_scenarios" and threshold != int(threshold):
            raise ValueError("failed_scenarios.maximum must be a whole number.")


def load_quality_gate_config(path: str | Path) -> dict:
    """Load and validate the versioned YAML configuration."""
    try:
        with Path(path).open(encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load quality gate configuration {str(path)!r}: {error}") from error
    _validate_config(config)
    return config


def evaluate_quality_gate(scorecard: AgentGuardScorecard, config: dict) -> QualityGateResult:
    """Evaluate every requirement; unavailable metrics fail their checks."""
    _validate_config(config)
    checks = []
    failures = []
    for metric, requirement in REQUIREMENTS.items():
        actual = getattr(scorecard, metric)
        threshold = config["quality_gates"][metric][requirement]
        comparison = ">=" if requirement == "minimum" else "<="
        valid = _is_number(actual)
        passed = valid and (actual >= threshold if requirement == "minimum" else actual <= threshold)
        checks.append({
            "metric": metric, "actual": actual, "threshold": threshold,
            "comparison": comparison, "passed": passed,
        })
        if not passed:
            detail = " (unavailable or invalid metric)" if not valid else ""
            failures.append(f"{metric}: actual {actual!r}, required {comparison} {threshold!r}{detail}")
    return QualityGateResult(passed=not failures, checks=checks, failures=failures)
