"""No network: the one-shot diagnostic's execution and publication contracts."""
from concurrent.futures import Future
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import diagnose_decisions as diagnostic
from src.agentguard.datasets import load_dataset
from src.agent.runtime_reliability import default_runtime_policy
from src.hosting.runtime import Admission, CompletedRequest


@pytest.fixture
def scenario():
    return next(s for s in load_dataset(diagnostic.ROOT / "evals/datasets/functional.json", dataset_type="functional", suite="full")
                if s["id"] == "order_status_004")


def test_one_owned_diagnostic_request_with_persistable_evidence(scenario):
    chain = {"router": {"bindings": []}, "completeness": {"review_triggered": False},
             "policy": {"needs_clarification": False},
             "execution_plan": {"required_operations": [], "authorized_grants": [], "empty_reason": "EMPTY_BINDINGS_UNASSESSED"}}
    result = CompletedRequest("request", 0, "success", 0, 5, 5, 20, "Unable to look up the order.",
        json.dumps({"request_id": "request", "decision_evidence": chain}), json.dumps({"operations": []}), "[]")
    future = Future()
    future.set_result(result)
    runtime = Mock(_policy=default_runtime_policy())
    runtime.submit.return_value = Admission("request", True, None, 0, 1, 20, future)
    runtime.status.return_value = {}
    factory = Mock(return_value=runtime)
    report = diagnostic.diagnose(scenario, runtime_factory=factory)
    factory.assert_called_once_with(1, 0, diagnostic_mode=True)
    runtime.submit.assert_called_once_with(scenario["input"])
    runtime.shutdown.assert_called_once()
    assert report["requests"][0]["decision_evidence"] == chain
    assert report["diagnostic"]["decision_categories"] == ["ROUTER_OMISSION", "COMPLETENESS_SKIP"]
    assert report["diagnostic"]["tool_pass"] is False
    assert report["diagnostic"]["executions"] == 1
    assert scenario["input"] not in json.dumps(report)
    assert result.final_output not in json.dumps(report)


def test_diagnostic_error_is_not_retried(scenario):
    runtime = Mock()
    runtime.submit.side_effect = RuntimeError("failure")
    with pytest.raises(RuntimeError, match="failure"):
        diagnostic.diagnose(scenario, runtime_factory=Mock(return_value=runtime))
    runtime.submit.assert_called_once()
    runtime.shutdown.assert_called_once()


@pytest.mark.parametrize("flag", [False, True])
def test_cli_requires_explicit_live_flag_and_publishes_once(tmp_path, monkeypatch, scenario, flag):
    dataset = tmp_path / "evals/datasets"
    dataset.mkdir(parents=True)
    (dataset / "functional.json").write_text(json.dumps([scenario]))
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    run = Mock(return_value={"diagnostic": {"evidence_available": True}})
    monkeypatch.setattr(diagnostic, "diagnose", run)
    output = tmp_path / "reports/result.json"
    args = ["--scenario", scenario["id"], "--output", str(output)]
    if not flag:
        with pytest.raises(SystemExit):
            diagnostic.main(args)
        run.assert_not_called()
    else:
        assert diagnostic.main(args + ["--execute-live"]) == 0
        run.assert_called_once_with(scenario)
        assert json.loads(output.read_text())["diagnostic"]["evidence_available"]
        with pytest.raises(SystemExit):
            diagnostic.main(args + ["--execute-live"])
        run.assert_called_once()


def test_claim_prevents_rerun_after_failed_diagnostic(tmp_path, monkeypatch, scenario):
    dataset = tmp_path / "evals/datasets"
    dataset.mkdir(parents=True)
    (dataset / "functional.json").write_text(json.dumps([scenario]))
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    run = Mock(side_effect=RuntimeError("execution failed"))
    monkeypatch.setattr(diagnostic, "diagnose", run)
    args = ["--scenario", scenario["id"], "--output", str(tmp_path / "reports/result.json"), "--execute-live"]
    with pytest.raises(RuntimeError):
        diagnostic.main(args)
    with pytest.raises(FileExistsError):
        diagnostic.main(args)
    run.assert_called_once()
