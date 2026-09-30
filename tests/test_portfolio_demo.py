"""Committed portfolio evidence stays valid, private and inspectable offline."""
import json
from pathlib import Path
import re
import shutil
from unittest.mock import Mock

import pytest

from src.agentguard.lineage import load_run
from src.agentguard.safety_incidents import Redactor

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/portfolio/evaluations"
SUCCESS = "17000000000000000000000000000001"
DEADLINE = "17000000000000000000000000000002"


@pytest.mark.parametrize("run_id,state", [(SUCCESS, "COMPLETED"), (DEADLINE, "INCOMPLETE")])
def test_demo_artifact_contract(run_id, state):
    run = load_run(EXAMPLES / run_id)
    assert run["manifest"]["execution_mode"] == "offline_fixture"
    assert run["completion_state"] == state
    if run_id == SUCCESS:
        assert run["results"]["aggregate_results"]["quality_result"]["passed"] is True
        assert all(row["completed"] for row in run["results"]["observations"])
    else:
        evidence = run["results"]["executions"][0]["failure_evidence"]
        assert evidence["exception_type"] == "RequestDeadlineExceeded"
        assert evidence["failure_category"] == "DEADLINE_EXHAUSTED"
        assert evidence["request_id"] == "demo-deadline-request"
        assert evidence["result_abandoned"] is True
        assert evidence["retry_attempts_total"] == 0
        assert run["results"]["completed_scenario_count"] == 0


@pytest.mark.parametrize("run_id", [SUCCESS, DEADLINE])
def test_demo_inspection_without_execution(tmp_path, monkeypatch, capsys, run_id):
    import agents
    from scripts import inspect_observability as cli
    from src.agentguard.observability import validate_event
    from src.agentguard import evaluation_record, semantic_evaluator, safety_evaluator
    forbidden = Mock(side_effect=AssertionError("Demo cannot execute requests or judges"))
    monkeypatch.setattr(agents.Runner, "run", forbidden)
    monkeypatch.setattr(agents.Runner, "run_sync", forbidden)
    monkeypatch.setattr(evaluation_record, "execute_scenario", forbidden)
    monkeypatch.setattr(semantic_evaluator, "evaluate_semantics", forbidden)
    monkeypatch.setattr(safety_evaluator, "safety_evaluate_record", forbidden)
    source = EXAMPLES / run_id
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    shutil.copytree(source, tmp_path / "reports/evaluations" / run_id)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["inspect_observability.py", "--run-id", run_id, "--scenario-id", "order_status_001", "--json"])
    cli.main()
    assert "agentguard.request" in capsys.readouterr().out
    artifact = next((tmp_path / "reports/observability" / run_id).glob("*.json"))
    events = json.loads(artifact.read_text())["events"]
    for event in events:
        validate_event(event)
        assert event["run_id"] == run_id and event["invocation_id"] is None
    if run_id == DEADLINE:
        synthesis = next(e for e in events if e.get("component") == "synthesis")
        assert synthesis["status"] == "ERROR"
        assert synthesis["attributes"]["result_abandoned"] is True
    forbidden.assert_not_called()
    assert original == {p.name: p.read_bytes() for p in source.iterdir()}


def test_demo_privacy_and_no_machine_paths():
    forbidden_keys = {"input", "prompt", "response", "final_output", "tool_outputs", "arguments", "headers", "credentials", "api_key", "customer_id", "exception_message"}
    def check(value):
        if isinstance(value, dict):
            assert not forbidden_keys.intersection(value)
            for child in value.values(): check(child)
        elif isinstance(value, list):
            for child in value: check(child)
        elif isinstance(value, str):
            assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/Users/|/home/", value)
    for path in EXAMPLES.rglob("*.json"):
        document = json.loads(path.read_text())
        check(document)
        assert Redactor([document]).clean(document) == document
