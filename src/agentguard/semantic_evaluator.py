"""Evaluate captured answers for relevancy, correctness, and hallucination."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING

from deepeval.metrics import AnswerRelevancyMetric, GEval, HallucinationMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams
from src.agentguard.evaluation_usage import EvaluationUsage, append_usage

if TYPE_CHECKING:
    from src.agentguard.evaluation_record import EvaluationRecord


@dataclass
class SemanticScore:
    """Hallucination score/pass are None when no tool outputs were captured."""

    scenario_id: str
    answer_relevancy_score: float
    answer_relevancy_pass: bool
    answer_relevancy_reason: str | None
    correctness_score: float
    correctness_pass: bool
    correctness_reason: str | None
    hallucination_score: float | None
    hallucination_pass: bool | None
    hallucination_reason: str | None
    evaluation_usage: list[EvaluationUsage] = field(default_factory=list, compare=False)


def evaluate_semantics(record: EvaluationRecord, expected_output: str) -> SemanticScore:
    """Judge the stored answer; only the DeepEval judge makes model calls."""
    # Build once from captured outputs, preserving tool associations and values.
    # An empty list supplies no grounding facts and leaves hallucination skipped.
    context = [
        json.dumps(output, ensure_ascii=False, indent=2, default=str)
        for output in record.tool_outputs
    ]
    test_case = LLMTestCase(
        input=record.input,
        actual_output=record.final_output,
        expected_output=expected_output,
        context=context,
    )
    relevancy = AnswerRelevancyMetric(threshold=0.80)
    correctness = GEval(
        name="Correctness",
        criteria=(
            "Determine whether the response accurately communicates the essential expected outcome. "
            "Differences in wording and formatting must not be penalized. "
            "Additional facts are allowed when they are supported by the provided context "
            "and relevant to the user's request. Penalize factual contradictions, unsupported "
            "invented information, or omission of information necessary to answer the user's request."
        ),
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
            SingleTurnParams.CONTEXT,
        ],
        threshold=0.85,
    )
    relevancy.measure(test_case)
    correctness.measure(test_case)
    evaluation_usage = []
    append_usage(evaluation_usage, "answer_relevancy", relevancy)
    append_usage(evaluation_usage, "correctness", correctness)

    hallucination_score = None
    hallucination_pass = None
    hallucination_reason = "Skipped: no captured tool outputs available as context."
    if record.tool_outputs:
        hallucination_case = LLMTestCase(
            input=record.input,
            actual_output=record.final_output,
            context=context,
        )
        # DeepEval 4.2.5 scores aligned contexts: higher is better (1 is perfect).
        # Require consistency with every captured output and defer pass to the SDK.
        hallucination = HallucinationMetric(threshold=1.0)
        hallucination.measure(hallucination_case)
        append_usage(evaluation_usage, "hallucination", hallucination)
        hallucination_score = hallucination.score
        hallucination_pass = hallucination.is_successful()
        hallucination_reason = hallucination.reason

    return SemanticScore(
        evaluation_usage=evaluation_usage,
        scenario_id=record.scenario_id,
        answer_relevancy_score=relevancy.score,
        answer_relevancy_pass=relevancy.is_successful(),
        answer_relevancy_reason=relevancy.reason,
        correctness_score=correctness.score,
        correctness_pass=correctness.is_successful(),
        correctness_reason=correctness.reason,
        hallucination_score=hallucination_score,
        hallucination_pass=hallucination_pass,
        hallucination_reason=hallucination_reason,
    )
