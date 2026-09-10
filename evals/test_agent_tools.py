"""Live evaluations of function calls recorded by the support agent."""

import json
from pathlib import Path

import pytest
from agents.items import ToolCallItem
from openai.types.responses import ResponseFunctionToolCall

from src.agent.support_agent import run_support_agent_detailed


DATASET_FILE = Path(__file__).resolve().parent / "datasets" / "functional.json"
with DATASET_FILE.open(encoding="utf-8") as dataset_file:
    CASES = json.load(dataset_file)


def normalized_arguments(arguments: dict) -> dict:
    """Compare order IDs without case differences; preserve all other values."""
    return {
        key: value.lower() if key == "order_id" and isinstance(value, str) else value
        for key, value in arguments.items()
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_agent_tools(case):
    result = run_support_agent_detailed(case["input"])
    actual_calls = []
    errors = []
    for item in result.new_items:
        if not isinstance(item, ToolCallItem):
            continue
        raw = item.raw_item
        if not isinstance(raw, ResponseFunctionToolCall):
            continue
        try:
            arguments = json.loads(raw.arguments)
        except (TypeError, ValueError):
            arguments = raw.arguments
            errors.append(f"Invalid JSON arguments for tool {raw.name!r}")
        if not isinstance(arguments, dict):
            errors.append(f"Arguments for tool {raw.name!r} must be an object")
        actual_calls.append({"name": raw.name, "arguments": arguments})

    expected_calls = case["expected_tools"]
    details = (
        f"Scenario {case['id']}\n"
        f"Expected tool calls: {expected_calls!r}\n"
        f"Actual tool calls: {actual_calls!r}"
    )
    assert not errors, f"{'; '.join(errors)}\n{details}"

    # Match calls without requiring an execution order, consuming each once.
    remaining = actual_calls.copy()
    for expected in expected_calls:
        candidates = [call for call in remaining if call["name"] == expected["name"]]
        if not candidates:
            errors.append(f"Missing expected tool {expected['name']!r}")
            continue
        expected_arguments = normalized_arguments(expected["arguments"])
        match = next(
            (call for call in candidates
             if normalized_arguments(call["arguments"]) == expected_arguments),
            None,
        )
        if match is not None:
            remaining.remove(match)
            continue

        actual = candidates[0]
        remaining.remove(actual)
        actual_arguments = normalized_arguments(actual["arguments"])
        for key, value in expected_arguments.items():
            if key not in actual_arguments:
                errors.append(f"Missing argument {key!r} for tool {expected['name']!r}")
            elif actual_arguments[key] != value:
                errors.append(f"Incorrect argument {key!r} for tool {expected['name']!r}")
        for key in actual_arguments.keys() - expected_arguments.keys():
            errors.append(f"Unexpected argument {key!r} for tool {expected['name']!r}")

    for call in remaining:
        errors.append(f"Wrong or unexpected extra tool call: {call['name']!r}")
    assert not errors, f"{'; '.join(errors)}\n{details}"
