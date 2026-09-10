"""Validate response text and tool calls from one live execution per scenario."""

import json
from pathlib import Path

import pytest

from src.agentguard.evaluation_record import execute_scenario


DATASET_FILE = Path(__file__).resolve().parent / "datasets" / "functional.json"
with DATASET_FILE.open(encoding="utf-8") as dataset_file:
    SCENARIOS = json.load(dataset_file)


def normalize_arguments(arguments: dict) -> dict:
    return {
        key: value.lower() if key == "order_id" and isinstance(value, str) else value
        for key, value in arguments.items()
    }


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[case["id"] for case in SCENARIOS])
def test_agentguard_record(scenario):
    record = execute_scenario(scenario)
    final_output = record.final_output.lower()
    failures = []

    for expected in scenario["expected_contains"]:
        if expected.lower() not in final_output:
            failures.append(f"Missing expected text {expected!r}")
    for forbidden in scenario["forbidden_contains"]:
        if forbidden.lower() in final_output:
            failures.append(f"Found forbidden text {forbidden!r}")

    # Each expected call must have a distinct match; extra calls are allowed.
    remaining_calls = record.tool_calls.copy()
    for expected in scenario["expected_tools"]:
        candidates = [call for call in remaining_calls if call["name"] == expected["name"]]
        if not candidates:
            failures.append(f"Missing expected tool call {expected!r}")
            continue
        match = next(
            (
                call for call in candidates
                if isinstance(call["arguments"], dict)
                and normalize_arguments(call["arguments"])
                == normalize_arguments(expected["arguments"])
            ),
            None,
        )
        if match is None:
            failures.append(f"Arguments did not match expected tool call {expected!r}")
        else:
            remaining_calls.remove(match)

    assert not failures, (
        f"Scenario {scenario['id']}:\n"
        + "\n".join(failures)
        + f"\nActual final output: {record.final_output!r}"
        + f"\nExpected tool calls: {scenario['expected_tools']!r}"
        + f"\nActual tool calls: {record.tool_calls!r}"
    )
