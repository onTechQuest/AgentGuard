"""Release-level metrics calculated from captured records and scores."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agentguard.evaluation_record import EvaluationRecord
    from src.agentguard.scoring import ScenarioScore
    from src.agentguard.semantic_evaluator import SemanticScore


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
    average_answer_relevancy: float | None = None
    average_correctness: float | None = None
    average_hallucination_score: float | None = None
    semantic_pass_rate: float | None = None


def _available_average(values: Iterable[float | None]) -> float | None:
    available = [
        value for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
    ]
    return fmean(available) if available else None


def build_scorecard(
    records_and_scores: Iterable[tuple[EvaluationRecord, ScenarioScore]],
    semantic_scores: Iterable[SemanticScore | None] | None = None,
) -> AgentGuardScorecard:
    """Aggregate record/score pairs without executing agents.

    Empty input yields zero counts, accuracies, and latencies. Token averages
    exclude None values and are None when no usage is available. P95 selects
    the sorted latency at the one-based rank ceil(0.95 * count).

    Optional semantic scores describe the same evaluation batch; missing entries
    can be omitted or None. Each semantic average includes only finite numeric
    values (excluding booleans). The semantic pass rate is passing checks divided
    by available boolean checks, independent of numeric score availability.
    Skipped checks are excluded; no available values/checks yields None.
    """
    pairs = list(records_and_scores)
    total = len(pairs)
    passed = sum(score.overall_pass for _, score in pairs)
    latencies = sorted(record.latency_ms for record, _ in pairs)
    tokens = [record.total_tokens for record, _ in pairs if record.total_tokens is not None]
    # Integer arithmetic gives the exact nearest rank, including small samples.
    p95_rank = (95 * total + 99) // 100
    semantics = [score for score in semantic_scores if score is not None] if semantic_scores is not None else []
    semantic_checks = [
        check
        for score in semantics
        for check in (score.answer_relevancy_pass, score.correctness_pass, score.hallucination_pass)
        if isinstance(check, bool)
    ]

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
        average_answer_relevancy=_available_average(score.answer_relevancy_score for score in semantics),
        average_correctness=_available_average(score.correctness_score for score in semantics),
        average_hallucination_score=_available_average(score.hallucination_score for score in semantics),
        semantic_pass_rate=fmean(semantic_checks) if semantic_checks else None,
    )
