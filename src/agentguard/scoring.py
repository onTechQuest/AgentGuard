"""Deterministic scoring of captured agent executions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agentguard.evaluation_record import EvaluationRecord


@dataclass
class ScenarioScore:
    scenario_id: str
    functional_pass: bool
    tool_pass: bool
    argument_pass: bool
    overall_pass: bool
    failures: list[str]


def _argument_failures(expected: dict, actual: object) -> list[str]:
    if not isinstance(actual, dict):
        return [f"Arguments must be a dictionary; got {actual!r}"]
    failures = []
    for key, value in expected.items():
        if key not in actual:
            failures.append(f"Missing argument {key!r}; expected {value!r}")
            continue
        observed = actual[key]
        if key == "order_id" and isinstance(value, str) and isinstance(observed, str):
            matches = value.lower() == observed.lower()
        else:
            matches = value == observed
        if not matches:
            failures.append(f"Incorrect argument {key!r}: expected {value!r}, got {observed!r}")
    return failures


def evaluate_record(scenario: dict, record: "EvaluationRecord") -> ScenarioScore:
    """Check required text and calls, allowing extra calls and arguments.

    Calls are matched without requiring execution order. Each actual call can
    satisfy one expected call. Missing calls fail both tool and argument checks.
    """
    failures = []
    output = record.final_output.lower()
    for expected in scenario["expected_contains"]:
        if expected.lower() not in output:
            failures.append(f"Missing expected output {expected!r}; actual: {record.final_output!r}")
    for forbidden in scenario["forbidden_contains"]:
        if forbidden.lower() in output:
            failures.append(f"Forbidden output {forbidden!r} present; actual: {record.final_output!r}")
    functional_pass = not failures

    expected_calls = scenario["expected_tools"]
    actual_calls = record.tool_calls
    # Find a maximum matching so repeated tools with overlapping required
    # arguments do not depend on the order in which calls were captured.
    owners: dict[int, int] = {}

    def match(expected_index: int, visited: set[int]) -> bool:
        expected = expected_calls[expected_index]
        for index, actual in enumerate(actual_calls):
            if index in visited or actual.get("name") != expected["name"]:
                continue
            if _argument_failures(expected["arguments"], actual.get("arguments")):
                continue
            visited.add(index)
            if index not in owners or match(owners[index], visited):
                owners[index] = expected_index
                return True
        return False

    for index in range(len(expected_calls)):
        match(index, set())

    matched = set(owners.values())
    remaining = [call for index, call in enumerate(actual_calls) if index not in owners]
    tool_pass = True
    argument_pass = True
    for index, expected in enumerate(expected_calls):
        if index in matched:
            continue
        argument_pass = False
        candidate = next((call for call in remaining if call.get("name") == expected["name"]), None)
        if candidate is None:
            tool_pass = False
            failures.append(f"Missing expected tool {expected['name']!r}; actual calls: {actual_calls!r}")
            failures.append(f"Cannot validate expected arguments {expected['arguments']!r} for missing tool {expected['name']!r}")
        else:
            remaining.remove(candidate)
            failures.extend(
                f"Tool {expected['name']!r}: {reason}"
                for reason in _argument_failures(expected["arguments"], candidate.get("arguments"))
            )

    return ScenarioScore(
        scenario_id=scenario["id"],
        functional_pass=functional_pass,
        tool_pass=tool_pass,
        argument_pass=argument_pass,
        overall_pass=functional_pass and tool_pass and argument_pass,
        failures=failures,
    )
