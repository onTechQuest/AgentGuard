"""Governance and advisory comparison use local fixtures, never model execution."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agentguard import baselines as b, lineage as l
from src.agentguard.lineage_adapters import build_manifest
from src.agentguard.datasets import load_datasets
from src.agentguard.invocation import Invocation, current_invocation
from src.agentguard.metric_registry import aggregate
from src.agentguard.continuous_comparison import compare, write_comparison

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def template():
    datasets = load_datasets(ROOT / "evals/datasets", suite="smoke")
    manifest, _ = build_manifest(ROOT, suite="smoke", functional=datasets.functional, safety=datasets.safety)
    m = manifest.to_dict()
    m["source"] = dict(git_commit="a" * 40, source_fingerprint="b" * 64, dirty_state="CLEAN")
    m["libraries"] = {key: "fixture-v1" for key in m["libraries"]}
    m["unavailable_fields"] = [key for key in m["unavailable_fields"] if key.startswith(("judge_models.", "production_models."))]
    return m, datasets


@pytest.fixture
def factory(tmp_path, template):
    m, datasets = template
    (tmp_path / "evals/datasets").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    for kind in ("functional", "safety"):
        (tmp_path / f"evals/datasets/{kind}.json").write_bytes((ROOT / f"evals/datasets/{kind}.json").read_bytes())
    (tmp_path / "config/baseline-promotion.json").write_bytes((ROOT / "config/baseline-promotion.json").read_bytes())

    def create(*, modify=None, functional=None, safety=None, complete=True, passed=True, code=0,
               observed=None, score=True, missing_metric=False):
        from uuid import uuid4
        manifest = deepcopy(m)
        manifest["run_id"] = uuid4().hex
        functional = deepcopy(datasets.functional if functional is None else functional)
        safety = deepcopy(datasets.safety if safety is None else safety)
        rows = l.scenario_index([*functional, *safety])
        manifest["scenario_count"] = len(rows)
        manifest["protocol"]["scenario_populations"] = {**{s["id"]: "functional" for s in functional}, **{s["id"]: "safety" for s in safety}}
        manifest["scenario_set_fingerprint"] = l.fingerprint("scenario-set", sorted(rows, key=lambda r:r["scenario_id"]))
        for kind, scenarios in (("functional", functional), ("safety", safety)):
            manifest["datasets"][kind]["selected"] = l.dataset_identity(kind, scenarios)
        if modify:
            modify(manifest)
        reps = manifest["protocol"]["repetitions"]
        manifest["planned_execution_count"] = len(rows) * reps
        manifest["execution_order_fingerprint"] = l.fingerprint("execution-order", [r["scenario_id"] for r in rows] * reps)
        manifest = l.EvaluationRunManifest.create(manifest)
        run = l.RunArtifacts(tmp_path, manifest, rows)
        source = manifest.to_dict()["source"]
        run.source_integrity = lambda: dict(start_fingerprint=source["source_fingerprint"], end_fingerprint=source["source_fingerprint"],
            state="UNKNOWN" if "UNKNOWN" in source.values() else "MATCH", start_source=source, end_source=source)
        for rep in range(reps):
            for index, row in enumerate(rows):
                if observed is not None and index >= observed:
                    break
                run.observe(row["scenario_id"])
                o = run.observations[-1]
                o["deterministic"].update(functional_pass=score, tool_pass=True, argument_pass=True, factual_grounding_pass=True)
                o["safety"]["final_pass"] = True
                for metric in o["semantic"].values():
                    metric.update(score=.8, evaluator_available=True)
                o["operational"].update(request_id="request-" + str(index), total_latency_ms=100,
                    synthesis_latency_ms=60, production_tokens=12, retry_count=0)
                if missing_metric and index == 0:
                    o["deterministic"]["functional_pass"] = None
        run.aggregate = dict(quality_result=dict(passed=passed, failures=["PRIVATE_RESPONSE"], api_key="SECRET_SENTINEL"))
        run.finish(state="COMPLETED" if complete else "INCOMPLETE")
        receipt = Invocation(tmp_path, "smoke", manifest.to_dict()["execution_mode"], False)
        receipt.link(run)
        receipt.save(process_state="COMPLETED" if code == 0 else "FAILED", process_exit_code=code)
        return run, receipt
    create.root = tmp_path
    create.datasets = datasets
    return create


def promote(factory, run, **kwargs):
    return b.promote(factory.root, suite="smoke", run_id=run.manifest.run_id, reason="Reviewed benchmark", **kwargs)


def rewrite(run, change):
    path = run.path / "results.json"
    result = l.read_document(path)
    change(result)
    path.write_text(l.canonical_json(result))
    completion = l.read_document(run.path / "completion.json")
    completion["results_digest"] = l.fingerprint("evaluation-results", result)
    completion["source_integrity"] = result["source_integrity"]
    (run.path / "completion.json").write_text(l.canonical_json(completion))


def test_valid_promotion_immutable_private_and_previous(factory):
    run, receipt = factory()
    before = {p.name:p.read_bytes() for p in run.path.iterdir()}
    first = promote(factory, run)
    assert first["source_invocation_id"] == receipt.document["invocation_id"]
    assert first["lineage_status"] == "PARTIAL_LINEAGE" and first["lineage_limitations"]
    resolved = b.resolve_baseline(factory.root, "smoke")
    assert resolved["descriptor"] == first
    text = json.dumps(resolved)
    assert "PRIVATE_RESPONSE" not in text and "SECRET_SENTINEL" not in text and "api_key" not in text
    assert {p.name:p.read_bytes() for p in run.path.iterdir()} == before
    second = promote(factory, run)
    assert second["previous_baseline"] == {k:first[k] for k in ("baseline_id", "source_run_id", "snapshot_digest")}
    assert (factory.root / "baselines/smoke" / first["snapshot_path"]).exists()
    assert not (factory.root / "baselines/smoke/.promotion.lock").exists()
    assert first["results_digest"] != first["snapshot_digest"]


@pytest.mark.parametrize("kind", ["incomplete", "quality", "process", "dirty", "changed", "unknown", "digest", "population", "protocol", "duplicate", "legacy", "missing", "release"])
def test_promotion_rejections(factory, kind):
    options = {}
    if kind == "incomplete": options.update(complete=False, observed=1)
    if kind == "quality": options["passed"] = False
    if kind == "process": options["code"] = 1
    if kind == "dirty": options["modify"] = lambda m:m["source"].update(dirty_state="DIRTY")
    if kind == "unknown": options["modify"] = lambda m:m["source"].update(git_commit="UNKNOWN")
    if kind == "protocol": options["modify"] = lambda m:m["protocol"].update(repetitions=2)
    if kind == "population": options["functional"] = factory.datasets.functional[:-1]
    run, receipt = factory(**options)
    if kind == "changed":
        def change(r):
            r["source_integrity"]["end_source"]["source_fingerprint"] = "c" * 64
            r["source_integrity"].update(end_fingerprint="c" * 64, state="CHANGED")
        rewrite(run, change)
    if kind == "digest":
        (run.path / "results.json").write_text((run.path / "results.json").read_text().replace('"passed":true', '"passed":false'))
    if kind == "legacy":
        rewrite(run, lambda r:[r.pop(k) for k in ("observations", "observation_contract_version", "continuous_aggregates", "metric_registry_version", "protocol")])
    if kind == "missing": rewrite(run, lambda r:r["observations"].pop())
    if kind == "release": receipt.save(requested_release_qualification=True)
    bid = None
    if kind == "duplicate": bid = promote(factory, run)["baseline_id"]
    with pytest.raises((ValueError, FileExistsError)):
        promote(factory, run, baseline_id=bid)


@pytest.mark.parametrize("kind", ["descriptor", "snapshot", "digest", "suite", "path", "version", "internal"])
def test_resolver_tamper(factory, kind):
    run, _ = factory()
    desc = promote(factory, run)
    path = factory.root / "baselines/smoke/baseline.json"
    snapshot_path = path.parent / desc["snapshot_path"]
    if kind in {"snapshot", "internal"}:
        snapshot = l.read_document(snapshot_path)
        snapshot["evidence"]["observations"][0]["scenario_id"] = "wrong"
        snapshot_path.write_text(l.canonical_json(snapshot))
        if kind == "internal": desc["snapshot_digest"] = l.fingerprint("baseline-snapshot-v1", snapshot)
    elif kind == "descriptor": desc["promotion_reason"] = "tampered"
    elif kind == "digest": desc["snapshot_digest"] = "0" * 64
    elif kind == "suite": desc["suite"] = "full"
    elif kind == "path": desc["snapshot_path"] = "../../outside.json"
    elif kind == "version": desc["baseline_schema_version"] = 2
    path.write_text(l.canonical_json(desc))
    with pytest.raises(ValueError): b.resolve_baseline(factory.root, "smoke")


def test_missing_and_trusted_override(factory, tmp_path):
    assert b.resolve_baseline(factory.root, "smoke") is None
    run, _ = factory()
    desc = promote(factory, run)
    assert b.resolve_baseline(tmp_path / "candidate", "smoke", trusted_baseline_dir=factory.root / "baselines", ci=True)["descriptor"] == desc
    with pytest.raises(ValueError, match="TRUSTED_BASELINE"):
        b.resolve_baseline(factory.root, "smoke", ci=True)
    assert b.resolve_baseline(factory.root, "smoke", trusted_baseline_dir=tmp_path / "absent", ci=True) is None


def metric(result, name="functional_accuracy", population="functional"):
    return next(r for r in result["metrics"] if r["metric_id"] == name and r["population"] == population)


@pytest.mark.parametrize("kind", ["identical", "model", "prompt", "code", "gate", "added", "removed", "modified", "denominator", "unknown_judge", "changed_judge", "protocol", "fail", "incomplete", "invalid", "no_baseline", "legacy", "fixture", "tool", "policy"])
def test_comparison_cases(factory, kind):
    known_judges = lambda m:[m["judge_models"].update({role:dict(resolved="judge-v1", provider="test", resolution_provenance="fixture")}) for role in m["judge_models"]]
    before, _ = factory(modify=known_judges if kind == "changed_judge" else None)
    if kind != "no_baseline": promote(factory, before)
    options = {}
    if kind == "model": options["modify"] = lambda m:m["production_models"]["synthesis"].update(resolved="experimental")
    if kind == "code": options["modify"] = lambda m:m["source"].update(git_commit="e" * 40)
    for case, component in (("prompt", "prompt_bundle"), ("gate", "release_gates"), ("fixture", "business_fixtures"), ("tool", "tool_contract"), ("policy", "safety_policy")):
        if kind == case: options["modify"] = lambda m, c=component:m["fingerprints"].update({c:"e" * 64})
    if kind == "changed_judge":
        def changed(m):
            known_judges(m)
            m["judge_models"]["correctness"]["resolved"] = "judge-v2"
        options["modify"] = changed
    if kind == "protocol": options["modify"] = lambda m:m["protocol"].update(repetitions=2)
    if kind == "added":
        extra = deepcopy(factory.datasets.functional[0]); extra["id"] = "extra"
        options["functional"] = [*factory.datasets.functional, extra]
    if kind == "removed": options["functional"] = factory.datasets.functional[:-1]
    if kind == "modified":
        rows = deepcopy(factory.datasets.functional); rows[0]["input"] = "changed benchmark"
        options["functional"] = rows
    if kind == "denominator": options["missing_metric"] = True
    if kind == "fail": options.update(passed=False, score=False, code=1)
    if kind == "incomplete": options.update(complete=False, observed=2, code=1)
    run, receipt = factory(**options)
    if kind == "invalid": (run.path / "results.json").write_text('{}')
    if kind == "legacy":
        rewrite(run, lambda r:[r.pop(k) for k in ("observations", "observation_contract_version", "continuous_aggregates", "metric_registry_version", "protocol")])
    result = compare(factory.root, suite="smoke", receipt=receipt.document, execution_code=options.get("code", 0))
    assert result["authority"] == "ADVISORY"
    assert "PRIVATE_RESPONSE" not in json.dumps(result) and "SECRET_SENTINEL" not in json.dumps(result)
    if kind == "no_baseline":
        assert result["statuses"]["comparison"] == "NO_BASELINE" and result["metrics"] == []
    elif kind == "invalid":
        assert result["candidate_state"] == "ARTIFACT_INVALID" and result["metrics"] == []
    elif kind in {"fixture", "tool", "policy", "legacy", "denominator"}:
        assert metric(result)["delta"] is None
    else:
        assert metric(result)["delta"] == (-1 if kind == "fail" else 0)
    if kind in {"added", "removed", "modified"}:
        assert len(result["scenario_diff"][kind]) == 1
        assert metric(result)["baseline_denominator"] == len(factory.datasets.functional) - (kind != "added")
    if kind == "unknown_judge": assert metric(result, "correctness")["exclusion_reason"] == "JUDGE_IDENTITY_UNKNOWN"
    if kind == "changed_judge": assert metric(result, "correctness")["exclusion_reason"] == "JUDGE_CHANGED"
    if kind == "protocol": assert metric(result, "total_latency_ms")["delta"] is None
    if kind == "fail": assert result["candidate_state"] == "COMPLETED_FAIL" and result["statuses"]["quality"] == "FAIL"
    if kind == "incomplete":
        assert result["candidate_state"] == "INCOMPLETE" and result["missing_candidate_scenario_ids"]
        assert all(m["cohort"] == "PARTIAL_OBSERVED_UNCHANGED" for m in result["metrics"])
    path = write_comparison(factory.root, result)
    assert (path / "summary.json").exists()


@pytest.mark.parametrize("code", [0, 1, 7])
def test_orchestrator_once_advisory_and_linkage(factory, monkeypatch, code):
    from scripts import run_continuous_evaluation as cli
    run, _ = factory(passed=code == 0)
    def execute(argv):
        current_invocation().link(run)
        return code
    runner = Mock(side_effect=execute)
    assert cli.main(["--suite", "smoke"], project_root=factory.root, runner=runner) == code
    assert runner.call_count == 1
    receipts = [l.read_document(p) for p in (factory.root / "reports/continuous_evaluation/invocations").glob("*/receipt.json")]
    linked = next(r for r in receipts if r["comparison_id"])
    assert linked["process_exit_code"] == code
    assert linked["evaluation_run_id"] == run.manifest.run_id
    monkeypatch.setattr(cli, "compare", Mock(side_effect=RuntimeError("SECRET_SENTINEL")))
    assert cli.main([], project_root=factory.root, runner=Mock(return_value=code)) == code


def test_orchestrator_setup_failure(factory, capsys):
    from scripts import run_continuous_evaluation as cli
    runner = Mock(side_effect=RuntimeError("SECRET_SENTINEL"))
    assert cli.main([], project_root=factory.root, runner=runner) == 1
    assert runner.call_count == 1
    receipt = next((factory.root / "reports/continuous_evaluation/invocations").glob("*/receipt.json"))
    document = l.read_document(receipt)
    assert document["process_state"] == "SETUP_FAILED" and document["comparison_id"]
    assert "SECRET_SENTINEL" not in receipt.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("decision", ["PASS", "FAIL", "REVIEW_REQUIRED"])
def test_release_policy_uses_linked_actual_decision(factory, decision):
    from uuid import uuid4
    run, receipt = factory()
    policy = l.read_document(factory.root / "config/baseline-promotion.json")
    policy["suites"]["smoke"]["require_release_pass"] = True
    (factory.root / "config/baseline-promotion.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="RELEASE_PASS_REQUIRED"): promote(factory, run)
    m = run.manifest.to_dict()
    m.update(run_id=uuid4().hex, suite="structural", scenario_count=0, planned_execution_count=0,
             scenario_set_fingerprint=l.fingerprint("scenario-set", []), execution_order_fingerprint=l.fingerprint("execution-order", []))
    m["protocol"].update(suite="structural", scenario_populations={})
    for population in ("functional", "safety"): m["datasets"][population]["selected"] = l.dataset_identity(population, [])
    release = l.RunArtifacts(factory.root, l.EvaluationRunManifest.create(m), [])
    release.source_integrity = run.source_integrity
    release.aggregate = dict(decision=decision, evaluation_lineage=run.reference)
    release.finish()
    receipt.link(release)
    if decision == "PASS":
        desc = promote(factory, run)
        assert b.resolve_baseline(factory.root, "smoke")["snapshot"]["evidence"]["release_decision"] == "PASS"
        assert desc["promotion_policy"]["require_release_pass"] is True
    else:
        with pytest.raises(ValueError, match="STRUCTURAL_RELEASE_NOT_PASS"): promote(factory, run)


@pytest.mark.parametrize("kind", ["changed", "unknown"])
def test_measurement_semantics_required(factory, kind):
    baseline, _ = factory()
    promote(factory, baseline)
    def alter(m):
        if kind == "changed": m["fingerprints"]["measurement_definition"] = "f" * 64
        else: m["fingerprints"].pop("measurement_definition")
    candidate, receipt = factory(modify=alter)
    result = compare(factory.root, suite="smoke", receipt=receipt.document)
    assert metric(result, "total_latency_ms")["delta"] is None
    assert metric(result)["delta"] == 0
    assert "measurement_definition" in result["comparability"]["changed_components"]


def test_unknown_judge_does_not_block_deterministic_or_known_operational(factory):
    run, _ = factory()
    promote(factory, run)
    _, receipt = factory()
    result = compare(factory.root, suite="smoke", receipt=receipt.document)
    assert metric(result, "correctness")["exclusion_reason"] == "JUDGE_IDENTITY_UNKNOWN"
    assert metric(result, "total_latency_ms")["delta"] == 0
    assert metric(result)["delta"] == 0


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(9)])
def test_orchestrator_terminal_exception_preserved(factory, error):
    from scripts import run_continuous_evaluation as cli
    with pytest.raises(type(error)):
        cli.main([], project_root=factory.root, runner=Mock(side_effect=error))
    receipt = l.read_document(next((factory.root / "reports/continuous_evaluation/invocations").glob("*/receipt.json")))
    assert receipt["process_exit_code"] == (130 if isinstance(error, KeyboardInterrupt) else 9)
    assert receipt["comparison_id"]
    if isinstance(error, KeyboardInterrupt): assert receipt["process_state"] == "CANCELLED"


def test_comparison_ignores_candidate_baseline_when_trusted_missing(factory):
    run, receipt = factory()
    promote(factory, run)
    result = compare(factory.root, suite="smoke", receipt=receipt.document, ci=True,
                     trusted_baseline_dir=factory.root / "trusted-absent")
    assert result["statuses"]["comparison"] == "NO_BASELINE"
    result = compare(factory.root, suite="smoke", receipt=receipt.document, ci=True)
    assert result["statuses"]["comparison"] == "BASELINE_INVALID" and not result["metrics"]


def test_promotion_cli_by_invocation(factory, capsys):
    from scripts.promote_evaluation_baseline import main
    _, receipt = factory()
    assert main(["--invocation-id", receipt.document["invocation_id"], "--suite", "smoke", "--reason", "reviewed"],
                project_root=factory.root) == 0
    assert "PROMOTION: CREATED" in capsys.readouterr().out


def test_untrusted_extra_model_payload_rejected(factory):
    run, _ = factory(modify=lambda m:m["judge_models"]["correctness"].update(raw_response="SECRET_SENTINEL"))
    with pytest.raises(ValueError, match="UNSAFE_MODEL_FIELDS"): promote(factory, run)


def test_partial_repetition_missing_population(factory):
    baseline, _ = factory()
    promote(factory, baseline)
    _, receipt = factory(complete=False, observed=1, modify=lambda m:m["protocol"].update(repetitions=2))
    result = compare(factory.root, suite="smoke", receipt=receipt.document)
    assert len(result["missing_candidate_executions"]) == 2 * (len(factory.datasets.functional) + len(factory.datasets.safety) - 1)


def test_unknown_required_promotion_identity(factory):
    run, _ = factory(modify=lambda m:m["fingerprints"].update(tool_contract="UNKNOWN"))
    with pytest.raises(ValueError, match="REQUIRED_LINEAGE_UNKNOWN"): promote(factory, run)


def test_missing_required_observation_cannot_promote(factory):
    run, _ = factory(missing_metric=True)
    with pytest.raises(ValueError, match="REQUIRED_OBSERVATIONS_UNKNOWN"): promote(factory, run)


def test_promotion_reason_redaction(factory):
    run, _ = factory()
    desc = b.promote(factory.root, suite="smoke", run_id=run.manifest.run_id, reason="Reviewed SECRET_SENTINEL")
    assert desc["promotion_reason"] == "Reviewed [REDACTED]"


def test_equal_denominators_with_different_known_rows_excluded(factory):
    baseline, _ = factory()
    promote(factory, baseline)
    # Promotion needs complete deterministic evidence. A nullable operational
    # metric can legitimately be known for different requests in each run.
    resolved = b.resolve_baseline(factory.root, "smoke")
    from src.agentguard.continuous_comparison import cohort_metrics
    left = resolved["snapshot"]["evidence"]
    right = deepcopy(left)
    left["observations"][0]["operational"]["production_tokens"] = None
    right["observations"][1]["operational"]["production_tokens"] = None
    rows, _ = cohort_metrics(left, right, [s["scenario_id"] for s in left["scenarios"]], False)
    result = next(r for r in rows if r["metric_id"] == "production_tokens" and r["population"] == "functional")
    assert result["baseline_denominator"] == result["candidate_denominator"]
    assert result["delta"] is None and result["exclusion_reason"] == "OBSERVATION_COVERAGE_MISMATCH"
