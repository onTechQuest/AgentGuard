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


@pytest.fixture
def missing_order():
    from src.agentguard.datasets import load_datasets
    scenario = next(s for s in load_datasets().functional if s["id"] == "missing_order_001")
    record = EvaluationRecord(scenario["id"], scenario["input"], "", deepcopy(scenario["expected_tools"]),
        1.0, None, None, None, None,
        tool_outputs=[dict(name="get_order_status", output=dict(order_id="ORD-9999", found=False))])
    return scenario, record


@pytest.mark.parametrize("output", [
    "Order ORD-9999 not found.",
    "Order ORD-9999 was not found.",
    "Order ORD-9999 wasn't found.",
    "Order ORD-9999 wasn\u2019t found. Please verify the order ID and try again.",
    "Order ORD-9999 wasn\u00e2\u20ac\u2122t found. Please verify the order ID and try again.",
    "Order ORD-9999 could not be found.",
    "Order ORD-9999 does not exist.",
    "Order ORD-9999 doesn't exist.",
    "No order was found for ORD-9999.",
    "ORDER: `ord-9999`, WAS\tNOT   FOUND!",
    "Your order was not found in our records.",
    "ORD-9999 was not found.",
    "Order **ORD-9999** wasn\u2019t found. Please verify the order ID and try again.",
    "Order **ORD-9999** was not found.",
    "Order *ORD-9999* could not be found.",
    "Order *ORD-9999* does not exist.",
    "Order `ORD-9999` was not found.",
    "Order: **ORD-9999**, was not found!",
    "**Order ORD-9999 was not found.**",
    "Order **ORD-9999** **wasn't found**.",
])
def test_order_not_found_realizations(missing_order, output):
    scenario, record = missing_order
    record.final_output = output
    score = evaluate_record(scenario, record)
    assert score.functional_pass and score.tool_pass and score.argument_pass and score.overall_pass
    assert score.factual_grounding_pass is True


@pytest.mark.parametrize("output", [
    "Order ORD-9999 shipped yesterday.", "Order ORD-9999 is processing.",
    "I don't know where ORD-9999 is.", "Order status is unavailable.",
    "The carrier could not be found.", "The tracking number was not found.",
    "ORD-9999 was found but tracking was not found.", "Order ORD-9999 was found.", "",
    "Order ORD-1001 was not found.", "If order ORD-9999 was not found, try again.",
    "Order ORD-9999 was not found?", "Order ORD-9999 was not found. Order ORD-9999 is processing.",
    "The tracking number **wasn't found** for ORD-9999.",
    "The carrier **was not found** for ORD-9999.",
    "**ORD-9999** was found but tracking was not found.",
    "Order **ORD-9999** is processing.",
    "Order **ORD-9999** shipped yesterday.",
    "Order *ORD-9999* was found.",
    "Order `ORD-9999` is processing.",
    "Order **ORD-9999** was not found. Order **ORD-9999** is processing.",
])
def test_order_not_found_rejects_wrong_outcomes(missing_order, output):
    scenario, record = missing_order
    record.final_output = output
    score = evaluate_record(scenario, record)
    assert not score.functional_pass and not score.overall_pass
    assert "Expected behavior 'order_not_found' was not satisfied" in score.failures
    assert score.tool_pass and score.argument_pass


@pytest.mark.parametrize("text", [
    "ord**-9999**", "**ord**9999", "ord-**9999**", "**ord-9999**suffix",
    "ord*9999", "**ord-9999*", "* ord-9999 *", "```ord-9999```",
    "**ord-\n9999**",
])
def test_presentation_normalization_preserves_token_boundaries(text):
    from src.agentguard.behavior_predicates import _normalize
    assert _normalize(text) == text


@pytest.mark.parametrize("forbidden", ["UPS", "FedEx", "shipped"])
def test_behavior_preserves_forbidden_literals(missing_order, forbidden):
    scenario, record = missing_order
    record.final_output = f"Order ORD-9999 wasn't found. {forbidden}."
    score = evaluate_record(scenario, record)
    assert not score.functional_pass
    assert any("Forbidden output" in reason for reason in score.failures)


def test_behavior_and_literal_assertions_are_independent(missing_order):
    scenario, record = missing_order
    scenario["expected_contains"] = ["required literal"]
    record.final_output = "Order ORD-9999 wasn't found."
    assert not evaluate_record(scenario, record).functional_pass
    record.final_output += " required literal"
    assert evaluate_record(scenario, record).overall_pass
    scenario.pop("expected_behavior")
    scenario["expected_contains"] = ["not found"]
    assert not evaluate_record(scenario, record).functional_pass  # Literal contract unchanged.


def test_behavior_is_not_keyed_to_scenario_or_order_id(missing_order):
    scenario, record = missing_order
    scenario["id"] = record.scenario_id = "arbitrary-case"
    scenario["expected_tools"][0]["arguments"]["order_id"] = "ORD-8888"
    scenario["expected_authoritative_facts"][0]["order_id"] = "ORD-8888"
    record.tool_calls = deepcopy(scenario["expected_tools"])
    record.tool_outputs[0]["output"]["order_id"] = "ORD-8888"
    record.final_output = "Order ORD-8888 doesn't exist."
    assert evaluate_record(scenario, record).overall_pass


@pytest.mark.parametrize("change", ["missing_tool", "wrong_argument", "contradictory_fact", "missing_fact"])
def test_behavior_does_not_bypass_tool_argument_or_grounding_checks(missing_order, change):
    scenario, record = missing_order
    record.final_output = "Order ORD-9999 wasn't found."
    if change == "missing_tool": record.tool_calls = []
    elif change == "wrong_argument": record.tool_calls[0]["arguments"]["order_id"] = "ORD-1001"
    elif change == "contradictory_fact": record.tool_outputs[0]["output"]["found"] = True
    else: record.tool_outputs = []
    assert not evaluate_record(scenario, record).overall_pass


@pytest.mark.parametrize("behavior", ["unregistered", None, ["order_not_found"]])
def test_unknown_behavior_fails_scoring_explicitly(sample, behavior):
    scenario, record = sample
    scenario["expected_behavior"] = behavior
    score = evaluate_record(scenario, record)
    assert not score.functional_pass
    assert "Unknown or invalid expected_behavior assertion" in score.failures


def test_behavior_changes_only_expectation_and_composite_fingerprints(missing_order):
    from src.agentguard.lineage import scenario_identity
    scenario, _ = missing_order
    migrated = scenario_identity(scenario)
    old = deepcopy(scenario)
    old.pop("expected_behavior")
    old["expected_contains"] = ["not found"]
    original = scenario_identity(old)
    assert original["content_fingerprint"] == migrated["content_fingerprint"]
    assert original["metadata_fingerprint"] == migrated["metadata_fingerprint"]
    for key in ("expected_behavior_fingerprint", "scenario_fingerprint"):
        assert original[key] != migrated[key]
    scenario["expected_behavior"] = "different_behavior"
    assert scenario_identity(scenario)["expected_behavior_fingerprint"] != migrated["expected_behavior_fingerprint"]
