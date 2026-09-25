"""Production-only measurement populations and repeated-run qualification."""

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from statistics import fmean, median


def distribution(values):
    values = list(values)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and isfinite(value) and value >= 0 for value in values):
        raise ValueError("Measurements must be finite nonnegative numbers")
    values.sort()
    if not values:
        return {"count": 0}
    result = {"count": len(values), "mean": fmean(values), "median": median(values),
              "min": values[0], "max": values[-1]}
    for percentile in (50, 90, 95, 99):
        result[f"p{percentile}"] = values[(percentile * len(values) + 99) // 100 - 1]
    return result


@dataclass(frozen=True)
class LatencyObservation:
    scenario_id: str
    latency_ms: float


@dataclass(frozen=True)
class ProductionUsage:
    execution_count: int
    average_latency_ms: float | None
    maximum_latency_ms: float | None
    p95_latency_ms: float | None
    token_observation_count: int
    average_tokens_per_execution: float | None
    latency_observations: tuple[LatencyObservation, ...]

    def exceeding(self, threshold: float) -> tuple[LatencyObservation, ...]:
        return tuple(row for row in self.latency_observations if row.latency_ms > threshold)


def production_usage(records) -> ProductionUsage:
    records = list(records)
    latencies = distribution([record.latency_ms for record in records])
    tokens = distribution([record.total_tokens for record in records if record.total_tokens is not None])
    return ProductionUsage(
        len(records), latencies.get("mean"), latencies.get("max"), latencies.get("p95"),
        tokens["count"], tokens.get("mean"),
        tuple(LatencyObservation(record.scenario_id, record.latency_ms) for record in records),
    )


@dataclass(frozen=True)
class PerformanceQualification:
    passed: bool
    observation_count: int
    p95_latency_ms: float | None
    threshold_ms: float
    failures: list[str]


def qualify_performance(observations, scenario_ids, repetitions, threshold_ms):
    """Fail closed unless the complete, balanced benchmark meets the YAML limit."""
    rows, ids = list(observations), list(scenario_ids)
    failures = []
    if (type(repetitions) is not int or repetitions < 5 or not ids
            or len(set(ids)) != len(ids) or len(ids) * repetitions < 40):
        failures.append("Qualification requires at least 40 observations and five repetitions per scenario.")
    else:
        expected = Counter((scenario_id, repetition) for scenario_id in ids for repetition in range(1, repetitions + 1))
        actual = Counter((row.get("scenario_id"), row.get("repetition")) for row in rows)
        if actual != expected:
            failures.append("Incomplete or duplicate performance observations; every planned execution is required.")
    if any(row.get("status") != "completed" for row in rows):
        failures.append("Performance execution failed or is incomplete; no failed observation can be omitted.")
    p95 = None
    try:
        p95 = distribution([row.get("latency_ms") for row in rows]).get("p95")
    except ValueError:
        failures.append("Latency measurement is unavailable or invalid.")
    if not isinstance(threshold_ms, (float, int)) or isinstance(threshold_ms, bool) or not isfinite(threshold_ms) or threshold_ms < 0:
        raise ValueError("Latency threshold must be a finite nonnegative number")
    if p95 is None or p95 > threshold_ms:
        failures.append(f"p95_latency_ms: actual {p95!r}, required <= {threshold_ms!r}")
    return PerformanceQualification(not failures, len(rows), p95, threshold_ms, failures)
