from copy import deepcopy

import pytest

from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.scoring import evaluate_record


@pytest.fixture
def sample():
    scenario = {
        "id": "status-1", "expected_contains": ["ORD-1001", "shipped"],
        "forbidden_contains": ["not found"],
        "expected_tools": [{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}],
    }
    record = EvaluationRecord(
        "status-1", "Where is my order?", "ORD-1001 SHIPPED",
        deepcopy(scenario["expected_tools"]), 1.0, None, None, None, None,
    )
    return scenario, record


def test_all_passing(sample):
    scenario, record = sample
    before = deepcopy((scenario, record))
    score = evaluate_record(scenario, record)
    assert score.scenario_id == scenario["id"]
    assert score.functional_pass and score.tool_pass and score.argument_pass and score.overall_pass
    assert score.failures == []
    assert (scenario, record) == before


@pytest.mark.parametrize("output, reason", [
    ("ORD-1001", "Missing expected output 'shipped'"),
    ("ORD-1001 shipped NOT FOUND", "Forbidden output 'not found'"),
])
def test_functional_failures(sample, output, reason):
    scenario, record = sample
    record.final_output = output
    score = evaluate_record(scenario, record)
    assert not score.functional_pass and not score.overall_pass
    assert score.tool_pass and score.argument_pass
    assert any(reason in failure for failure in score.failures)


@pytest.mark.parametrize("calls", [[], [{"name": "check_return_eligibility", "arguments": {"order_id": "ORD-1001"}}]])
def test_missing_or_wrong_tool(sample, calls):
    scenario, record = sample
    record.tool_calls = calls
    score = evaluate_record(scenario, record)
    assert not score.tool_pass and not score.argument_pass and not score.overall_pass
    assert any("Missing expected tool 'get_order_status'" in failure for failure in score.failures)


@pytest.mark.parametrize("arguments, reason", [
    ({}, "Missing argument 'order_id'"),
    ({"order_id": "ORD-9999"}, "Incorrect argument 'order_id'"),
    ("invalid-json", "Arguments must be a dictionary"),
])
def test_argument_failures(sample, arguments, reason):
    scenario, record = sample
    record.tool_calls[0]["arguments"] = arguments
    score = evaluate_record(scenario, record)
    assert score.functional_pass and score.tool_pass
    assert not score.argument_pass and not score.overall_pass
    assert any(reason in failure for failure in score.failures)


def test_case_insensitive_order_id_and_extra_arguments(sample):
    scenario, record = sample
    record.tool_calls[0]["arguments"] = {"order_id": "ord-1001", "extra": True}
    assert evaluate_record(scenario, record).overall_pass


def test_order_id_whitespace_matches_tool_normalization(sample):
    scenario, record = sample
    record.tool_calls[0]["arguments"] = {"order_id": "  ord-1001  "}
    assert evaluate_record(scenario, record).overall_pass


def test_other_arguments_are_case_sensitive(sample):
    scenario, record = sample
    scenario["expected_tools"][0]["arguments"]["carrier"] = "UPS"
    record.tool_calls[0]["arguments"]["carrier"] = "ups"
    assert not evaluate_record(scenario, record).argument_pass


def test_multiple_failures(sample):
    scenario, record = sample
    record.final_output = "NOT FOUND"
    scenario["expected_tools"][0]["arguments"]["quantity"] = 2
    record.tool_calls[0]["arguments"] = {"quantity": 3}
    score = evaluate_record(scenario, record)
    assert not score.functional_pass and not score.argument_pass and not score.overall_pass
    assert len(score.failures) == 5


def test_repeated_tools_match_distinct_calls_in_any_order(sample):
    scenario, record = sample
    scenario["expected_tools"].insert(0, {"name": "get_order_status", "arguments": {}})
    record.tool_calls.append({"name": "get_order_status", "arguments": {"order_id": "ORD-1002"}})
    assert evaluate_record(scenario, record).overall_pass
    record.tool_calls.pop()
    assert not evaluate_record(scenario, record).tool_pass


def test_extra_calls_allowed(sample):
    scenario, record = sample
    record.tool_calls.append({"name": "check_return_eligibility", "arguments": {"order_id": "ORD-1001"}})
    assert evaluate_record(scenario, record).overall_pass


def test_empty_expectations(sample):
    scenario, record = sample
    scenario.update(expected_contains=[], forbidden_contains=[], expected_tools=[])
    record.tool_calls = []
    assert evaluate_record(scenario, record).overall_pass


def test_minimal_return_trajectory_passes_but_redundant_status_call_fails(sample):
    scenario, record = sample
    scenario.update(expected_contains=[], allowed_tools=["check_return_eligibility"],
                    expected_tools=[{"name": "check_return_eligibility", "arguments": {"order_id": "ORD-1001"}}])
    record.tool_calls = deepcopy(scenario["expected_tools"])
    assert evaluate_record(scenario, record).overall_pass
    record.tool_calls.insert(0, {"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}})
    score = evaluate_record(scenario, record)
    assert not score.tool_pass and not score.overall_pass
    assert score.argument_pass
    assert score.failures == [
        "Tool-policy failure: unexpected_tools=['get_order_status']; "
        "allowed_tools=['check_return_eligibility']; actual_tools=['get_order_status', 'check_return_eligibility']"
    ]
