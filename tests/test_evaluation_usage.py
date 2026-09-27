"""Judge usage is observable separately and cannot affect scoring."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from agents.usage import Usage

from src.agentguard import semantic_evaluator as semantic
from src.agentguard import safety_evaluator as safety
from src.agentguard.action_claims import SemanticActionClaimClassifier, ActionClaimBatch, evaluate_unsupported_actions
from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.evaluation_usage import capture
from src.agentguard.tool_policy import action_policy_snapshot


def record():
    return EvaluationRecord("offline", "a question", "a refusal", [], 1, 2, 20, 10, 30)


def metric():
    return Mock(score=1.0, reason="ok", input_tokens=5, output_tokens=3,
                is_successful=Mock(return_value=True))


def test_semantic_usage_does_not_enter_production_tokens(monkeypatch):
    original = record()
    before = deepcopy(original)
    for name in ("AnswerRelevancyMetric", "GEval", "HallucinationMetric"):
        monkeypatch.setattr(semantic, name, Mock(return_value=metric()))
    score = semantic.evaluate_semantics(original, "an answer")
    assert original == before and original.total_tokens == 30
    assert [row.component for row in score.evaluation_usage] == ["answer_relevancy", "correctness"]
    assert sum(row.total_tokens for row in score.evaluation_usage) == 16
    assert all(row.sdk_visible_requests is None and row.http_retry_count is None for row in score.evaluation_usage)


def test_injection_usage_does_not_enter_production_tokens():
    original = record()
    classifier = Mock(reason="resisted", input_tokens=9, output_tokens=2)
    classifier.classify.return_value = "resisted"
    score = safety.safety_evaluate_record(
        {"id": "offline", "input": "a question", "expected_injection_label": "resisted"}, original,
        classifier_factory=lambda: classifier)
    assert score.passed
    assert score.evaluation_usage[0].component == "prompt_injection"
    assert score.evaluation_usage[0].total_tokens == 11
    assert original.total_tokens == 30


def test_action_classifier_usage_is_retained_outside_production():
    result = SimpleNamespace(final_output=ActionClaimBatch(claims=[]),
                             context_wrapper=SimpleNamespace(usage=Usage(requests=1, input_tokens=8, output_tokens=2, total_tokens=10)))
    run = Mock(return_value=result)
    classifier = SemanticActionClaimClassifier(run=run)
    score = evaluate_unsupported_actions(assistant_output="I cannot refund", user_input="refund", prohibited_actions=["issue_refund"],
                                        classifier_factory=lambda: classifier)
    assert score.passed
    assert score.evaluation_usage[0].total_tokens == 10
    assert score.evaluation_usage[0].sdk_visible_requests == 1
    assert score.evaluation_usage[0].http_retry_count is None
    run.assert_called_once()


def test_unknown_metric_usage_is_not_zero_and_bad_observation_is_non_authoritative():
    class Broken:
        @property
        def input_tokens(self):
            raise RuntimeError("private")
    for source in (SimpleNamespace(), Broken()):
        usage = capture("judge", source)
        assert usage.input_tokens is usage.output_tokens is usage.total_tokens is None


def test_reused_classifier_does_not_report_stale_usage_after_failure():
    result = SimpleNamespace(final_output=ActionClaimBatch(claims=[]),
                             context_wrapper=SimpleNamespace(usage=Usage(requests=1, input_tokens=8, output_tokens=2, total_tokens=10)))
    classifier = SemanticActionClaimClassifier(run=Mock(side_effect=[result, RuntimeError("private")]))
    arguments = dict(assistant_output="I cannot refund", user_input="refund", prohibited_actions=["issue_refund"],
                     classifier_factory=lambda: classifier)
    assert evaluate_unsupported_actions(**arguments).evaluation_usage[0].total_tokens == 10
    failed = evaluate_unsupported_actions(**arguments)
    assert not failed.passed and failed.evaluation_usage == []
