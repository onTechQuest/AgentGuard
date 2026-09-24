"""Live safety evaluations: one captured agent execution per dataset scenario."""

import json
from pathlib import Path

import pytest

from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.safety_evaluator import safety_evaluate_record


DATASET_FILE = Path(__file__).resolve().parent / "datasets" / "safety.json"
with DATASET_FILE.open(encoding="utf-8") as dataset_file:
    SCENARIOS = json.load(dataset_file)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_agent_safety(scenario):
    record = execute_scenario(scenario)
    score = safety_evaluate_record(scenario, record)

    assert score.passed, (
        f"Scenario ID: {scenario['id']}\n"
        f"User input: {scenario['input']}\n"
        f"Final output: {record.final_output}\n"
        f"Tool calls: {record.tool_calls!r}\n"
        "Safety failure reasons:\n"
        + "\n".join(f"  - {reason}" for reason in score.failures)
    )
