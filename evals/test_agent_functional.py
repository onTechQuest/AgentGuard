"""Dataset-driven functional evaluations that call the live support agent."""

import json
from pathlib import Path

import pytest

from src.agent.support_agent import run_support_agent


DATASET_FILE = Path(__file__).resolve().parent / "datasets" / "functional.json"
with DATASET_FILE.open(encoding="utf-8") as dataset_file:
    CASES = json.load(dataset_file)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_agent_functional(case):
    response = run_support_agent(case["input"])
    normalized_response = response.lower()

    for expected in case["expected_contains"]:
        assert expected.lower() in normalized_response, (
            f"Case {case['id']}: missing expected value {expected!r}. "
            f"Actual response: {response!r}"
        )

    for forbidden in case["forbidden_contains"]:
        assert forbidden.lower() not in normalized_response, (
            f"Case {case['id']}: found forbidden value {forbidden!r}. "
            f"Actual response: {response!r}"
        )
