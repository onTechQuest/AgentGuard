from copy import deepcopy
from dataclasses import replace

import pytest

from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.scorecard import AgentGuardScorecard, build_scorecard
from src.agentguard.scoring import ScenarioScore
from src.agentguard.semantic_evaluator import SemanticScore
from src.agentguard.safety_evaluator import SafetyScore


def make_pair(latency=100.0, tokens=None, passes=(True, True, True)):
    record = EvaluationRecord(
        scenario_id="example", input="Question", final_output="Answer",
        tool_calls=[], latency_ms=latency, request_count=None,
        input_tokens=None, output_tokens=None, total_tokens=tokens,
    )
    score = ScenarioScore("example", *passes, all(passes), [])
    return record, score


def test_aggregate_metrics_and_preserve_inputs():
    pairs = [
        make_pair(400.0, 100),
        make_pair(100.0, None, (False, True, True)),
        make_pair(300.0, 200, (False, False, True)),
        make_pair(200.0, 0, (False, False, False)),
    ]
    original = deepcopy(pairs)

    card = build_scorecard(pairs)

    assert replace(card, functional_production_usage=None) == AgentGuardScorecard(4, 1, 3, 0.25, 0.5, 0.75, 250.0, 400.0, 100.0)
    assert pairs == original


def test_empty_input():
    assert replace(build_scorecard([]), functional_production_usage=None) == AgentGuardScorecard(
        0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None,
    )


def test_single_scenario():
    assert replace(build_scorecard([make_pair(12.5, 7)]), functional_production_usage=None) == AgentGuardScorecard(
        1, 1, 0, 1.0, 1.0, 1.0, 12.5, 12.5, 7.0,
    )


@pytest.mark.parametrize("count, expected", [(2, 2), (19, 19), (20, 19), (21, 20), (100, 95)])
def test_p95_nearest_rank_on_unsorted_generator(count, expected):
    pairs = (make_pair(float(value)) for value in range(count, 0, -1))
    card = build_scorecard(pairs)
    assert card.p95_latency_ms == expected
    assert card.total_scenarios == count


@pytest.mark.parametrize("tokens, expected", [
    ([None, None], None),
    ([None, 0], 0.0),
    ([1, None, 2], 1.5),
])
def test_available_token_average(tokens, expected):
    card = build_scorecard(make_pair(tokens=value) for value in tokens)
    assert card.average_tokens_per_run == expected


def test_failed_count_uses_overall_pass():
    pair = make_pair()
    pair[1].overall_pass = False
    card = build_scorecard([pair])
    assert card.passed_scenarios == 0
    assert card.failed_scenarios == 1
    assert card.functional_accuracy == card.tool_accuracy == card.argument_accuracy == 1.0


def test_zero_and_repeated_latencies():
    card = build_scorecard([make_pair(0.0), make_pair(0.0)])
    assert card.average_latency_ms == card.p95_latency_ms == 0.0


def test_functional_safety_and_judge_usage_are_separate():
    functional = [make_pair(100, 1200), make_pair(200, 1300)]
    safety = [make_pair(22493, 4000)[0], make_pair(900, None)[0]]
    semantic = make_semantic()
    semantic.total_tokens = 999999  # Evaluation-only counters are never production inputs.
    semantic.latency_ms = 999999
    card = build_scorecard(functional, semantic_scores=[semantic], safety_records=safety)
    assert card.functional_production_usage.execution_count == 2
    assert card.functional_production_usage.average_latency_ms == card.average_latency_ms == 150
    assert card.functional_production_usage.average_tokens_per_execution == card.average_tokens_per_run == 1250
    assert card.safety_production_usage.execution_count == 2
    assert card.safety_production_usage.maximum_latency_ms == 22493
    assert len(card.safety_production_usage.exceeding(7500)) == 1
    assert card.safety_production_usage.average_tokens_per_execution == 4000
    assert card.safety_production_usage.token_observation_count == 1


def make_semantic(scores=(0.9, 0.95, 1.0), passes=(True, True, True)):
    return SemanticScore(
        scenario_id="example",
        answer_relevancy_score=scores[0], answer_relevancy_pass=passes[0], answer_relevancy_reason=None,
        correctness_score=scores[1], correctness_pass=passes[1], correctness_reason=None,
        hallucination_score=scores[2], hallucination_pass=passes[2], hallucination_reason=None,
    )


def test_all_semantic_scores_available_and_deterministic_metrics_unchanged():
    pairs = [make_pair(100.0, 20), make_pair(300.0, 40, (False, True, False))]
    semantics = [make_semantic(), make_semantic((1.0, 0.85, 1.0))]
    before = deepcopy((pairs, semantics))

    card = build_scorecard(pairs, semantic_scores=semantics)

    assert replace(
        card, average_answer_relevancy=None, average_correctness=None,
        average_hallucination_score=None, semantic_pass_rate=None, functional_production_usage=None,
    ) == AgentGuardScorecard(
        total_scenarios=2, passed_scenarios=1, failed_scenarios=1,
        functional_accuracy=0.5, tool_accuracy=1.0, argument_accuracy=0.5,
        average_latency_ms=200.0, p95_latency_ms=300.0, average_tokens_per_run=30.0,
    )
    assert card.average_answer_relevancy == pytest.approx(0.95)
    assert card.average_correctness == pytest.approx(0.9)
    assert card.average_hallucination_score == 1.0
    assert card.semantic_pass_rate == 1.0
    assert (pairs, semantics) == before


def test_skipped_scores_have_independent_denominators():
    semantics = [
        make_semantic((0.9, 0.8, None), (True, False, None)),
        None,
        make_semantic((None, 1.0, 0.0), (None, True, False)),
    ]

    card = build_scorecard([make_pair()] * 3, semantics)

    assert card.average_answer_relevancy == 0.9
    assert card.average_correctness == 0.9
    assert card.average_hallucination_score == 0.0
    assert card.semantic_pass_rate == 0.5


@pytest.mark.parametrize("semantics", [
    None, [], [None],
    [make_semantic((None, None, None), (None, None, None))],
])
def test_all_semantics_absent(semantics):
    card = build_scorecard([make_pair()], semantics)

    assert card.average_answer_relevancy is None
    assert card.average_correctness is None
    assert card.average_hallucination_score is None
    assert card.semantic_pass_rate is None
    assert card.total_scenarios == card.passed_scenarios == 1


def test_semantic_pass_rate_counts_checks_instead_of_scenarios():
    semantics = [
        make_semantic((0.95, 0.9, None), (True, True, None)),
        make_semantic((0.5, 0.7, 0.0), (False, False, False)),
    ]

    card = build_scorecard((make_pair() for _ in semantics), (score for score in semantics))

    assert card.semantic_pass_rate == pytest.approx(2 / 5)
    assert card.passed_scenarios == 2
    assert card.failed_scenarios == 0


def test_zero_semantic_scores_and_all_failed_checks():
    card = build_scorecard([make_pair()], [make_semantic((0.0, 0.0, 0.0), (False, False, False))])

    assert card.average_answer_relevancy == 0.0
    assert card.average_correctness == 0.0
    assert card.average_hallucination_score == 0.0
    assert card.semantic_pass_rate == 0.0


@pytest.mark.parametrize("unavailable", [float("nan"), float("inf"), float("-inf"), "0.9", True])
def test_semantic_averages_exclude_non_numeric_and_non_finite_values(unavailable):
    semantics = [
        make_semantic((unavailable, unavailable, unavailable), (None, None, None)),
        make_semantic((0.9, 0.95, 1.0)),
    ]

    card = build_scorecard([make_pair()] * 2, semantics)

    assert card.average_answer_relevancy == 0.9
    assert card.average_correctness == 0.95
    assert card.average_hallucination_score == 1.0
    assert card.semantic_pass_rate == 1.0


def test_available_checks_and_scores_are_aggregated_independently():
    semantics = [
        make_semantic((None, None, None), (True, False, None)),
        make_semantic((0.9, 0.95, 1.0), (None, None, None)),
    ]

    card = build_scorecard([make_pair()] * 2, semantics)

    assert card.average_answer_relevancy == 0.9
    assert card.average_correctness == 0.95
    assert card.average_hallucination_score == 1.0
    assert card.semantic_pass_rate == 0.5


def test_safety_aggregation_counts_scenarios_and_explicit_category_failures():
    safety = [
        SafetyScore("safe", True, True, None, None, True, []),
        SafetyScore("multi", False, False, False, None, False, ["multiple failures"]),
        SafetyScore("data", False, None, None, False, None, ["disclosure"]),
        SafetyScore("tool", False, None, None, None, False, ["missing tool"]),
    ]
    before = deepcopy(safety)
    pairs = [make_pair(123, 42)]
    semantics = [make_semantic()]
    baseline = build_scorecard(pairs, semantics)

    card = build_scorecard(pairs, semantics, (score for score in safety))

    assert card.safety_pass_rate == 0.25
    assert card.prompt_injection_failures == 1
    assert card.unsupported_action_failures == 1
    assert card.data_protection_failures == 1
    assert card.tool_policy_failures == 2
    assert replace(
        card, safety_pass_rate=None, prompt_injection_failures=0, unsupported_action_failures=0,
        data_protection_failures=0, tool_policy_failures=0,
    ) == baseline
    assert safety == before


def test_all_safety_scenarios_pass_with_inapplicable_categories():
    card = build_scorecard([], safety_scores=[
        SafetyScore("refusal", True, None, True, None, None, []),
        SafetyScore("data", True, None, None, True, None, []),
    ])
    assert card.safety_pass_rate == 1.0
    assert (card.prompt_injection_failures, card.unsupported_action_failures,
            card.data_protection_failures, card.tool_policy_failures) == (0, 0, 0, 0)


@pytest.mark.parametrize("safety", [None, []])
def test_no_safety_scores_is_unavailable(safety):
    card = build_scorecard([], safety_scores=safety)
    assert card.safety_pass_rate is None
    assert (card.prompt_injection_failures, card.unsupported_action_failures,
            card.data_protection_failures, card.tool_policy_failures) == (0, 0, 0, 0)


def test_safety_pass_rate_uses_overall_pass_even_without_category_failure():
    card = build_scorecard([], safety_scores=[SafetyScore("other", False, None, None, None, None, ["forbidden claim"])])
    assert card.safety_pass_rate == 0.0
    assert card.prompt_injection_failures == 0
