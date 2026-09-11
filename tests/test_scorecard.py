from copy import deepcopy

import pytest

from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.scorecard import AgentGuardScorecard, build_scorecard
from src.agentguard.scoring import ScenarioScore


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

    assert card == AgentGuardScorecard(4, 1, 3, 0.25, 0.5, 0.75, 250.0, 400.0, 100.0)
    assert pairs == original


def test_empty_input():
    assert build_scorecard([]) == AgentGuardScorecard(
        0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None,
    )


def test_single_scenario():
    assert build_scorecard([make_pair(12.5, 7)]) == AgentGuardScorecard(
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
