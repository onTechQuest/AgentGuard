"""Lineage contracts use local synthetic data; no provider or judge execution."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess

import pytest

from src.agentguard import lineage as l
from src.agentguard import lineage_adapters as adapters
from src.agentguard.comparability import compare_runs, diff_scenarios

ROOT = Path(__file__).resolve().parents[1]


def scenario(**overrides):
    return {"id": "case_1", "input": "Synthetic input", "tier": "smoke", "expected_output": "Expected", **overrides}


def manifest(root=ROOT, **kwargs):
    return adapters.build_manifest(root, suite="smoke", functional=[scenario()], safety=[], **kwargs)


@pytest.mark.parametrize("text,format", [('{"b":2, "a":1}', "json"), ('b: 2\na: 1\n', "yaml"),
                                         ('# comment\na: 1\nb: 2\n', "yaml")])
def test_canonical_equivalence(text, format):
    assert l.fingerprint("test", l.parse_document(text, format=format)) == l.fingerprint("test", {"a": 1, "b": 2})


@pytest.mark.parametrize("a,b", [("prompt", "prompt "), ([1, 2], [2, 1]), ({"threshold": .8}, {"threshold": .9}),
                                (l.UNKNOWN, l.ABSENT), (l.ABSENT, l.DISABLED)])
def test_meaningful_changes(a, b):
    assert l.fingerprint("test", a) != l.fingerprint("test", b)


@pytest.mark.parametrize("text,format", [('{"a":1,"a":2}', "json"), ('a: 1\na: 2', "yaml"),
    ('{"v":NaN}', "json"), ('{"v":Infinity}', "json"), ('v: .nan', "yaml"), ('v: .inf', "yaml")])
def test_reject_ambiguous_documents(text, format):
    with pytest.raises(ValueError):
        l.parse_document(text, format=format)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), {1: "x"}, {"x"}, object()])
def test_reject_unsupported_content(value):
    with pytest.raises(ValueError):
        l.fingerprint("test", value)


@pytest.mark.parametrize("field,value,partition", [("input", "changed", "content"), ("risk", "high", "metadata"),
                                                   ("expected_output", "changed", "expected_behavior")])
def test_scenario_partitions(field, value, partition):
    a = l.scenario_index([scenario()])
    b = l.scenario_index([scenario(**{field: value})])
    assert diff_scenarios(a, b) == [dict(scenario_id="case_1", classification="MODIFIED", changed_partitions=[partition])]


def test_added_removed_unchanged_and_duplicate_ids():
    a = l.scenario_index([scenario(), scenario(id="removed")])
    b = l.scenario_index([scenario(), scenario(id="added")])
    assert {r["scenario_id"]: r["classification"] for r in diff_scenarios(a, b)} == {
        "case_1": "UNCHANGED", "removed": "REMOVED", "added": "ADDED"}
    with pytest.raises(ValueError):
        l.scenario_index([scenario(), scenario()])
    with pytest.raises(ValueError):
        l.scenario_identity(scenario(new_behavior=True))


def test_manifest_immutable_and_private(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET_SENTINEL")
    m, rows = manifest()
    copy = m.to_dict()
    copy["source"]["git_commit"] = "tampered"
    assert copy != m.to_dict()
    with pytest.raises(FrozenInstanceError):
        m._json = "tampered"
    run = l.RunArtifacts(tmp_path, m, rows)
    raw = (run.path / "manifest.json").read_text()
    assert "SECRET_SENTINEL" not in raw and "Synthetic input" not in raw and "Expected" not in raw
    assert l.load_run(run.path)["completion_state"] == "INCOMPLETE"
    run.observe("case_1")
    run.finish()
    loaded = l.load_run(run.path)
    assert loaded["completion_state"] == "COMPLETED"
    assert loaded["results"]["manifest_digest"] == m.digest
    with pytest.raises(FileExistsError):
        run.finish()
    with pytest.raises(FileExistsError):
        l.RunArtifacts(tmp_path, m, rows)


@pytest.mark.parametrize("filename", ["manifest.json", "scenarios.json", "results.json", "completion.json"])
def test_tampering_detected(tmp_path, filename):
    m, rows = manifest()
    run = l.RunArtifacts(tmp_path, m, rows)
    run.finish()
    path = run.path / filename
    doc = json.loads(path.read_text())
    if filename == "manifest.json":
        doc["suite"] = "full"
    elif filename == "scenarios.json":
        doc["scenarios"][0]["content_fingerprint"] = "changed"
    elif filename == "results.json":
        doc["aggregate_results"] = {"changed": True}
    else:
        doc["manifest_digest"] = "changed"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        l.load_run(run.path)


def test_git_identity_dirty_and_missing(tmp_path):
    assert l.source_identity(tmp_path)["git_commit"] == l.UNKNOWN
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "new.py").write_text("x = 1")
    identity = l.source_identity(tmp_path)
    assert identity["dirty_state"] == "DIRTY"
    assert identity["source_fingerprint"] != l.UNKNOWN


def test_missing_library_is_unknown(monkeypatch):
    def missing(name):
        raise adapters.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(adapters.metadata, "version", missing)
    assert adapters.library_versions()["deepeval"] == l.UNKNOWN


def test_actual_contract_coverage():
    contracts = adapters.runtime_contracts()
    assert set(contracts["prompts"]) == {"router", "omitted_work_recovery", "actionability_recovery", "synthesis"}
    assert {t["name"] for t in contracts["tools"]["tools"]} == {"get_order_status", "check_return_eligibility"}
    assert contracts["business_constants"] == {"evaluation_date": "2026-09-10", "return_window_days": 30}
    m, _ = manifest()
    assert "runtime_contracts" not in m.to_dict()["unavailable_fields"]
    assert m.to_dict()["datasets"]["functional"]["full"]["count"] == 38


@pytest.mark.parametrize("path,key", [("data/orders.json", "business_fixtures"),
    ("src/agent/support_agent.py", "prompt_bundle"), ("src/agent/request_policy.py", "tool_contract"),
    ("src/agent/data_policy.py", "tool_contract"), ("src/agent/runtime_reliability.py", "runtime_policy"),
    ("src/agentguard/semantic_evaluator.py", "evaluator_prompts"), ("config/release-gates.yaml", "release_gates")])
def test_source_dependency_coverage(tmp_path, path, key):
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text('{"value": 1}' if target.suffix in {".json", ".yaml"} else "VALUE = 1\n")
    before, _ = manifest(tmp_path)
    target.write_text('{"value": 2}' if target.suffix in {".json", ".yaml"} else "VALUE = 2\n")
    after, _ = manifest(tmp_path)
    assert before.to_dict()["fingerprints"][key] != after.to_dict()["fingerprints"][key]


def test_tool_schema_observation(monkeypatch):
    from src.agent.support_agent import BUSINESS_TOOLS
    tool = BUSINESS_TOOLS["get_order_status"]
    before, _ = manifest()
    schema = deepcopy(tool.params_json_schema)
    schema["description"] = "changed contract"
    monkeypatch.setattr(tool, "params_json_schema", schema)
    after, _ = manifest()
    assert before.to_dict()["fingerprints"]["tool_contract"] != after.to_dict()["fingerprints"]["tool_contract"]


def comparison_pair():
    m, rows = manifest()
    doc = m.to_dict()
    for judge in doc["judge_models"].values():
        judge.update(resolved="fixed-judge", provider="test", resolution_provenance="fixture")
    doc["libraries"] = {k: "test-version" if v == l.UNKNOWN else v for k, v in doc["libraries"].items()}
    run = dict(manifest=doc, scenarios=rows)
    return run, deepcopy(run)


@pytest.mark.parametrize("key", ["prompt_bundle", "tool_contract", "business_fixtures"])
def test_application_change_directly_comparable(key):
    a, b = comparison_pair()
    b["manifest"]["fingerprints"][key] = "changed"
    assert compare_runs(a, b)["classification"] == "DIRECTLY_COMPARABLE"


def test_model_and_code_change_are_experiment_variables():
    a, b = comparison_pair()
    b["manifest"]["production_models"]["router"]["resolved"] = "different-model"
    b["manifest"]["source"]["git_commit"] = "different-code"
    result = compare_runs(a, b)
    assert result["classification"] == "DIRECTLY_COMPARABLE"
    assert {"PRODUCTION_MODEL_CHANGED", "CODE_CHANGED"} <= set(result["reason_codes"])


def test_judge_change_preserves_deterministic_metrics():
    a, b = comparison_pair()
    b["manifest"]["judge_models"]["correctness"]["resolved"] = "new-judge"
    result = compare_runs(a, b)
    assert result["classification"] == "PARTIALLY_COMPARABLE"
    assert "correctness" not in result["eligible_metrics"]
    assert "functional_accuracy" in result["eligible_metrics"]
    assert "JUDGE_CHANGED" in result["reason_codes"]
    assert not result["gate_decisions_comparable"]


def test_gate_only_change():
    a, b = comparison_pair()
    b["manifest"]["fingerprints"]["release_gates"] = "changed"
    result = compare_runs(a, b)
    assert result["classification"] == "DIRECTLY_COMPARABLE"
    assert not result["gate_decisions_comparable"]


def test_subset_and_modified_scenario():
    a, b = comparison_pair()
    b["scenarios"].append(l.scenario_identity(scenario(id="added")))
    result = compare_runs(a, b)
    assert result["classification"] == "PARTIALLY_COMPARABLE"
    assert result["eligible_scenario_ids"] == ["case_1"]
    b["scenarios"][0] = l.scenario_identity(scenario(input="changed"))
    assert compare_runs(a, b)["classification"] == "NOT_COMPARABLE"


def test_unknown_judge_not_equivalent():
    m, rows = manifest()
    run = dict(manifest=m.to_dict(), scenarios=rows)
    result = compare_runs(run, run)
    assert result["classification"] == "PARTIALLY_COMPARABLE"
    assert "JUDGE_IDENTITY_UNKNOWN" in result["reason_codes"]


def test_hosted_latency_and_fixture_modes():
    a, b = comparison_pair()
    b["manifest"]["hosting"]["mode"] = "bounded"
    result = compare_runs(a, b)
    assert result["classification"] == "PARTIALLY_COMPARABLE" and "latency" not in result["eligible_metrics"]
    b["manifest"]["execution_mode"] = "offline_fixture"
    assert compare_runs(a, b)["classification"] == "NOT_COMPARABLE"


@pytest.mark.parametrize("report,status", [({"suite": "smoke", "sdk_versions": {"openai": "old"}}, "PARTIAL_LINEAGE"),
                                          ({"scores": []}, "LEGACY_UNVERSIONED")])
def test_legacy_read_only(tmp_path, report, status):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(report))
    original = path.read_bytes()
    result = l.read_legacy(path)
    assert result["lineage_status"] == status and result["provenance"]["git_commit"] == l.UNKNOWN
    assert path.read_bytes() == original


def test_interrupted_run_preserves_identity(tmp_path):
    @l.lineage_entry
    def execute():
        run = l.start_run(tmp_path, suite="smoke", functional=[scenario()])
        run.observe("case_1", completed=False)
        raise RuntimeError("sensitive exception body")
    with pytest.raises(RuntimeError):
        execute()
    directory = next((tmp_path / "reports/evaluations").iterdir())
    loaded = l.load_run(directory)
    assert loaded["completion_state"] == "INCOMPLETE"
    assert loaded["results"]["executed_scenario_count"] == 1
    assert loaded["results"]["completed_scenario_count"] == 0
    assert "sensitive exception body" not in (directory / "results.json").read_text()


def test_checked_in_schemas(tmp_path):
    import jsonschema
    m, rows = manifest()
    jsonschema.validate(m.to_dict(), l.read_document(ROOT / "config/evaluation-run-manifest.schema.json"))
    run = l.RunArtifacts(tmp_path, m, rows)
    run.finish()
    jsonschema.validate(l.read_document(run.path / "results.json"), l.read_document(ROOT / "config/evaluation-result.schema.json"))


def test_model_settings_secret_not_even_hashed(monkeypatch):
    from src.agent.support_agent import support_agent
    before, _ = manifest()
    monkeypatch.setattr(support_agent.model_settings, "extra_headers", {"Authorization": "SECRET_SENTINEL"})
    after, _ = manifest()
    assert before.to_dict()["fingerprints"] == after.to_dict()["fingerprints"]
    assert "SECRET_SENTINEL" not in after._json


def test_configuration_credentials_not_even_hashed():
    before, _ = manifest(evaluation_config={"qualification": {"enabled": False}})
    after, _ = manifest(evaluation_config={"api_key": "SECRET_SENTINEL", "qualification": {
        "enabled": False, "headers": {"Authorization": "SECRET_SENTINEL"}}})
    assert before.to_dict()["fingerprints"] == after.to_dict()["fingerprints"]


def test_dataset_set_and_execution_order_are_separate():
    a, b = scenario(), scenario(id="case_2")
    assert l.dataset_identity("functional", [a, b]) == l.dataset_identity("functional", [b, a])
    first, _ = adapters.build_manifest(ROOT, suite="smoke", functional=[a, b], safety=[])
    second, _ = adapters.build_manifest(ROOT, suite="smoke", functional=[b, a], safety=[])
    assert first.to_dict()["scenario_set_fingerprint"] == second.to_dict()["scenario_set_fingerprint"]
    assert first.to_dict()["execution_order_fingerprint"] != second.to_dict()["execution_order_fingerprint"]


def test_missing_critical_identity_and_incomplete_run():
    a, b = comparison_pair()
    a["manifest"]["production_models"]["router"]["resolved"] = l.UNKNOWN
    assert compare_runs(a, b)["classification"] == "PARTIALLY_COMPARABLE"
    a["completion_state"] = "INCOMPLETE"
    assert compare_runs(a, b)["classification"] == "NOT_COMPARABLE"


def test_effective_qualification_policy_not_production_default():
    from src.agent.runtime_reliability import RuntimeReliabilityPolicy
    normal, _ = manifest()
    qualification, _ = manifest(effective_runtime_policy=RuntimeReliabilityPolicy.unbounded().snapshot())
    assert normal.to_dict()["fingerprints"]["runtime_policy"] != qualification.to_dict()["fingerprints"]["runtime_policy"]


def test_record_incident_results_share_one_id_without_extra_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agents.usage import Usage
    from src.agentguard import evaluation_record
    from src.agentguard.safety_incidents import SafetyIncidentRecorder
    model_run = Mock(return_value=SimpleNamespace(final_output="CUSTOMER_RESPONSE_SENTINEL", new_items=[],
        context_wrapper=SimpleNamespace(usage=Usage())))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", model_run)
    @l.lineage_entry
    def execute():
        run = l.start_run(tmp_path, suite="smoke", functional=[scenario(input="CUSTOMER_INPUT_SENTINEL")])
        recorder = SafetyIncidentRecorder(tmp_path, retain_all=True)
        record = evaluation_record.execute_scenario(scenario(input="CUSTOMER_INPUT_SENTINEL"))
        assert record.run_id == recorder.run_id == run.manifest.run_id
        recorder.retain(scenario(), record)
        run.evaluation_complete = True
        return run.path, recorder.run_id
    path, run_id = execute()
    model_run.assert_called_once_with("CUSTOMER_INPUT_SENTINEL")
    loaded = l.load_run(path)
    assert loaded["manifest"]["scenario_count"] == loaded["results"]["executed_scenario_count"] == 1
    assert loaded["results"]["run_id"] == run_id
    incident = l.read_document(next((tmp_path / "reports/safety_incidents" / run_id).glob("*.json")))
    assert incident["run_id"] == run_id
    for file in path.glob("*.json"):
        text = file.read_text()
        assert "CUSTOMER_RESPONSE_SENTINEL" not in text and "CUSTOMER_INPUT_SENTINEL" not in text


def test_performance_real_profiler_records_once_per_attempt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agents.usage import Usage
    from scripts import audit_latency
    from src.agentguard import evaluation_record
    monkeypatch.setattr(audit_latency, "load_dataset", lambda *a, **k: [scenario()])
    monkeypatch.setattr(audit_latency, "load_quality_gate_config", lambda *a: {"quality_gates": {"p95_latency_ms": {"maximum": 7500}}})
    model_run = Mock(return_value=SimpleNamespace(final_output="Synthetic answer", new_items=[],
        context_wrapper=SimpleNamespace(usage=Usage())))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", model_run)
    audit_latency.main(["--repetitions", "2", "--output", str(tmp_path / "performance.json")], project_root=tmp_path)
    assert model_run.call_count == 2
    loaded = l.load_run(next((tmp_path / "reports/evaluations").iterdir()))
    assert loaded["manifest"]["planned_execution_count"] == loaded["results"]["executed_scenario_count"] == 2


def test_release_assembly_links_correctness_run(tmp_path):
    import shutil
    from scripts.qualify_release import assemble_release
    (tmp_path / "config").mkdir()
    for name in ("release-gates.yaml", "quality-gates.yaml", "runtime-reliability.json"):
        shutil.copyfile(ROOT / "config" / name, tmp_path / "config" / name)
    from unittest.mock import patch
    source = {"run_id": "a" * 32, "manifest_digest": "b" * 64}
    with patch("scripts.qualify_release.evidence_identity", return_value={"git_commit": "fixture", "source_fingerprint": "fixture", "sdk_versions": {}, "timestamp": "fixture"}):
        report = assemble_release(tmp_path, structural_path=tmp_path / "absent.json",
            output=tmp_path / "qualification.json", evaluation_lineage=source)
    loaded = l.load_run(tmp_path / "reports/evaluations" / report["run_id"])
    assert report["evaluation_lineage"] == source
    assert loaded["results"]["aggregate_results"]["evaluation_lineage"] == source
    assert report["decision"] == "INSUFFICIENT_EVIDENCE"
    assert loaded["completion_state"] == "COMPLETED"
