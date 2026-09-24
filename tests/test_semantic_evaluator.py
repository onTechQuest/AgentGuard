from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest
from deepeval.metrics import HallucinationMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams

from src.agentguard import evaluation_record, semantic_evaluator


@pytest.fixture(autouse=True)
def hallucination_factory(monkeypatch):
    metric = Mock(score=1.0, reason="The answer agrees with the tool outputs.")
    metric.is_successful.return_value = True
    factory = Mock(return_value=metric)
    monkeypatch.setattr(semantic_evaluator, "HallucinationMetric", factory)
    return factory


@pytest.mark.parametrize("score, passed, reason", [
    (0.95, True, "The answer addresses the question."),
    (0.80, True, None),
    (0.79, False, "The answer includes unrelated details."),
    (0.90, False, "Judge reported failure."),
])
@pytest.mark.parametrize("correctness_score, correctness_pass, correctness_reason", [
    (0.97, True, "The essential facts match."),
    (0.85, True, None),
    (0.84, False, "A necessary fact is missing."),
    (0.92, False, "Judge reported failure."),
])
def test_evaluates_captured_answer(
    monkeypatch, score, passed, reason,
    correctness_score, correctness_pass, correctness_reason,
):
    record = evaluation_record.EvaluationRecord(
        scenario_id="status-1", input="Where is ORD-1001?",
        final_output="ORD-1001 has shipped.",
        tool_calls=[{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}],
        latency_ms=10.0, request_count=1, input_tokens=20,
        output_tokens=10, total_tokens=30,
    )
    before = deepcopy(record)
    run = Mock(side_effect=AssertionError("Must not run the support agent"))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", run)
    metric = Mock(score=score, reason=reason)
    metric.is_successful.return_value = passed
    factory = Mock(return_value=metric)
    monkeypatch.setattr(semantic_evaluator, "AnswerRelevancyMetric", factory)
    correctness = Mock(score=correctness_score, reason=correctness_reason)
    correctness.is_successful.return_value = correctness_pass
    correctness_factory = Mock(return_value=correctness)
    monkeypatch.setattr(semantic_evaluator, "GEval", correctness_factory)
    expected_output = "Order ORD-1001 has shipped via UPS."

    result = semantic_evaluator.evaluate_semantics(record, expected_output)

    factory.assert_called_once_with(threshold=0.80)
    correctness_factory.assert_called_once_with(
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
    metric.measure.assert_called_once()
    test_case = metric.measure.call_args.args[0]
    assert isinstance(test_case, LLMTestCase)
    assert test_case.input == record.input
    assert test_case.actual_output == record.final_output
    assert test_case.expected_output == expected_output
    assert test_case.context == []
    correctness.measure.assert_called_once_with(test_case)
    assert correctness.measure.call_args.args[0] is test_case
    metric.is_successful.assert_called_once_with()
    correctness.is_successful.assert_called_once_with()
    assert result == semantic_evaluator.SemanticScore(
        scenario_id="status-1",
        answer_relevancy_score=score,
        answer_relevancy_pass=passed,
        answer_relevancy_reason=reason,
        correctness_score=correctness_score,
        correctness_pass=correctness_pass,
        correctness_reason=correctness_reason,
        hallucination_score=None,
        hallucination_pass=None,
        hallucination_reason="Skipped: no captured tool outputs available as context.",
    )
    assert record == before
    run.assert_not_called()


@pytest.fixture
def hallucination_sample(monkeypatch):
    record = evaluation_record.EvaluationRecord(
        scenario_id="grounding-1", input="Where is ORD-1001?",
        final_output="ORD-1001 has shipped via UPS.",
        tool_calls=[{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}],
        latency_ms=10.0, request_count=1, input_tokens=20, output_tokens=10, total_tokens=30,
        tool_outputs=[{
            "name": "get_order_status", "call_id": "call-1",
            "output": {"order_id": "ORD-1001", "status": "shipped", "carrier": "UPS"},
        }],
    )
    for name in ("AnswerRelevancyMetric", "GEval"):
        metric = Mock(score=0.95, reason="Relevant and correct.")
        metric.is_successful.return_value = True
        monkeypatch.setattr(semantic_evaluator, name, Mock(return_value=metric))
    run = Mock(side_effect=AssertionError("Must not run the support agent"))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", run)
    yield record
    run.assert_not_called()


@pytest.mark.parametrize("answer, score, passed, reason", [
    ("ORD-1001 has shipped via UPS.", 1.0, True, "All facts agree with the tool result."),
    ("ORD-1001 was delivered by FedEx.", 0.0, False, "Status and carrier contradict the tool result."),
    ("ORD-1001 has shipped via UPS.", 1.0, False, "Judge reported failure."),
    ("ORD-1001 has shipped via UPS.", 1.0, True, None),
])
def test_hallucination_uses_only_captured_outputs(
    hallucination_sample, hallucination_factory, answer, score, passed, reason,
):
    record = hallucination_sample
    record.final_output = answer
    before = deepcopy(record)
    metric = hallucination_factory.return_value
    metric.score = score
    metric.reason = reason
    metric.is_successful.return_value = passed

    result = semantic_evaluator.evaluate_semantics(record, "EXPECTED_ONLY_SENTINEL")

    hallucination_factory.assert_called_once_with(threshold=1.0)
    metric.measure.assert_called_once()
    case = metric.measure.call_args.args[0]
    assert isinstance(case, LLMTestCase)
    assert case.input == record.input
    assert case.actual_output == record.final_output
    assert case.expected_output is None
    assert [json.loads(context) for context in case.context] == record.tool_outputs
    assert "EXPECTED_ONLY_SENTINEL" not in "".join(case.context)
    metric.is_successful.assert_called_once_with()
    assert (result.hallucination_score, result.hallucination_pass, result.hallucination_reason) == (
        score, passed, reason,
    )
    assert record == before


def test_multiple_outputs_preserve_factual_values(hallucination_sample, hallucination_factory):
    record = hallucination_sample
    record.tool_outputs.append({
        "name": "check_return_eligibility", "call_id": "call-2",
        "output": {
            "eligible": False, "days_remaining": 0, "refund": 12.75, "reason": None,
            "items": [{"name": "Café mug", "quantity": 2}],
        },
    })
    semantic_evaluator.evaluate_semantics(record, "Expected answer")

    context = hallucination_factory.return_value.measure.call_args.args[0].context
    assert len(context) == 2
    assert [json.loads(entry) for entry in context] == record.tool_outputs
    assert "Café mug" in context[1]
    correctness_case = semantic_evaluator.GEval.return_value.measure.call_args.args[0]
    assert correctness_case.context == context


def test_no_outputs_skips_hallucination(hallucination_sample, hallucination_factory):
    record = hallucination_sample
    record.tool_outputs = []

    result = semantic_evaluator.evaluate_semantics(record, "UPS shipped ORD-1001.")

    hallucination_factory.assert_not_called()
    assert result.hallucination_score is None
    assert result.hallucination_pass is None
    assert result.hallucination_reason == "Skipped: no captured tool outputs available as context."
    assert result.answer_relevancy_pass
    assert result.correctness_pass
    correctness_case = semantic_evaluator.GEval.return_value.measure.call_args.args[0]
    assert correctness_case.context == []


@pytest.mark.parametrize("output", ["shipped via UPS", '{"status":', "", None, ["UPS", 0, False]])
def test_unstructured_output_is_preserved(hallucination_sample, hallucination_factory, output):
    record = hallucination_sample
    record.tool_outputs[0]["output"] = output

    semantic_evaluator.evaluate_semantics(record, "Expected answer")

    context = hallucination_factory.return_value.measure.call_args.args[0].context
    assert json.loads(context[0])["output"] == output
    assert semantic_evaluator.GEval.return_value.measure.call_args.args[0].context == context


@pytest.mark.parametrize("answer, score, passed, reason", [
    pytest.param(
        "ORD-1001 has shipped via UPS. Tracking number: 1Z999.",
        1.0, True, "The additional tracking number is supported by tool context.",
        id="supported-additional-detail",
    ),
    pytest.param(
        "ORD-1001 has shipped via UPS. You received a $50 refund.",
        0.3, False, "The refund claim has no support in the tool context.",
        id="unsupported-additional-detail",
    ),
    pytest.param(
        "ORD-1001 has been cancelled and will not ship.",
        0.0, False, "Cancellation contradicts the expected shipped outcome.",
        id="contradiction",
    ),
    pytest.param(
        "**ORD-1001**\n- Status: shipped\n- Carrier: UPS",
        1.0, True, "Formatting changes preserve all essential facts.",
        id="formatting-only",
    ),
])
def test_correctness_with_grounding_and_mocked_judge_outcomes(
    hallucination_sample, hallucination_factory, answer, score, passed, reason,
):
    # Judge verdicts are mocked: verify evidence, criteria, and result propagation,
    # not the quality of a live model's judgments.
    record = hallucination_sample
    record.final_output = answer
    record.tool_outputs[0]["output"]["tracking_number"] = "1Z999"
    before = deepcopy(record)
    expected_output = "ORD-1001 has shipped via UPS."
    correctness = semantic_evaluator.GEval.return_value
    correctness.score = score
    correctness.reason = reason
    correctness.is_successful.return_value = passed

    result = semantic_evaluator.evaluate_semantics(record, expected_output)

    correctness.measure.assert_called_once()
    case = correctness.measure.call_args.args[0]
    assert case.input == record.input
    assert case.actual_output == answer
    assert case.expected_output == expected_output
    assert "1Z999" not in case.expected_output
    assert [json.loads(entry) for entry in case.context] == record.tool_outputs
    assert json.loads(case.context[0])["output"]["tracking_number"] == "1Z999"
    assert "refund" not in "".join(case.context)
    configuration = semantic_evaluator.GEval.call_args.kwargs
    assert SingleTurnParams.CONTEXT in configuration["evaluation_params"]
    assert configuration["threshold"] == 0.85
    assert "Additional facts are allowed when they are supported by the provided context" in configuration["criteria"]
    assert "Differences in wording and formatting must not be penalized." in configuration["criteria"]
    assert "Penalize factual contradictions, unsupported invented information" in configuration["criteria"]
    assert (result.correctness_score, result.correctness_pass, result.correctness_reason) == (score, passed, reason)
    hallucination_case = hallucination_factory.return_value.measure.call_args.args[0]
    assert hallucination_case.context == case.context
    assert hallucination_case.expected_output is None
    assert record == before


def test_non_json_business_values_remain_readable(hallucination_sample, hallucination_factory):
    record = hallucination_sample
    record.tool_outputs[0]["output"] = {"amount": Decimal("12.30"), "date": date(2026, 9, 12)}

    semantic_evaluator.evaluate_semantics(record, "Expected answer")

    context = hallucination_factory.return_value.measure.call_args.args[0].context
    assert json.loads(context[0])["output"] == {"amount": "12.30", "date": "2026-09-12"}


@pytest.mark.parametrize("score, passed", [(1.0, True), (0.99, False), (0.0, False)])
def test_installed_hallucination_pass_direction_without_model_calls(score, passed):
    # Bypass model initialization; exercise only the installed public pass API.
    metric = object.__new__(HallucinationMetric)
    metric.threshold = 1.0
    metric.score = score
    metric.error = None

    assert metric.is_successful() is passed
