"""Explicit, local baseline governance. Never executes evaluations or commits files."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

from src.agentguard.artifact_validation import schema
from src.agentguard.datasets import load_datasets
from src.agentguard.lineage import (canonical_json, fingerprint, load_run, publish,
                                   read_document, scenario_index, UNKNOWN)
from src.agentguard.metric_registry import METRICS, REGISTRY_VERSION, aggregate
from src.agentguard.safety_incidents import Redactor


class BaselineError(ValueError):
    """Fixed public rejection code, never an exception-message projection."""


def require(condition, code):
    if not condition:
        raise BaselineError(code)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value), "INVALID_ID")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def promotion_lock(path):
    stream = path.open("x")
    try:
        yield
    finally:
        stream.close()
        path.unlink()


def quality(run):
    value = run.get("results", {}).get("aggregate_results", {}).get("quality_result", {}).get("passed")
    return "PASS" if value is True else "FAIL" if value is False else "UNKNOWN"


def receipt_for(root, *, run_id=None, invocation_id=None):
    directory = Path(root) / "reports/continuous_evaluation/invocations"
    if invocation_id:
        receipt = read_document(directory / identifier(invocation_id) / "receipt.json")
        require(receipt.get("invocation_id") == invocation_id, "INVOCATION_ID_MISMATCH")
    else:
        identifier(run_id)
        # Exact ID lookup only. Neither timestamps nor newest-PASS selection.
        matches = []
        for path in directory.glob("*/receipt.json"):
            candidate = read_document(path)
            if candidate.get("evaluation_run_id") == run_id:
                matches.append(candidate)
        require(len(matches) == 1, "EXACT_INVOCATION_REQUIRED")
        receipt = matches[0]
    schema(receipt, "evaluation-invocation.schema.json")
    identifier(receipt["evaluation_run_id"])
    if run_id:
        require(receipt["evaluation_run_id"] == run_id, "RUN_ID_MISMATCH")
    expected = Path(root) / "reports/evaluations" / receipt["evaluation_run_id"]
    require(Path(receipt["evaluation_artifact_path"]).resolve() == expected.resolve(), "ARTIFACT_PATH_MISMATCH")
    return receipt, expected


def release_decision(root, receipt, run):
    rid = receipt.get("linked_release_run_id")
    if not rid:
        require(not receipt.get("linked_release_artifact_path"), "RELEASE_LINK_INVALID")
        return "NOT_REQUESTED" if not receipt.get("requested_release_qualification") else "UNKNOWN"
    path = Path(root) / "reports/evaluations" / identifier(rid)
    require(Path(receipt.get("linked_release_artifact_path") or "").resolve() == path.resolve(), "RELEASE_LINK_INVALID")
    release = load_run(path)
    reference = dict(run_id=run["manifest"]["run_id"], manifest_digest=fingerprint("evaluation-run-manifest", run["manifest"]))
    evidence = release.get("results", {}).get("aggregate_results", {})
    require(release["manifest"]["suite"] == "structural" and evidence.get("evaluation_lineage") == reference,
            "RELEASE_LINK_INVALID")
    if release["completion_state"] != "COMPLETED":
        return "UNKNOWN"
    value = evidence.get("decision")
    return value if value in {"PASS", "FAIL", "REVIEW_REQUIRED"} else "UNKNOWN"


def policy_for(root, suite, policy_path=None):
    policy = read_document(policy_path or Path(root) / "config/baseline-promotion.json")
    require(set(policy) == {"promotion_policy_version", "suites"} and policy["promotion_policy_version"] == 1,
            "PROMOTION_POLICY_UNSUPPORTED")
    require(suite in policy["suites"], "SUITE_NOT_APPROVED")
    selected = policy["suites"][suite]
    require(set(selected) == {"require_release_pass", "execution_mode", "hosting_mode", "execution_ordering", "repetitions"}
            and type(selected["require_release_pass"]) is bool and type(selected["repetitions"]) is int
            and selected["repetitions"] > 0, "PROMOTION_POLICY_INVALID")
    return selected


def eligible(root, suite, run, receipt, policy, release):
    manifest, results = run["manifest"], run.get("results", {})
    require(manifest["suite"] == suite == receipt["suite"], "SUITE_MISMATCH")
    require(run["completion_state"] == "COMPLETED", "RUN_INCOMPLETE")
    require(receipt["process_state"] == "COMPLETED" and receipt["process_exit_code"] == 0
            and not receipt["setup_error_type"] and not receipt["finalization_error_type"], "PROCESS_UNSUCCESSFUL")
    require(quality(run) == "PASS", "QUALITY_NOT_PASS")
    require(results.get("source_integrity", {}).get("state") == "MATCH", "SOURCE_INTEGRITY_NOT_MATCH")
    require(manifest["source"]["dirty_state"] == "CLEAN", "SOURCE_DIRTY")
    require(all(manifest["source"].get(key) not in {None, UNKNOWN, ""} for key in ("git_commit", "source_fingerprint")),
            "SOURCE_IDENTITY_UNKNOWN")
    require(results.get("observation_contract_version") == 1 and results.get("metric_registry_version") == REGISTRY_VERSION,
            "OBSERVATIONS_REQUIRED")
    protocol = manifest.get("protocol", {})
    require(all(protocol.get(k) == policy[k] for k in ("execution_mode", "hosting_mode", "execution_ordering", "repetitions")),
            "PROTOCOL_NOT_APPROVED")
    require(receipt["requested_mode"] == protocol["execution_mode"], "INVOCATION_MODE_MISMATCH")
    datasets = load_datasets(Path(root) / "evals/datasets", suite=suite)
    approved = scenario_index([*datasets.functional, *datasets.safety])
    require(run["scenarios"] == approved, "POPULATION_NOT_APPROVED")
    expected = len(approved) * policy["repetitions"]
    require(results["completed_scenario_count"] == results["executed_scenario_count"] == expected
            and len(results["observations"]) == expected and all(o["completed"] for o in results["observations"])
            and not results.get("failures") and not any(e.get("failure_evidence") for e in results["executions"]),
            "EXECUTIONS_NOT_COMPLETE")
    # Unknown judge/revision identities are allowed and explicitly retained.
    # Missing benchmark/policy/evaluator sources are not eligible evidence.
    require(not any(p.startswith(("src/", "config/", "evals/", "data/", "runtime_contracts"))
                    for p in manifest["unavailable_fields"]), "REQUIRED_LINEAGE_UNKNOWN")
    required = ("business_fixtures", "aggregation", "tool_contract", "safety_policy", "deterministic_evaluator", "evaluation_config")
    require(all(re.fullmatch(r"[0-9a-f]{64}", manifest["fingerprints"].get(key, "")) for key in required), "REQUIRED_LINEAGE_UNKNOWN")
    require(all(all(type(o["deterministic"][key]) is bool for key in ("functional_pass", "tool_pass", "argument_pass"))
                if o["population"] == "functional" else type(o["safety"]["final_pass"]) is bool
                for o in results["observations"]), "REQUIRED_OBSERVATIONS_UNKNOWN")
    require(release not in {"FAIL", "REVIEW_REQUIRED"}, "STRUCTURAL_RELEASE_NOT_PASS")
    if policy["require_release_pass"] or receipt["requested_release_qualification"]:
        require(release == "PASS", "RELEASE_PASS_REQUIRED")


def evidence_view(run, release="UNKNOWN"):
    """Strict schemas supply the allowlist; original aggregate free text is omitted."""
    manifest = deepcopy(run["manifest"])
    schema(manifest, "evaluation-run-manifest.schema.json")
    from src.agentguard.lineage_adapters import PACKAGES, JUDGES, SOURCE_GROUPS
    require(set(manifest["libraries"]) <= {"python", *PACKAGES}
            and set(manifest["hosting"]) == {"mode", "max_workers", "queue_capacity", "configuration_fingerprint"}
            and set(manifest["datasets"]) == {"functional", "safety"}
            and set(manifest["judge_models"]) == set(JUDGES)
            and (not isinstance(manifest["production_models"], dict) or set(manifest["production_models"]) == {"router", "recovery", "synthesis"})
            and set(manifest["fingerprints"]) <= {*SOURCE_GROUPS, "production_model_settings", "aggregation", "release_gates", "slo_spec", "business_fixtures"},
            "UNSAFE_MANIFEST_FIELDS")
    require(all(set(d) == {"full", "selected", "schema_version"} for d in manifest["datasets"].values()), "UNSAFE_DATASET_FIELDS")
    model_fields = {"configured", "resolved", "provider", "resolution_provenance", "immutable_revision"}
    for group in (manifest["production_models"], manifest["judge_models"]):
        if isinstance(group, dict):
            require(all(set(model) <= model_fields for model in group.values()), "UNSAFE_MODEL_FIELDS")
    results = run.get("results", {})
    observations = deepcopy(results.get("observations", []))
    for row in observations:
        schema(row, "evaluation-observation.schema.json")
        for metric in row["semantic"].values():
            require(metric["judge_identity"] == manifest["judge_models"][metric["judge_role"]], "JUDGE_LINKAGE_INVALID")
            require(metric["evaluator_available"] == (metric["score"] is not None), "EVALUATOR_AVAILABILITY_INVALID")
        require(row["safety_judge_linkage"] == {role: manifest["judge_models"][role] for role in ("prompt_injection", "action_claim")},
                "JUDGE_LINKAGE_INVALID")
    view = dict(manifest=manifest, scenarios=deepcopy(run["scenarios"]),
        completion_state=run["completion_state"], observations=observations,
        quality_decision=quality(run), release_decision=release,
        aggregate_observations=aggregate(observations, population_complete=run["completion_state"] == "COMPLETED"),
        observed_production_models=deepcopy(results.get("observed_model_identities", [])),
        observation_contract_version=results.get("observation_contract_version"),
        metric_registry_version=results.get("metric_registry_version"))
    # Never copy arbitrary top-level model dictionaries from old result envelopes.
    view["observed_production_models"] = [dict(model=m["model"], immutable_revision=m["immutable_revision"],
        request_id=m.get("request_id"), scenario_id=m.get("scenario_id"), repetition=m.get("repetition"))
        for m in view["observed_production_models"] if set(m) <= {"model", "immutable_revision", "request_id", "scenario_id", "repetition"}
        and "model" in m and "immutable_revision" in m]
    require(Redactor([run]).clean(view) == view, "UNSAFE_IDENTITY_EVIDENCE")
    return view


@dataclass(frozen=True)
class EvaluationBaseline:
    """Versioned descriptor whose reviewed metadata is bound into its snapshot."""
    document: dict

    @classmethod
    def load(cls, document):
        schema(document, "evaluation-baseline.schema.json")
        return cls(deepcopy(document))


def resolve_baseline(root, suite, *, trusted_baseline_dir=None, ci=False):
    require(bool(re.fullmatch(r"[a-z][a-z0-9_]*", suite)), "INVALID_SUITE")
    require(not ci or trusted_baseline_dir is not None, "TRUSTED_BASELINE_DIRECTORY_REQUIRED")
    base = Path(trusted_baseline_dir) if trusted_baseline_dir is not None else Path(root) / "baselines"
    descriptor_path = base / suite / "baseline.json"
    require(descriptor_path.resolve().is_relative_to(base.resolve()), "BASELINE_PATH_ESCAPE")
    if not descriptor_path.exists():
        return None
    descriptor = EvaluationBaseline.load(read_document(descriptor_path)).document
    require(descriptor["suite"] == suite, "BASELINE_SUITE_MISMATCH")
    relative = f"snapshots/{identifier(descriptor['baseline_id'])}.json"
    require(descriptor["snapshot_path"] == relative, "SNAPSHOT_PATH_INVALID")
    path = descriptor_path.parent / relative
    require(path.resolve().is_relative_to(descriptor_path.parent.resolve()), "SNAPSHOT_PATH_ESCAPE")
    snapshot = read_document(path)
    require(fingerprint("baseline-snapshot-v1", snapshot) == descriptor["snapshot_digest"], "SNAPSHOT_DIGEST_MISMATCH")
    schema(snapshot, "evaluation-baseline-snapshot.schema.json")
    metadata = {k: v for k, v in descriptor.items() if k not in {"snapshot_path", "snapshot_digest"}}
    require(snapshot["baseline"] == metadata, "BASELINE_DESCRIPTOR_MISMATCH")
    view = snapshot["evidence"]
    m = view["manifest"]
    schema(m, "evaluation-run-manifest.schema.json")
    require(m["suite"] == suite and m["run_id"] == descriptor["source_run_id"]
            and fingerprint("evaluation-run-manifest", m) == descriptor["manifest_digest"], "BASELINE_PROVENANCE_MISMATCH")
    require(snapshot["metric_definitions"] == list(METRICS), "METRIC_DEFINITIONS_UNSUPPORTED")
    for observation in view["observations"]:
        schema(observation, "evaluation-observation.schema.json")
    require(view["aggregate_observations"] == aggregate(view["observations"], population_complete=True), "SNAPSHOT_AGGREGATE_MISMATCH")
    require(snapshot["scenario_index"] == [dict(row, population=m["protocol"]["scenario_populations"][row["scenario_id"]])
            for row in view["scenarios"]], "SNAPSHOT_SCENARIO_INDEX_MISMATCH")
    from src.agentguard.artifact_validation import validate_loaded
    observations = view["observations"]
    results = dict(result_schema_version=1, run_id=m["run_id"], manifest_digest=descriptor["manifest_digest"],
        completion_state="COMPLETED", executed_scenario_count=len(observations), completed_scenario_count=len(observations),
        executions=[dict(scenario_id=o["scenario_id"], completed=o["completed"], repetition=o["repetition"]) for o in observations],
        failures=[], aggregate_results={}, observed_model_identities=view["observed_production_models"], observation_contract_version=1,
        observations=observations, continuous_aggregates=view["aggregate_observations"], metric_registry_version=REGISTRY_VERSION,
        source_integrity=descriptor["source_integrity"], protocol=m["protocol"])
    validate_loaded(dict(manifest=m, scenarios=view["scenarios"], results=results))
    evidence_view(dict(manifest=m, scenarios=view["scenarios"], completion_state="COMPLETED", results=results))
    require(view["completion_state"] == "COMPLETED" and view["quality_decision"] == "PASS"
            and descriptor["source_integrity"]["state"] == "MATCH" and m["source"]["dirty_state"] == "CLEAN", "BASELINE_NOT_ELIGIBLE")
    require(descriptor["protocol"] == m["protocol"] and descriptor["fingerprints"] == m["fingerprints"]
            and descriptor["git_commit"] == m["source"]["git_commit"]
            and descriptor["source_fingerprint"] == m["source"]["source_fingerprint"]
            and descriptor["lineage_limitations"] == m["unavailable_fields"]
            and descriptor["judge_model_identities"] == m["judge_models"]
            and descriptor["production_model_identities"] == m["production_models"], "BASELINE_IDENTITY_MISMATCH")
    require(descriptor["selected_datasets"] == {k:m["datasets"][k]["selected"] for k in ("functional", "safety")}
            and descriptor["scenario_set_fingerprint"] == m["scenario_set_fingerprint"]
            and descriptor["business_fixture_fingerprint"] == m["fingerprints"]["business_fixtures"]
            and descriptor["lineage_status"] == m["lineage_status"]
            and descriptor["observed_production_model_identities"] == view["observed_production_models"], "BASELINE_IDENTITY_MISMATCH")
    require(not descriptor["promotion_policy"]["require_release_pass"] or view["release_decision"] == "PASS", "RELEASE_PASS_REQUIRED")
    require(Redactor([snapshot]).clean(snapshot) == snapshot, "UNSAFE_SNAPSHOT")
    return dict(descriptor=descriptor, snapshot=snapshot)


def promote(root, *, suite, reason, run_id=None, invocation_id=None, policy_path=None, baseline_id=None, promoted_by_mode="MANUAL"):
    root = Path(root)
    receipt, path = receipt_for(root, run_id=run_id, invocation_id=invocation_id)
    run = load_run(path)
    policy = policy_for(root, suite, policy_path)
    release = release_decision(root, receipt, run)
    eligible(root, suite, run, receipt, policy, release)
    view = evidence_view(run, release)
    safe_reason = Redactor([run]).clean(reason).strip()
    require(0 < len(safe_reason) <= 500, "PROMOTION_REASON_REQUIRED")
    bid = identifier(baseline_id or uuid4().hex)
    directory = root / "baselines" / suite
    directory.mkdir(parents=True, exist_ok=True)
    # Serialize descriptor updates; fail closed instead of losing previous linkage.
    lock = directory / ".promotion.lock"
    with promotion_lock(lock):
        previous = resolve_baseline(root, suite)
        m, r = run["manifest"], run["results"]
        meta = dict(baseline_schema_version=1, baseline_id=bid, suite=suite, protocol=m["protocol"],
            promoted_at=now(), promoted_by_mode=promoted_by_mode, source_run_id=m["run_id"],
            source_invocation_id=receipt["invocation_id"], manifest_digest=fingerprint("evaluation-run-manifest", m),
            results_digest=fingerprint("evaluation-results", r), git_commit=m["source"]["git_commit"],
            source_fingerprint=m["source"]["source_fingerprint"], source_integrity=r["source_integrity"],
            lineage_status=m["lineage_status"], lineage_limitations=m["unavailable_fields"],
            scenario_set_fingerprint=m["scenario_set_fingerprint"],
            selected_datasets={k: m["datasets"][k]["selected"] for k in ("functional", "safety")},
            business_fixture_fingerprint=m["fingerprints"]["business_fixtures"],
            production_model_identities=m["production_models"], observed_production_model_identities=view["observed_production_models"],
            judge_model_identities=m["judge_models"], fingerprints=m["fingerprints"],
            metric_registry_version=REGISTRY_VERSION, observation_contract_version=1,
            promotion_reason=safe_reason, promotion_policy=policy, previous_baseline=None if previous is None else
            {k: previous["descriptor"][k] for k in ("baseline_id", "source_run_id", "snapshot_digest")})
        snapshot = dict(snapshot_schema_version=1, baseline=meta, evidence=view, metric_definitions=list(METRICS),
            scenario_index=[dict(row, population=m["protocol"]["scenario_populations"][row["scenario_id"]]) for row in view["scenarios"]])
        descriptor = dict(meta, snapshot_path=f"snapshots/{bid}.json", snapshot_digest=fingerprint("baseline-snapshot-v1", snapshot))
        EvaluationBaseline.load(descriptor)
        schema(snapshot, "evaluation-baseline-snapshot.schema.json")
        destination = directory / descriptor["snapshot_path"]
        destination.parent.mkdir(exist_ok=True)
        publish(destination, snapshot)
        require(fingerprint("baseline-snapshot-v1", read_document(destination)) == descriptor["snapshot_digest"], "SNAPSHOT_WRITE_MISMATCH")
        temporary = directory / (".baseline-" + uuid4().hex + ".tmp")
        try:
            temporary.write_text(canonical_json(descriptor) + "\n", encoding="utf-8")
            temporary.replace(directory / "baseline.json")
        finally:
            temporary.unlink(missing_ok=True)
        return descriptor
