"""M16 uses real artifact contracts and synthetic retained scores, never providers."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_baseline_comparison import factory, template, rewrite
from src.agentguard import continuous_comparison as cc
from src.agentguard.lineage import read_document
from src.agentguard.metric_registry import aggregate, METRICS


def identities(manifest):
    for model in manifest["production_models"].values():
        model.update(resolved="fixture-model-v1", provider="fixture", immutable_revision="fixture-revision-1")
    for model in manifest["judge_models"].values():
        model.update(resolved="fixture-judge-v1", provider="fixture", resolution_provenance="offline_fixture", immutable_revision="fixture-judge-revision-1")
    manifest["execution_mode"] = manifest["protocol"]["execution_mode"] = "offline_fixture"
    manifest["unavailable_fields"] = []


def pair(factory, change=None, **options):
    left, _ = factory(modify=identities)
    def modify(m):
        identities(m)
        if change: change(m)
    right, _ = factory(modify=modify, **options)
    return left, right


def compare(factory, left, right, **options):
    return cc.compare_model_prompt_runs(factory.root, baseline_run_id=left.manifest.run_id, candidate_run_id=right.manifest.run_id, **options)


def metric(report, name="functional_accuracy", population="functional"):
    return next(m for m in report["metric_comparisons"] if m["metric_name"] == name and m["population"] == population)


def scores(run, change):
    def update(result):
        change(result)
        result["continuous_aggregates"] = aggregate(result["observations"], population_complete=result["completion_state"] == "COMPLETED")
    rewrite(run, update)


@pytest.mark.parametrize("kind,expected", [("prompt", "PROMPT_CHANGE"), ("model", "MODEL_CHANGE"), ("both", "MODEL_AND_PROMPT_CHANGE"), ("none", "NO_MODEL_PROMPT_CHANGE"), ("unknown", "UNKNOWN")])
def test_change_detection(factory, kind, expected):
    def change(m):
        if kind in {"prompt", "both"}: m["fingerprints"]["prompt_bundle"] = "d" * 64
        if kind in {"model", "both"}: m["production_models"]["synthesis"]["resolved"] = "fixture-model-v2"
        if kind == "unknown": m["production_models"]["router"]["resolved"] = "UNKNOWN"
    left, right = pair(factory, change)
    report = compare(factory, left, right, baseline_label="model-A", candidate_label="prompt-B")
    assert report["variant_identity"]["change_type"] == expected
    assert report["comparability"]["quality"] == "COMPARABLE"
    assert metric(report)["comparison_status"] == "UNCHANGED"


def test_regressions_directions_and_gate_failure(factory):
    left, right = pair(factory, passed=False, score=False)
    def change(result):
        for row in result["observations"]:
            row["semantic"]["correctness"]["score"] = .5
            row["safety"]["final_pass"] = False
            row["operational"].update(total_latency_ms=80, production_tokens=8, retry_count=1)
        result["aggregate_results"]["quality_result"]["checks"] = [dict(metric="functional_accuracy", actual=0, threshold=1, comparison=">=", passed=False, reason="PRIVATE_REASON")]
    scores(right, change)
    report = compare(factory, left, right)
    assert metric(report)["comparison_status"] == "REGRESSED"
    assert metric(report, "correctness")["comparison_status"] == "REGRESSED"
    assert metric(report, "safety_pass_rate", "safety")["comparison_status"] == "REGRESSED"
    assert metric(report, "total_latency_ms")["comparison_status"] == "IMPROVED"
    assert metric(report, "retry_count")["comparison_status"] == "REGRESSED"
    assert metric(report, "retry_count")["relative_delta"] is None
    tokens = metric(report, "production_tokens")
    assert tokens["absolute_delta"] == -4 and tokens["relative_delta"] == pytest.approx(-1/3)
    assert tokens["direction"] == "INFORMATIONAL" and tokens["comparison_status"] == "UNAVAILABLE"
    assert report["candidate_gate_decision"] == "FAIL"
    assert report["gate_context"]["candidate"]["failed_gate_names"] == ["functional_accuracy"]
    assert "PRIVATE_REASON" not in json.dumps(report)
    assert report["performance_qualification"] is False


def test_scenario_transitions(factory):
    left, right = pair(factory)
    def before(r):
        r["observations"][1]["deterministic"]["functional_pass"] = False
        r["observations"][2]["deterministic"]["functional_pass"] = False
    def after(r):
        r["observations"][0]["deterministic"]["functional_pass"] = False
        r["observations"][2]["deterministic"]["functional_pass"] = False
    scores(left, before); scores(right, after)
    result = compare(factory, left, right)["scenario_regressions"]
    assert all(len(result[name]) == 1 for name in ("new_failures", "resolved_failures", "unchanged_failures"))
    assert all(result[name][0]["repetition"] == 1 for name in ("new_failures", "resolved_failures", "unchanged_failures"))


@pytest.mark.parametrize("kind", ["judge", "unknown_judge", "dataset", "protocol", "evaluator", "profile", "tool", "policy", "metric_definition"])
def test_incompatibility(factory, kind):
    def change(m):
        if kind in {"judge", "unknown_judge"}: m["judge_models"]["correctness"]["resolved"] = "UNKNOWN" if kind == "unknown_judge" else "fixture-judge-v2"
        if kind == "dataset": m["datasets"]["functional"]["full"]["fingerprint"] = "e" * 64
        if kind == "protocol": m["protocol"]["repetitions"] = 2
        if kind in {"evaluator", "profile", "tool", "policy", "metric_definition"}:
            key = {"evaluator":"deterministic_evaluator", "profile":"runtime_policy", "tool":"tool_contract", "policy":"safety_policy", "metric_definition":"aggregation"}[kind]
            m["fingerprints"][key] = "e" * 64
    left, right = pair(factory, change)
    report = compare(factory, left, right)
    name = "correctness" if kind in {"judge", "unknown_judge"} else "total_latency_ms" if kind == "profile" else "functional_accuracy"
    assert metric(report, name)["comparison_status"] == "NOT_COMPARABLE"
    assert metric(report, name)["absolute_delta"] is None
    if kind in {"judge", "unknown_judge", "profile"}: assert report["comparability"]["quality"] == "COMPARABLE"
    if kind in {"dataset", "protocol"}: assert not report["scenario_regressions"]["available"]


def test_unknown_judge_never_equals_unknown(factory):
    def change(m):
        identities(m)
        for j in m["judge_models"].values(): j["resolved"] = "UNKNOWN"
    left, _ = factory(modify=change)
    right, _ = factory(modify=change)
    report = compare(factory, left, right)
    assert report["comparability"]["semantic"] == report["comparability"]["safety"] == "NOT_COMPARABLE"


@pytest.mark.parametrize("state", ["CHANGED", "UNKNOWN"])
def test_source_integrity(factory, state):
    left, right = pair(factory)
    def change(r):
        integrity = r["source_integrity"]
        integrity["end_source"]["source_fingerprint"] = "UNKNOWN" if state == "UNKNOWN" else "e" * 64
        integrity.update(state=state, end_fingerprint=integrity["end_source"]["source_fingerprint"])
    rewrite(right, change)
    report = compare(factory, left, right)
    assert all(report["comparability"][key] == "NOT_COMPARABLE" for key in ("quality", "semantic", "safety", "operational"))


def test_incomplete_and_tamper_rejected(factory):
    left, right = pair(factory, complete=False)
    with pytest.raises(ValueError, match="RUN_INCOMPLETE"): compare(factory, left, right)
    (right.path / "results.json").write_text('{}')
    with pytest.raises(ValueError): compare(factory, left, right)


def test_legacy_readable_without_claims(factory):
    left, right = pair(factory)
    rewrite(right, lambda r:[r.pop(k) for k in ("observations", "observation_contract_version", "continuous_aggregates", "metric_registry_version", "protocol")])
    report = compare(factory, left, right)
    assert report["comparability"]["quality"] == "NOT_COMPARABLE"
    assert report["candidate_gate_decision"] == "PASS"


def test_deterministic_id_immutable_sources_and_reuse(factory, monkeypatch):
    left, right = pair(factory)
    before = {str(p):p.read_bytes() for run in (left,right) for p in run.path.iterdir()}
    cohort, old = Mock(wraps=cc.cohort_metrics), Mock(wraps=cc.compare_runs)
    monkeypatch.setattr(cc, "cohort_metrics", cohort); monkeypatch.setattr(cc, "compare_runs", old)
    first = compare(factory, left, right, baseline_label="v1")
    second = compare(factory, left, right, baseline_label="another-label")
    assert first["comparison_id"] == second["comparison_id"]
    assert first["metric_comparisons"] == second["metric_comparisons"]
    assert cohort.call_count == old.call_count == 2
    path = cc.write_model_prompt_comparison(factory.root, first)
    content = path.read_bytes()
    assert cc.write_model_prompt_comparison(factory.root, second) == path and path.read_bytes() == content
    assert {str(p):p.read_bytes() for run in (left,right) for p in run.path.iterdir()} == before
    definitions = {m["metric_id"]:m for m in METRICS}
    assert all(row["direction"] == definitions[row["metric_name"]]["direction"] for row in first["metric_comparisons"])


@pytest.mark.parametrize("key", ["prompt", "response", "input", "tool_output", "tool_payload", "credentials", "api_key", "headers", "environment", "customer_id", "exception_message"])
def test_privacy(factory, key):
    left, right = pair(factory)
    sentinel = "PRIVATE_SENTINEL_" + key
    rewrite(right, lambda r:r["aggregate_results"].update({key:sentinel}))
    report = compare(factory, left, right)
    path = cc.write_model_prompt_comparison(factory.root, report)
    assert sentinel not in path.read_text()


@pytest.mark.parametrize("label", ["sk-privatekey12345", "CUST-private", "Bearer credential", "../../unsafe", "password=private"])
def test_unsafe_labels(factory, label):
    left, right = pair(factory)
    with pytest.raises(ValueError): compare(factory, left, right, baseline_label=label)


def test_execution_profile_blocks_only_operational(factory):
    profile = dict(read_document(Path(__file__).resolve().parents[1] / "config/correctness-execution.json"), retries_enabled=False)
    def base(m):
        identities(m)
        m["protocol"]["execution_profile"] = deepcopy(profile)
    left, _ = factory(modify=base)
    def candidate(m):
        base(m)
        m["protocol"]["execution_profile"]["synthesis_allowance_ms"] += 1
    right, _ = factory(modify=candidate)
    report = compare(factory, left, right)
    assert report["comparability"]["quality"] == "COMPARABLE"
    assert report["comparability"]["operational"] == "NOT_COMPARABLE"


def test_operational_regression_and_improvement(factory):
    left, right = pair(factory)
    scores(left, lambda r:[o["operational"].update(retry_count=1) for o in r["observations"]])
    scores(right, lambda r:[o["operational"].update(total_latency_ms=200) for o in r["observations"]])
    report = compare(factory, left, right)
    assert metric(report, "total_latency_ms")["comparison_status"] == "REGRESSED"
    assert metric(report, "retry_count")["comparison_status"] == "IMPROVED"


def test_zero_calls_and_no_source_mutation(factory, monkeypatch):
    left, right = pair(factory)
    import agents
    from src.agentguard import evaluation_record, lineage_adapters
    from src.agentguard import semantic_evaluator, safety_evaluator
    forbidden = Mock(side_effect=AssertionError("No execution during comparison"))
    monkeypatch.setattr(agents.Runner, "run", forbidden)
    monkeypatch.setattr(agents.Runner, "run_sync", forbidden)
    monkeypatch.setattr(evaluation_record, "execute_scenario", forbidden)
    monkeypatch.setattr(lineage_adapters, "runtime_contracts", forbidden)
    # Guard evaluator entry points without constructing an evaluator or SDK client.
    for module in (semantic_evaluator, safety_evaluator):
        for name in ("evaluate_semantics", "safety_evaluate_record", "create_prompt_injection_classifier"):
            if hasattr(module, name): monkeypatch.setattr(module, name, forbidden)
    from src.agent.support_agent import BUSINESS_TOOLS
    for tool in BUSINESS_TOOLS.values(): monkeypatch.setattr(tool, "on_invoke_tool", forbidden)
    before = {str(p):p.read_bytes() for run in (left, right) for p in run.path.iterdir()}
    cc.write_model_prompt_comparison(factory.root, compare(factory, left, right))
    forbidden.assert_not_called()
    assert before == {str(p):p.read_bytes() for run in (left, right) for p in run.path.iterdir()}


def test_display_label_cannot_echo_raw_content(factory):
    left, right = pair(factory)
    rewrite(right, lambda r:r["aggregate_results"].update(prompt="PRIVATE_SENTINEL"))
    with pytest.raises(ValueError, match="UNSAFE_DISPLAY_LABEL"):
        compare(factory, left, right, candidate_label="PRIVATE_SENTINEL")


def test_unknown_prompt_and_revision_handling(factory):
    left, right = pair(factory, lambda m:m["fingerprints"].update(prompt_bundle="UNKNOWN"))
    assert compare(factory, left, right)["variant_identity"]["change_type"] == "UNKNOWN"


def test_modified_scenario_blocks_regression_claim(factory):
    left, _ = factory(modify=identities)
    functional = deepcopy(factory.datasets.functional)
    functional[0]["expected_output"] = "Different benchmark expectation"
    right, _ = factory(modify=identities, functional=functional, score=False)
    report = compare(factory, left, right)
    assert report["comparability"]["quality"] == "NOT_COMPARABLE"
    assert not report["scenario_regressions"]["new_failures"]


def test_missing_metric_denominator_and_unknown_model_version(factory):
    left, right = pair(factory, lambda m:[v.update(immutable_revision="UNKNOWN") for v in m["production_models"].values()], missing_metric=True)
    report = compare(factory, left, right)
    assert report["variant_identity"]["change_type"] == "UNKNOWN"
    assert metric(report)["comparison_status"] == "NOT_COMPARABLE"
    assert "PRODUCTION_REVISION_UNKNOWN" in report["comparability"]["limitations"]


def test_checked_in_demo_and_cli(tmp_path, monkeypatch, capsys):
    import shutil
    from scripts import compare_model_prompt_runs as cli
    source = Path(__file__).parent / "fixtures/model_prompt_runs"
    for run_id in ("16000000000000000000000000000001", "16000000000000000000000000000002"):
        shutil.copytree(source / run_id, tmp_path / "reports/evaluations" / run_id)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["compare_model_prompt_runs.py", "--baseline-run", "16000000000000000000000000000001", "--candidate-run", "16000000000000000000000000000002"])
    cli.main()
    output = capsys.readouterr().out
    assert "MODEL_AND_PROMPT_CHANGE" in output and "REGRESSED" in output and "IMPROVED" in output
    assert "fixture-model-v2" in output
    path = next((tmp_path / "reports/model_prompt_comparisons").glob("*/comparison.json"))
    report = read_document(path)
    assert report["comparability"]["quality"] == "COMPARABLE"
    assert metric(report, "production_tokens")["absolute_delta"] == -4


def test_comparison_artifact_rejects_tampering(factory):
    left, right = pair(factory)
    report = compare(factory, left, right)
    report["comparison_id"] = "a" * 32
    with pytest.raises(ValueError, match="COMPARISON_ID_MISMATCH"):
        cc.write_model_prompt_comparison(factory.root, report)
