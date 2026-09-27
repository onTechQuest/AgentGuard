"""Reliability qualification never needs a live provider or judge."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from openai import RateLimitError

from scripts import run_agentguard_eval as runner
from src.agent import telemetry
from src.agent.retry_policy import DeliveryCertainty as Delivery, FailureEvidence, ModelRetryPolicy
from src.agent.telemetry import FailureCategory as Category
from src.agentguard.datasets import EvaluationDatasets
from src.agentguard.performance import distribution
from src.agentguard.reliability import (
    measure_request, qualify, sanitize_measurement, shadow_observation, stats, summarize,
)
from src.agentguard.reliability_policy import (
    PolicyCandidate, QualificationConfig, load_qualification_config, shadow_retry,
)
from test_retry_policy import policy
from test_model_execution_transport import transport, _wire, PRIVATE


SECRET = "private-prompt-customer-tool-payload-provider-body-api-key"


def candidate(**changes):
    return replace(PolicyCandidate("candidate_A", 1000, policy()), **changes)


def config(**changes):
    return replace(QualificationConfig("smoke", 1, (PolicyCandidate("baseline_no_retry"), candidate()),
                                       "baseline_no_retry", ("safety_probe",)), **changes)


def datasets():
    return EvaluationDatasets([{"id": "functional_a", "input": SECRET}],
                              [{"id": "safety_probe", "input": SECRET}])


def observation(*, recovery=False, status="completed", unknown=False, operations=1):
    components = ["primary_router", *( ["recovery_planner"] if recovery else []), "synthesis"]
    spans = []
    offset = 0
    for component in components:
        duration = 20
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        attempt = telemetry.AttemptTelemetry("logical", component, 1, offset, duration_ms=duration,
                    status="completed", usage_known=True, returned_usage=usage,
                    lower_layer_retries_configured=False, sdk_visible_requests=1)
        if unknown and component == "primary_router":
            attempt.returned_usage = {}
            attempt.usage_known = False
            usage = dict.fromkeys(usage)
        if status == "failed" and component == "synthesis":
            attempt.status = "failed"
            attempt.failure_category = Category.NETWORK_ERROR
            attempt.delivery_certainty = Delivery.NOT_SENT
        spans.append(telemetry.ComponentSpan(component, offset, duration_ms=duration,
                      configured_model="gpt-test", resolved_model="gpt-test", attempts=[attempt],
                      remaining_budget_before_ms=1000-offset, **usage))
        offset += duration
    spans.extend([telemetry.ComponentSpan("planning_completeness", 20, duration_ms=25 if recovery else 5),
                  telemetry.ComponentSpan("policy_resolution", 45, duration_ms=2),
                  telemetry.ComponentSpan("execution_plan", 47, duration_ms=3),
                  telemetry.ComponentSpan("required_execution", 50, duration_ms=10)])
    record = telemetry.ProductionExecutionTelemetry(component_spans=spans,
                planning_summary={"recovery_triggered": recovery},
                required_operation_summary={"required": [SECRET] * operations},
                total_latency_ms=offset+20, observed_usage={"total_tokens": 15*(len(components)-int(unknown))},
                usage_completeness=telemetry.UsageCompleteness.PARTIAL if unknown else telemetry.UsageCompleteness.COMPLETE,
                terminal_failure_component="synthesis" if status == "failed" else None,
                terminal_failure_category=Category.NETWORK_ERROR if status == "failed" else None)
    return record


def measured(**kwargs):
    raw = observation(**kwargs).snapshot()
    raw["prompt"] = SECRET
    raw["component_spans"][0]["provider_body"] = SECRET
    raw["component_spans"][0]["attempts"][0]["raw_response"] = SECRET
    return sanitize_measurement(raw, status=kwargs.get("status", "completed"), elapsed=80)


def test_baseline_qualification_executes_once_per_row_not_per_candidate(tmp_path):
    measure = Mock(side_effect=lambda *a, **kw: measured())
    cfg = config(repetitions=2)
    output = tmp_path / "reports/reliability.json"
    report = qualify(cfg, datasets(), output=output, project_root=tmp_path, measure=measure)
    assert measure.call_count == 4
    assert all(call.kwargs["execute_retries"] is False for call in measure.call_args_list)
    assert report["complete"] and len(report["observations"]) == 4
    assert report["effective_retry_policy"]["enabled"] is False
    assert report["evaluator_usage"] == {"judge_calls": 0, "tokens": 0}
    assert report["effective_models"] == ["gpt-test"]
    assert json.loads(output.read_text()) == report
    assert SECRET not in output.read_text()
    assert sum(report["summary"]["population_counts"].values()) == 4


@pytest.mark.parametrize("category,delivery,status,reason", [
    (Category.NETWORK_ERROR, Delivery.NOT_SENT, None, None),
    (Category.TIMEOUT, Delivery.NOT_SENT, None, None),
    (Category.NETWORK_ERROR, Delivery.SENT_OUTCOME_UNKNOWN, None, "DELIVERY_UNKNOWN"),
    (Category.TIMEOUT, Delivery.SENT_OUTCOME_UNKNOWN, None, "DELIVERY_UNKNOWN"),
    (Category.AUTHENTICATION_FAILURE, Delivery.KNOWN_FAILED, 401, "FAILURE_NOT_ELIGIBLE"),
    (Category.INVALID_MODEL_OUTPUT, Delivery.KNOWN_FAILED, None, "FAILURE_NOT_ELIGIBLE"),
    (Category.PROVIDER_ERROR, Delivery.KNOWN_FAILED, 501, "FAILURE_NOT_ELIGIBLE"),
    (Category.PROVIDER_ERROR, Delivery.KNOWN_FAILED, 500, None),
])
def test_shadow_eligibility(category, delivery, status, reason):
    result = shadow_retry(candidate(), FailureEvidence(category, delivery, status),
                          component="synthesis", remaining_budget_ms=100)
    assert result["shadow_retry_admitted"] == (reason is None)
    assert result["shadow_retry_denial_reason"] == reason
    assert result["retry_success_estimate"] is None and result["shadow_dispatch_count"] == 0


@pytest.mark.parametrize("remaining,reserve,guidance,reason", [
    (39, 10, None, "INSUFFICIENT_RETRY_BUDGET"),
    (100, 80, None, "INSUFFICIENT_RETRY_BUDGET"),
    (100, 10, 50, None),
    (70, 10, 50, "RETRY_AFTER_EXCEEDS_BUDGET"),
    (1000, 10, 150, "RETRY_AFTER_EXCEEDS_DELAY_LIMIT"),
    (None, 10, 50, None),
])
def test_shadow_budget_reserves_and_retry_after(remaining, reserve, guidance, reason):
    c = candidate(retry_policy=policy(downstream_reserve_ms=reserve))
    result = shadow_retry(c, FailureEvidence(Category.RATE_LIMIT, Delivery.KNOWN_FAILED, 429, guidance),
                          component="primary_router", remaining_budget_ms=remaining)
    assert result["shadow_retry_denial_reason"] == reason
    assert result["shadow_retry_admitted"] == (reason is None)
    if reason is None and remaining is not None:
        assert result["shadow_remaining_budget_ms"] == remaining - max(10, guidance or 0)
        assert result["downstream_reserve_remaining_ms"] == reserve


def test_shadow_shared_allowance_and_recovery_reserve():
    from src.agent.request_budget import RecoveryBudgetPolicy
    evidence = FailureEvidence(Category.NETWORK_ERROR, Delivery.NOT_SENT)
    c = candidate(recovery_budget_policy=RecoveryBudgetPolicy(recovery_allowance_ms=20, synthesis_reserve_ms=80))
    denied = shadow_retry(c, evidence, component="recovery_planner", remaining_budget_ms=100)
    assert denied["shadow_retry_denial_reason"] == "INSUFFICIENT_RETRY_BUDGET"
    denied = shadow_retry(c, evidence, component="synthesis", remaining_budget_ms=1000, shared_allowance_used=1)
    assert denied["shadow_retry_denial_reason"] == "SHARED_ALLOWANCE_EXHAUSTED"
    assert ModelRetryPolicy.disabled().shared_extra_attempts_per_request == 0


def test_shadow_is_pure_and_does_not_dispatch_or_sleep(monkeypatch):
    from src.agent import model_execution
    forbidden = Mock(side_effect=AssertionError("No request or sleep allowed"))
    monkeypatch.setattr(model_execution.Runner, "run_sync", forbidden)
    monkeypatch.setattr("time.sleep", forbidden)
    result = shadow_retry(candidate(), FailureEvidence(Category.NETWORK_ERROR, Delivery.NOT_SENT),
                          component="primary_router", remaining_budget_ms=100)
    assert result["shadow_retry_admitted"]
    forbidden.assert_not_called()


def test_recovery_and_workloads_and_outcomes_are_disjoint_populations(tmp_path):
    values = [measured(), measured(recovery=True, operations=2), measured(status="failed"), measured()]
    report = qualify(config(repetitions=2), datasets(), project_root=tmp_path,
                     output=tmp_path / "reports/reliability.json", source="controlled", measure=Mock(side_effect=values))
    groups = report["summary"]["populations"]
    assert set(groups) == {"functional/no_recovery/single_operation/success",
                          "safety/recovery_triggered/multiple_operations/success",
                          "functional/no_recovery/single_operation/controlled_failure",
                          "safety/no_recovery/single_operation/success"}
    assert groups["safety/recovery_triggered/multiple_operations/success"]["tokens"]["recovery_planner"]["total_tokens"]["mean"] == 15
    assert report["summary"]["recovery"]["observed_executions"] == 1
    assert report["summary"]["recovery"]["probe_recovery_executions"] == 1
    assert report["summary"]["recovery"]["remaining_budget_before_ms"]["mean"] == 980
    # Nested recovery is excluded from local policy/planning time.
    assert groups["safety/recovery_triggered/multiple_operations/success"]["latency_ms"]["policy_planning"]["mean"] == 10


def test_zero_recovery_never_becomes_zero_duration_sample(tmp_path):
    report = qualify(config(), datasets(), project_root=tmp_path, output=tmp_path / "reports/r.json",
                     measure=Mock(side_effect=lambda *a, **kw: measured()))
    assert report["summary"]["recovery"]["observed_executions"] == 0
    assert "unavailable" in report["summary"]["recovery"]["measurement_gap"]
    for population in report["summary"]["populations"].values():
        assert population["latency_ms"]["recovery_planner"]["count"] == 0


def test_unknown_usage_and_absent_telemetry_not_imputed_zero(tmp_path):
    values = [measured(unknown=True), sanitize_measurement({}, status="failed", elapsed=30)]
    report = qualify(config(), datasets(), project_root=tmp_path, output=tmp_path / "reports/r.json",
                     measure=Mock(side_effect=values))
    first, second = report["observations"]
    assert first["usage_completeness"] == "PARTIAL" and first["unknown_usage_attempt_count"] == 1
    assert first["tokens"]["primary_router"]["total_tokens"] is None
    assert first["tokens"]["production_total"]["total_tokens"] == 15
    assert second["usage_completeness"] == "UNAVAILABLE" and second["observation_incomplete"]
    assert second["tokens"]["production_total"]["total_tokens"] is None
    assert second["recovery_triggered"] is None


@pytest.mark.parametrize("count", [0, 1, 5, 10, 20, 40, 99, 100, 200])
def test_percentile_convention_and_sample_support(count):
    values = list(range(count))
    result = stats(values)
    existing = distribution(values)
    for p, minimum in ((50, 2), (90, 10), (95, 20), (99, 100)):
        assert result[f"p{p}"] == (existing[f"p{p}"] if count >= minimum else None)
    assert result["count"] == count
    if count:
        assert result["max"] == count-1 and result["mean"] == existing["mean"]


def test_deadline_analysis_is_descriptive_and_includes_exact_exhaustion(tmp_path):
    c = candidate(request_budget_ms=60)
    cfg = config(candidates=(c,), execution_candidate=c.name)
    report = qualify(cfg, datasets(), project_root=tmp_path, output=tmp_path / "reports/r.json",
                     measure=Mock(side_effect=lambda *a, **kw: measured()))
    for group in report["summary"]["populations"].values():
        assert group["deadline_analysis"][c.name]["exceedance_rate"] == 1
    assert "qualification_passed" not in report and "winning_candidate" not in report


@pytest.mark.parametrize("execute", [False, True])
def test_only_explicit_flag_enables_execution_policy(execute):
    run = Mock(return_value=SimpleNamespace(context_wrapper=SimpleNamespace(production_telemetry=observation())))
    result = measure_request({"input": SECRET}, candidate=candidate(), execute_retries=execute, run=run)
    assert run.call_count == 1
    assert run.call_args.kwargs["retry_policy"].enabled is execute
    # Candidate configuration alone is descriptive; retries do not activate deadlines.
    assert run.call_args.kwargs["request_budget"].original_budget_ms is None
    assert SECRET not in json.dumps(result)


def test_default_failure_enriches_delivery_and_retry_after_without_replay():
    record = observation(status="failed")
    record.component_spans[1].attempts[0].delivery_certainty = None
    error = RateLimitError(SECRET, response=httpx2.Response(429, headers={"retry-after": "0.05"},
                           request=httpx2.Request("POST", "https://offline.invalid")), body={"secret": SECRET})
    error.production_telemetry = record
    run = Mock(side_effect=error)
    row = measure_request({"input": SECRET}, candidate=PolicyCandidate("baseline_no_retry"), run=run)
    assert run.call_count == 1
    failure = row["attempts"][-1]
    assert failure["delivery_certainty"] == "KNOWN_FAILED" and failure["retry_after_ms"] == 50
    decisions = shadow_observation(row, [candidate()])
    assert decisions[0]["shadow_retry_admitted"]
    assert SECRET not in json.dumps(row)


def test_candidate_json_roundtrip_and_defaults_unchanged(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(config().snapshot()))
    loaded = load_qualification_config(path)
    assert loaded == config()
    assert ModelRetryPolicy().enabled is False and ModelRetryPolicy().max_attempts == 1


@pytest.mark.parametrize("defect", ["unknown_field", "bad_suite", "zero_reps", "duplicate", "no_execution", "bad_delay", "callable_jitter"])
def test_invalid_candidate_configuration_rejected(tmp_path, defect):
    raw = config().snapshot()
    if defect == "unknown_field": raw["prompt"] = SECRET
    if defect == "bad_suite": raw["dataset_suite"] = "reliability"
    if defect == "zero_reps": raw["repetitions"] = 0
    if defect == "duplicate": raw["candidates"].append(raw["candidates"][0])
    if defect == "no_execution": raw["execution_candidate"] = "unregistered"
    if defect == "bad_delay": raw["candidates"][1]["retry_policy"]["maximum_retry_delay_ms"] = -1
    if defect == "callable_jitter": raw["candidates"][1]["retry_policy"]["backoff"]["jitter"] = "arbitrary"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_qualification_config(path)


@pytest.mark.parametrize("defect", ["outside", "exists", "disabled", "unknown_probe"])
def test_setup_rejected_before_any_execution(tmp_path, defect):
    cfg = config()
    output = tmp_path / "reports/r.json"
    if defect == "outside": output = tmp_path / "r.json"
    if defect == "exists":
        output.parent.mkdir()
        output.write_text("prior evidence")
    if defect == "unknown_probe": cfg = replace(cfg, recovery_scenario_ids=("unregistered",))
    measure = Mock()
    with pytest.raises(ValueError):
        qualify(cfg, datasets(), project_root=tmp_path, output=output, measure=measure,
                execute_retries=defect == "disabled")
    measure.assert_not_called()


@pytest.mark.parametrize("args", [
    ["--suite", "reliability"],
    ["--suite", "smoke", "--execute-retries"],
    ["--suite", "full", "--qualification-config", "config.json"],
    ["--suite", "performance", "--execution-candidate", "a"],
    ["--suite", "reliability", "--qualification-config", "config.json", "--repetitions", "0"],
])
def test_cli_rejects_invalid_mode_options(args):
    with pytest.raises(SystemExit): runner.parse_args(args)


def test_reliability_cli_never_invokes_evaluators(monkeypatch, tmp_path, capsys):
    from src.agentguard import reliability
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config().snapshot()))
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_datasets", Mock(return_value=datasets()))
    measure = Mock(side_effect=lambda *a, **kw: measured())
    monkeypatch.setattr(reliability, "measure_request", measure)
    forbidden = Mock(side_effect=AssertionError("No judges or quality gates"))
    for name in ("execute_scenario", "evaluate_semantics", "safety_evaluate_record", "evaluate_record", "load_quality_gate_config"):
        monkeypatch.setattr(runner, name, forbidden)
    assert runner.main(["--suite", "reliability", "--qualification-config", str(path)]) == 0
    assert measure.call_count == 2 and not measure.call_args.kwargs["execute_retries"]
    forbidden.assert_not_called()
    text = capsys.readouterr().out
    assert "RELIABILITY QUALIFICATION" in text and "Insufficient" in text and SECRET not in text
    assert "FINAL DECISION" not in text


def test_shadow_missing_elapsed_evidence_denies_finite_candidate():
    row = measured(status="failed")
    row["attempts"][-1]["duration_ms"] = None
    result = shadow_observation(row, [candidate()])
    assert result[0]["shadow_retry_denial_reason"] == "BUDGET_EVIDENCE_UNAVAILABLE"


def test_observationwise_shadow_does_not_spend_hypothetical_allowance():
    row = measured(status="failed")
    first = row["attempts"][0]
    first.update(status="failed", failure_category="NETWORK_ERROR", delivery_certainty="NOT_SENT")
    result = shadow_observation(row, [candidate()])
    assert all(d["shadow_retry_admitted"] for d in result)
    first["retry_performed"] = True
    result = shadow_observation(row, [candidate()])
    assert result[-1]["shadow_retry_denial_reason"] == "SHARED_ALLOWANCE_EXHAUSTED"


@pytest.mark.parametrize("execute", [False, True])
def test_real_sdk_mock_transport_qualification_counts_actual_and_shadow_separately(transport, tmp_path, execute):
    from src.agent import support_agent as support
    wire = transport(failure="429", guidance_headers={"retry-after-ms": "50"})
    c = candidate(request_budget_ms=None)
    cfg = config(candidates=(c,), execution_candidate=c.name, recovery_scenario_ids=())
    sample = EvaluationDatasets([{"id": "test_request", "input": "Where is ORD-1001?"}], [])
    def measure(scenario, **kwargs):
        token = _wire.set(wire)
        try:
            return measure_request(scenario, **kwargs, run=lambda *a, **kw:
                                   support.run_support_agent_detailed(*a, **kw, retry_sleeper=lambda _: None))
        finally:
            _wire.reset(token)
    output = tmp_path / "reports/r.json"
    report = qualify(cfg, sample, project_root=tmp_path, output=output, measure=measure,
                     execute_retries=execute, source="controlled")
    row = report["observations"][0]
    assert wire.attempts == ({"primary_router": 2, "synthesis": 1} if execute else {"primary_router": 1})
    assert row["actual_extra_attempts"] == int(execute)
    assert len(row["attempts"]) == sum(wire.attempts.values())
    assert row["shadow_decisions"][0]["shadow_retry_admitted"]
    assert row["shadow_decisions"][0]["shadow_retry_delay_ms"] == 50
    assert row["usage_completeness"] == ("PARTIAL" if execute else "UNAVAILABLE")
    assert row["attempts"][0]["delivery_certainty"] == "KNOWN_FAILED"
    assert len(wire.tool_calls) == int(execute)
    assert all(h["x-stainless-retry-count"] == "0" for h in wire.headers) and not transport.sleeps
    assert PRIVATE not in output.read_text() and "Where is" not in output.read_text()


def test_interrupted_qualification_preserves_partial_evidence(tmp_path):
    output = tmp_path / "reports/r.json"
    with pytest.raises(KeyboardInterrupt):
        qualify(config(), datasets(), project_root=tmp_path, output=output,
                measure=Mock(side_effect=[measured(), KeyboardInterrupt()]))
    report = json.loads(output.read_text())
    assert not report["complete"] and len(report["observations"]) == 1
    assert report["in_progress"]["scenario_id"] == "safety_probe"


def test_missing_model_resolution_not_reported_as_effective_identity():
    raw = observation().snapshot()
    for span in raw["component_spans"]:
        span["resolved_model"] = None
    result = sanitize_measurement(raw, status="completed", elapsed=50)
    assert result["models"] == [] and result["configured_models"] == ["gpt-test"]


def test_controlled_cli_requires_explicit_enable_and_named_candidate(monkeypatch, tmp_path):
    from src.agentguard import reliability
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config().snapshot()))
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_datasets", Mock(return_value=datasets()))
    measure = Mock(side_effect=lambda *a, **kw: measured())
    monkeypatch.setattr(reliability, "measure_request", measure)
    assert runner.main(["--suite", "reliability", "--qualification-config", str(path),
                        "--execution-candidate", "candidate_A", "--execute-retries"]) == 0
    assert measure.call_count == 2
    assert all(call.kwargs["execute_retries"] is True for call in measure.call_args_list)
