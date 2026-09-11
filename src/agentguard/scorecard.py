"""Release-level metrics calculated from captured records and scores."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agentguard.evaluation_record import EvaluationRecord
    from src.agentguard.scoring import ScenarioScore


@dataclass
class AgentGuardScorecard:
    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    functional_accuracy: float
    tool_accuracy: float
    argument_accuracy: float
    average_latency_ms: float
    p95_latency_ms: float
    average_tokens_per_run: float | None


def build_scorecard(
    records_and_scores: Iterable[tuple[EvaluationRecord, ScenarioScore]],
) -> AgentGuardScorecard:
    """Aggregate record/score pairs without executing agents.

    Empty input yields zero counts, accuracies, and latencies. Token averages
    exclude None values and are None when no usage is available. P95 selects
    the sorted latency at the one-based rank ceil(0.95 * count).
    """
    pairs = list(records_and_scores)
    total = len(pairs)
    passed = sum(score.overall_pass for _, score in pairs)
    latencies = sorted(record.latency_ms for record, _ in pairs)
    tokens = [record.total_tokens for record, _ in pairs if record.total_tokens is not None]
    # Integer arithmetic gives the exact nearest rank, including small samples.
    p95_rank = (95 * total + 99) // 100

    return AgentGuardScorecard(
        total_scenarios=total,
        passed_scenarios=passed,
        failed_scenarios=total - passed,
        functional_accuracy=sum(score.functional_pass for _, score in pairs) / total if total else 0.0,
        tool_accuracy=sum(score.tool_pass for _, score in pairs) / total if total else 0.0,
        argument_accuracy=sum(score.argument_pass for _, score in pairs) / total if total else 0.0,
        average_latency_ms=fmean(latencies) if latencies else 0.0,
        p95_latency_ms=latencies[p95_rank - 1] if latencies else 0.0,
        average_tokens_per_run=fmean(tokens) if tokens else None,
    )
