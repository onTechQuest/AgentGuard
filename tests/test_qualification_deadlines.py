"""Opt-in/config/CLI/transport contracts and saved timing replay; all offline."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import run_agentguard_eval as runner
from src.agent.qualification_budget import QualificationBudgetPolicy
from src.agent.request_budget import RequestBudget
from src.agent.retry_policy import ModelRetryPolicy
from src.agentguard.deadline_replay import replay_observation, replay_reports
from src.agentguard.reliability import measure_request, qualify
from src.agentguard.reliability_policy import PolicyCandidate, QualificationConfig, load_qualification_config
from test_model_execution_transport import transport
from test_reliability_qualification import datasets, measured, observation


CONFIG = Path(__file__).resolve().parents[1] / "config/reliability-deadline-candidates.json"
L, R = load_qualification_config(CONFIG).candidates


def test_experimental_configuration_round_trip(tmp_path):
    config = load_qualification_config(CONFIG)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.snapshot()))
    assert load_qualification_config(path) == config
    assert R.qualification_budget_policy == QualificationBudgetPolicy(20000, 13000, 3000, 6000)
    assert all(not c.retry_policy.enabled and c.retry_policy.max_attempts == 1
               and c.retry_policy.shared_extra_attempts_per_request == 0 for c in config.candidates)


@pytest.mark.parametrize("enabled", [False, True])
def test_enforcement_is_explicit_even_when_candidate_selected(enabled):
    run = Mock(return_value=SimpleNamespace(context_wrapper=SimpleNamespace(production_telemetry=observation())))
    measure_request({"input": "test"}, candidate=L, enforce_candidate_budget=enabled, run=run)
    assert run.call_count == 1
    kwargs = run.call_args.kwargs
    assert kwargs["request_budget"].original_budget_ms == (10000 if enabled else None)
    runtime = kwargs["runtime_reliability_policy"]
    assert runtime.request_deadline_ms == (10000 if enabled else None)
    assert runtime.router_allowance_ms == (4000 if enabled else None)
    assert not kwargs["retry_policy"].enabled


@pytest.mark.parametrize("changes", [
    {"request_budget_ms": 10001},
    {"retry_policy": ModelRetryPolicy(max_attempts=2)},
    {"retry_policy": ModelRetryPolicy(shared_extra_attempts_per_request=1)},
])
def test_invalid_candidate_rejected_before_execution(changes):
    with pytest.raises(ValueError):
        replace(L, **changes)


@pytest.mark.parametrize("field", list(QualificationBudgetPolicy.__dataclass_fields__))
@pytest.mark.parametrize("value", [-1, True, float("inf"), None])
def test_invalid_budget_values(field, value):
    with pytest.raises(ValueError):
        replace(L.qualification_budget_policy, **{field: value})


def test_missing_policy_does_not_silently_activate_request_budget():
    run = Mock()
    with pytest.raises(ValueError):
        measure_request({"input": "test"}, candidate=PolicyCandidate("legacy", 10000),
                        enforce_candidate_budget=True, run=run)
    run.assert_not_called()


@pytest.mark.parametrize("suite", ["smoke", "full", "performance"])
def test_budget_flag_only_for_reliability(suite):
    with pytest.raises(SystemExit):
        runner.parse_args(["--suite", suite, "--enforce-candidate-budget"])


def test_cli_flags_and_no_retry_conflict():
    args = ["--suite", "reliability", "--qualification-config", str(CONFIG)]
    assert not runner.parse_args(args).enforce_candidate_budget
    assert runner.parse_args([*args, "--enforce-candidate-budget"]).enforce_candidate_budget
    with pytest.raises(SystemExit):
        runner.parse_args([*args, "--enforce-candidate-budget", "--execute-retries"])


def test_qualification_report_records_effective_policy(tmp_path):
    measure = Mock(side_effect=lambda *a, **kw: measured())
    config = QualificationConfig("smoke", 1, (L, R), L.name)
    report = qualify(config, datasets(), project_root=tmp_path, output=tmp_path / "reports/enforced.json",
                     enforce_candidate_budget=True, measure=measure)
    assert measure.call_count == 2
    assert all(c.kwargs["enforce_candidate_budget"] for c in measure.call_args_list)
    assert not report["execute_retries"]
    assert report["effective_budget_policy"]["router_allowance_ms"] == 4000
    assert report["enforcement_summary"]["completed_successes"] == 2


def test_cli_passes_enforcement_flag_without_extra_executions(monkeypatch, tmp_path):
    from src.agentguard import reliability
    measure = Mock(side_effect=lambda *a, **kw: measured())
    monkeypatch.setattr(reliability, "measure_request", measure)
    monkeypatch.setattr(runner, "load_datasets", lambda *a, **kw: datasets())
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    assert runner.main(["--suite", "reliability", "--qualification-config", str(CONFIG),
                        "--enforce-candidate-budget", "--execution-candidate", R.name,
                        "--report", str(tmp_path / "reports/r.json")]) == 0
    assert measure.call_count == 2
    assert all(c.kwargs["candidate"].name == R.name and c.kwargs["enforce_candidate_budget"]
               for c in measure.call_args_list)


@pytest.mark.parametrize("failure", [None, "429", "503", "read_timeout"])
def test_enforced_real_sdk_path_never_retries(transport, failure):
    wire = transport(failure=failure)
    wire.run(runtime_reliability_policy=L.qualification_budget_policy.runtime_policy())
    assert all(count == 1 for count in wire.attempts.values())
    assert wire.observed["deadline_budget_ms"] == 10000
    attempts = [a for s in wire.observed["component_spans"] for a in s["attempts"]]
    assert all(a["lower_layer_retries_configured"] is False for a in attempts)
    assert all(not a["retry_performed"] for a in attempts)
    assert not transport.sleeps


@pytest.mark.parametrize("component,expected_tokens", [("primary_router", 14), ("synthesis", 44)])
def test_real_sdk_late_completion_retains_actual_usage(transport, component, expected_tokens):
    wire = transport(late_component=component)
    policy = QualificationBudgetPolicy(10000, 500, 100, 500)
    wire.run(budget=RequestBudget(10000, clock=wire.clock), runtime_reliability_policy=policy.runtime_policy())
    assert wire.error is not None and wire.result is None
    assert wire.observed["terminal_failure_component"] == component
    assert wire.observed["result_abandoned"]
    assert not wire.observed["cancellation_observed"]
    assert wire.observed["observed_usage"]["total_tokens"] == expected_tokens
    assert wire.observed["usage_completeness"] == "COMPLETE"
    assert wire.attempts[component] == 1
    from src.agentguard.reliability import sanitize_measurement
    row = sanitize_measurement(wire.observed, status="failed", elapsed=1000)
    attempt = next(a for a in row["attempts"] if a["component"] == component)
    assert attempt["late_completion"] and attempt["result_abandoned"]
    assert attempt["result_accepted"] is False


def timing_row(router=1000, synthesis=1000, total=2100, recovery=None):
    attempts = [{"component": "primary_router", "started_offset_ms": 0}]
    if recovery is not None:
        attempts.append({"component": "recovery_planner", "started_offset_ms": router + 1})
    attempts.append({"component": "synthesis", "started_offset_ms": router + (recovery or 0) + 2})
    return {"status": "completed", "actual_extra_attempts": 0, "attempts": attempts,
            "latency_ms": {"primary_router": router, "synthesis": synthesis, "total_request": total,
                           "recovery_planner": recovery, "required_operations": 0.5},
            "recovery_executed": recovery is not None, "required_operation_count": 1}


# The six 13C.4E router exceedances, preserving measured full stage durations.
@pytest.mark.parametrize("router,synthesis,total", [
    (5171.3789, 1521.4846, 6694.1698), (12192.5488, 3132.547, 15326.9126),
    (5825.8528, 1536.3614, 7363.4113), (4121.0712, 1709.1921, 5832.9723),
    (8319.995, 5210.4493, 13531.476), (4195.5798, 1545.2416, 5741.9413),
])
def test_known_outliers_replay_through_runtime_boundaries(router, synthesis, total):
    row = timing_row(router, synthesis, total)
    left = replay_observation(row, L.qualification_budget_policy)
    right = replay_observation(row, R.qualification_budget_policy)
    assert not left["accepted"] and left["rejection"]["component"] == "primary_router"
    assert left["visited_model_stages"] == ["primary_router"]
    assert left["replayed_completed_operation_count"] == 0
    assert right["accepted"]


@pytest.mark.parametrize("candidate", [L, R])
def test_saved_recovery_replay(candidate):
    row = timing_row(1716.0662000533193, 1068.9058999996632, 4360.010900069028, 1572.716899914667)
    row["attempts"][1]["started_offset_ms"] = 1717.57710003294
    row["attempts"][2]["started_offset_ms"] = 3290.982500067912
    result = replay_observation(row, candidate.qualification_budget_policy)
    assert result["accepted"] and result["recovery_admission"]["admitted"]
    assert result["recovery_admission"]["remaining_budget_ms"] == pytest.approx(
        candidate.request_budget_ms - 1717.57710003294)


def test_replay_retains_populations_and_does_not_overwrite_sources(tmp_path):
    row = {**timing_row(), "dataset": "safety", "scenario_id": "probe", "repetition": 1,
           "tokens": {"production_total": {"total_tokens": 100}}}
    paths = [tmp_path / name for name in ("balanced.json", "tail_probe.json")]
    source = json.dumps({"complete": True, "observations": [row]})
    for path in paths:
        path.write_text(source)
    report = replay_reports(paths, [L, R])
    assert len(report["populations"]) == 4
    assert report["actual_live_calls"] == report["actual_extra_retry_attempts"] == 0
    assert all(path.read_text() == source for path in paths)


def test_replay_request_boundary_catches_local_remainder():
    result = replay_observation(timing_row(total=10000), L.qualification_budget_policy)
    assert result["rejection"]["component"] == "response_acceptance"
    assert result["replayed_completed_operation_count"] == 1


def test_replay_dynamic_recovery_denial():
    result = replay_observation(timing_row(12000, 1000, 15000, 1000), R.qualification_budget_policy)
    assert result["rejection"]["component"] == "recovery_planner"
    assert not result["recovery_admission"]["admitted"]
    assert result["visited_model_stages"] == ["primary_router"]
