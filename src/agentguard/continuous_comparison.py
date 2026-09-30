"""Advisory cohort deltas over validated evidence; no drift or release decisions."""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from src.agentguard.baselines import evidence_view, now, quality, release_decision, resolve_baseline
from src.agentguard.comparability import compare_runs, diff_scenarios
from src.agentguard.lineage import fingerprint, load_run, publish, UNKNOWN, DISABLED
from src.agentguard.metric_registry import METRICS, aggregate


def known(value):
    if value is None or value in (UNKNOWN, ""):
        return False
    if isinstance(value, dict):
        return bool(value) and all(known(v) for v in value.values())
    if isinstance(value, list):
        return all(known(v) for v in value)
    return True


def exclusion(metric, left, right):
    a, b = left["manifest"], right["manifest"]
    family = metric["family"]
    if left.get("observation_contract_version") != 1 or right.get("observation_contract_version") != 1:
        return "LEGACY_OBSERVATIONS_UNAVAILABLE"
    if left.get("metric_registry_version") != right.get("metric_registry_version"):
        return "METRIC_REGISTRY_CHANGED"
    if a["suite"] != b["suite"]:
        return "SUITE_CHANGED"
    if a["execution_mode"] != b["execution_mode"]:
        return "EXECUTION_MODE_CHANGED"
    deps = ["business_fixtures", "aggregation"]
    if family == "DETERMINISTIC":
        deps += ["deterministic_evaluator", "tool_contract", "safety_policy"]
    elif family in {"SEMANTIC", "SAFETY"}:
        deps += ["evaluator_prompts", "evaluation_config"]
        deps += ["semantic_evaluator"] if family == "SEMANTIC" else ["safety_evaluator", "safety_policy", "tool_contract"]
        roles = {"answer_relevancy": ["answer_relevancy"], "correctness": ["correctness"],
                 "faithfulness": ["hallucination"], "safety_pass_rate": ["prompt_injection", "action_claim"]}[metric["metric_id"]]
        for role in roles:
            ja, jb = a["judge_models"].get(role), b["judge_models"].get(role)
            if not known(ja) or not known(jb) or ja.get("resolved") == DISABLED or jb.get("resolved") == DISABLED:
                return "JUDGE_IDENTITY_UNKNOWN"
            if ja != jb:
                return "JUDGE_CHANGED"
        if not known(a["libraries"]) or not known(b["libraries"]):
            return "LIBRARY_IDENTITY_UNKNOWN"
        if a["libraries"] != b["libraries"]:
            return "LIBRARY_CHANGED"
    else:
        deps += ["runtime_policy", "evaluation_config", "measurement_definition"]
        for key in ("protocol", "hosting", "libraries", "execution_order_fingerprint"):
            if not known(a.get(key)) or not known(b.get(key)):
                return "OPERATIONAL_IDENTITY_UNKNOWN"
            if a.get(key) != b.get(key):
                return "OPERATIONAL_PROTOCOL_CHANGED"
        if a["scenario_set_fingerprint"] != b["scenario_set_fingerprint"]:
            return "OPERATIONAL_POPULATION_CHANGED"
    for manifest in (a, b):
        if any(p.startswith(("src/agentguard/", "data/", "runtime_contracts")) for p in manifest.get("unavailable_fields", [])):
            return "REQUIRED_LINEAGE_UNKNOWN"
    for dep in deps:
        x, y = a["fingerprints"].get(dep), b["fingerprints"].get(dep)
        if not known(x) or not known(y):
            return "REQUIRED_LINEAGE_UNKNOWN"
        if x != y:
            return dep.upper() + "_CHANGED"
    return None


def metric_value(row, metric):
    value = row
    for part in metric["observation_path"].split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def cohort_metrics(left, right, unchanged, incomplete):
    completed = lambda view: {(o["scenario_id"], o["repetition"]) for o in view["observations"]
                              if o["completed"] and o["scenario_id"] in unchanged}
    keys = completed(left) & completed(right)
    a = [o for o in left["observations"] if (o["scenario_id"], o["repetition"]) in keys]
    b = [o for o in right["observations"] if (o["scenario_id"], o["repetition"]) in keys]
    la = {(v["metric_id"], v["population"]): v for v in aggregate(a, population_complete=False)}
    lb = {(v["metric_id"], v["population"]): v for v in aggregate(b, population_complete=False)}
    rows = []
    for metric in METRICS:
        populations = ("functional", "safety") if metric["population"] == "each_population" else (metric["population"],)
        for population in populations:
            x, y = la[metric["metric_id"], population], lb[metric["metric_id"], population]
            reason = exclusion(metric, left, right)
            coverage = lambda observations: {(o["scenario_id"], o["repetition"]) for o in observations
                if o["population"] == population and type(metric_value(o, metric)) in (int, float, bool)}
            if not reason:
                if not x["sample_count"] or not y["sample_count"]:
                    reason = "NO_KNOWN_PAIRED_OBSERVATIONS"
                elif x["sample_count"] != y["sample_count"]:
                    reason = "DENOMINATOR_MISMATCH"
                elif coverage(a) != coverage(b):
                    reason = "OBSERVATION_COVERAGE_MISMATCH"
            rows.append(dict(metric_id=metric["metric_id"], metric_version=metric["version"], population=population,
                cohort="PARTIAL_OBSERVED_UNCHANGED" if incomplete else "UNCHANGED", scenario_ids=sorted({sid for sid, rep in keys
                    if left["manifest"]["protocol"]["scenario_populations"].get(sid) == population}),
                baseline_raw_value=x["value"], candidate_raw_value=y["value"],
                delta=None if reason else y["value"] - x["value"],
                baseline_denominator=x["denominator"], baseline_sample_count=x["sample_count"],
                candidate_denominator=y["denominator"], candidate_sample_count=y["sample_count"],
                eligibility="INELIGIBLE" if reason else "ELIGIBLE", exclusion_reason=reason))
    return rows, keys


@dataclass(frozen=True)
class ContinuousEvaluationComparison:
    document: dict


def compare(root, *, suite, receipt, trusted_baseline_dir=None, ci=False, execution_code=0):
    """Never trusts invalid candidate measurements; failures remain advisory."""
    result = dict(comparison_schema_version=1, comparison_id=uuid4().hex, created_at=now(),
        authority="ADVISORY", baseline_id=None, baseline_run_id=None,
        candidate_run_id=receipt.get("evaluation_run_id"), candidate_invocation_id=receipt["invocation_id"],
        baseline=None, candidate=None, artifact_integrity=dict(baseline="NO_BASELINE", candidate="UNAVAILABLE"),
        candidate_state="INCOMPLETE", comparability=dict(overall_classification="NOT_COMPARABLE", reason_codes=[],
            changed_components=[], eligible_metrics=[], eligible_scenario_ids=[]),
        scenario_diff=dict(unchanged=[], added=[], removed=[], modified=[], changed_partitions=[]),
        metrics=[], candidate_observations=[], added_scenario_outcomes=[], modified_scenario_outcomes=[],
        missing_candidate_scenario_ids=[], missing_candidate_executions=[], lineage_limitations=dict(baseline=[], candidate=[]),
        statuses=dict(execution="PASS" if execution_code == 0 else "FAIL", artifact_integrity="UNKNOWN",
                      quality="UNKNOWN", release="UNKNOWN", comparison="NO_BASELINE"))
    candidate = None
    if receipt.get("evaluation_run_id"):
        try:
            from src.agentguard.baselines import identifier, require
            expected = Path(root) / "reports/evaluations" / identifier(receipt["evaluation_run_id"])
            require(Path(receipt["evaluation_artifact_path"]).resolve() == expected.resolve(), "CANDIDATE_PATH_MISMATCH")
            run = load_run(expected)
            require(run["manifest"]["run_id"] == receipt["evaluation_run_id"] and run["manifest"]["suite"] == suite, "CANDIDATE_ID_MISMATCH")
            release = release_decision(root, receipt, run)
            candidate = evidence_view(run, release)
            result["candidate"] = dict(manifest_digest=fingerprint("evaluation-run-manifest", run["manifest"]),
                results_digest=fingerprint("evaluation-results", run["results"]) if "results" in run else None,
                source_commit=run["manifest"]["source"]["git_commit"])
            result["candidate_state"] = "INCOMPLETE" if run["completion_state"] != "COMPLETED" else (
                "COMPLETED_PASS" if quality(run) == "PASS" and execution_code == 0 else "COMPLETED_FAIL")
            result["artifact_integrity"]["candidate"] = "PASS"
            result["candidate_observations"] = candidate["observations"]
            result["lineage_limitations"]["candidate"] = candidate["manifest"]["unavailable_fields"]
            result["statuses"].update(quality=quality(run), release=release)
            done = {o["scenario_id"] for o in candidate["observations"] if o["completed"]}
            result["missing_candidate_scenario_ids"] = sorted({s["scenario_id"] for s in candidate["scenarios"]} - done)
            protocol = candidate["manifest"].get("protocol")
            if protocol:
                completed = {(o["scenario_id"], o["repetition"]) for o in candidate["observations"] if o["completed"]}
                result["missing_candidate_executions"] = [dict(scenario_id=s["scenario_id"], repetition=rep)
                    for s in candidate["scenarios"] for rep in range(1, protocol["repetitions"] + 1)
                    if (s["scenario_id"], rep) not in completed]
        except (ValueError, OSError, KeyError, TypeError):
            result["candidate_state"] = "ARTIFACT_INVALID"
            result["artifact_integrity"]["candidate"] = "FAIL"
            candidate = None
    try:
        baseline = resolve_baseline(root, suite, trusted_baseline_dir=trusted_baseline_dir, ci=ci)
    except (ValueError, OSError, KeyError, TypeError):
        baseline = None
        result["artifact_integrity"]["baseline"] = "FAIL"
        result["statuses"]["comparison"] = "BASELINE_INVALID"
    if baseline is not None:
        descriptor, left = baseline["descriptor"], baseline["snapshot"]["evidence"]
        result.update(baseline_id=descriptor["baseline_id"], baseline_run_id=descriptor["source_run_id"],
            baseline=dict(manifest_digest=descriptor["manifest_digest"], results_digest=descriptor["results_digest"],
                snapshot_digest=descriptor["snapshot_digest"], source_commit=descriptor["git_commit"]))
        result["artifact_integrity"]["baseline"] = "PASS"
        result["lineage_limitations"]["baseline"] = descriptor["lineage_limitations"]
        result["statuses"]["comparison"] = "NOT_COMPARABLE"
        if candidate is not None:
            differences = diff_scenarios(left["scenarios"], candidate["scenarios"])
            diff = {kind.lower(): [d["scenario_id"] for d in differences if d["classification"] == kind]
                    for kind in ("UNCHANGED", "ADDED", "REMOVED", "MODIFIED")}
            diff["changed_partitions"] = [d for d in differences if d["changed_partitions"]]
            result["scenario_diff"] = diff
            result["added_scenario_outcomes"] = [o for o in candidate["observations"] if o["scenario_id"] in diff["added"]]
            result["modified_scenario_outcomes"] = [o for o in candidate["observations"] if o["scenario_id"] in diff["modified"]]
            metrics, keys = cohort_metrics(left, candidate, diff["unchanged"], result["candidate_state"] == "INCOMPLETE")
            result["metrics"] = metrics
            # 14A supplies source/component change evidence. Completion is handled
            # explicitly above, so its whole-run early exit does not hide cohorts.
            old = compare_runs(dict(left, completion_state="COMPLETED"), dict(candidate, completion_state="COMPLETED"))
            changed = set(old["changed_components"])
            af, bf = left["manifest"]["fingerprints"], candidate["manifest"]["fingerprints"]
            changed.update(key for key in af.keys() | bf.keys() if af.get(key) != bf.get(key))
            if left["manifest"].get("protocol") != candidate["manifest"].get("protocol"):
                changed.add("protocol")
            if left["observed_production_models"] != candidate["observed_production_models"]:
                # Request IDs change on every run; compare only the model identity.
                model_set = lambda v: {(m["model"], m["immutable_revision"]) for m in v["observed_production_models"]}
                if model_set(left) != model_set(candidate):
                    changed.add("observed_production_models")
            eligible = [m for m in metrics if m["eligibility"] == "ELIGIBLE"]
            classification = "NOT_COMPARABLE" if not eligible else "PARTIALLY_COMPARABLE" if (
                len(eligible) != len(metrics) or diff["added"] or diff["removed"] or diff["modified"] or result["candidate_state"] == "INCOMPLETE") else "DIRECTLY_COMPARABLE"
            reasons = set(old["reason_codes"]) | {m["exclusion_reason"] for m in metrics if m["exclusion_reason"]}
            if candidate.get("observation_contract_version") != 1:
                reasons.add("LIMITED_COMPARABILITY")
            if result["candidate_state"] == "INCOMPLETE":
                reasons.add("PARTIAL_POPULATION")
            result["comparability"] = dict(overall_classification=classification, reason_codes=sorted(reasons),
                changed_components=sorted(changed), eligible_metrics=sorted({m["metric_id"] for m in eligible}),
                eligible_scenario_ids=sorted({sid for m in eligible for sid in m["scenario_ids"]}))
            result["statuses"]["comparison"] = classification
    if candidate is None:
        result["comparability"]["reason_codes"].append(result["candidate_state"])
    if baseline is None:
        result["comparability"]["reason_codes"].append(result["statuses"]["comparison"])
    result["statuses"]["artifact_integrity"] = "FAIL" if "FAIL" in result["artifact_integrity"].values() else (
        "PASS" if result["artifact_integrity"]["candidate"] == "PASS" else "UNAVAILABLE")
    return ContinuousEvaluationComparison(result).document


def write_comparison(root, result):
    from src.agentguard.artifact_validation import schema
    schema(result, "continuous-evaluation-comparison.schema.json")
    directory = Path(root) / "reports/continuous_evaluation/comparisons" / result["comparison_id"]
    directory.mkdir(parents=True, exist_ok=False)
    publish(directory / "comparison.json", result)
    summary = {k: deepcopy(result[k]) for k in ("comparison_schema_version", "comparison_id", "baseline_id",
        "candidate_run_id", "candidate_invocation_id", "candidate_state", "statuses", "comparability", "authority")}
    publish(directory / "summary.json", summary)
    return directory


def compare_model_prompt_runs(root, *, baseline_run_id, candidate_run_id, baseline_label=None, candidate_label=None):
    """Explicit completed-run view over the same M14 cohort/eligibility engine.

    No baseline promotion, execution, gate evaluation, or threshold changes.
    """
    from src.agentguard.baselines import identifier, require
    from src.agentguard.model_prompt_report import variant, change_type, gate_context, metric_rows, scenario_changes, safe_label
    runs = []
    for run_id in (baseline_run_id, candidate_run_id):
        run = load_run(Path(root) / "reports/evaluations" / identifier(run_id))
        require(run["manifest"]["run_id"] == run_id, "RUN_ID_MISMATCH")
        require(run["completion_state"] == "COMPLETED", "RUN_INCOMPLETE")
        runs.append(run)
    left, right = [evidence_view(run) for run in runs]
    old = compare_runs(left, right)
    unchanged = [r["scenario_id"] for r in old["scenario_diff"] if r["classification"] == "UNCHANGED"]
    rows, _ = cohort_metrics(left, right, unchanged, False)
    # Explicit same-benchmark comparisons are stricter than M14 partial cohorts.
    common_reasons = []
    if any(r["classification"] != "UNCHANGED" for r in old["scenario_diff"]):
        common_reasons.append("BENCHMARK_CHANGED")
    a, b = left["manifest"], right["manifest"]
    if not known(a["datasets"]) or not known(b["datasets"]): common_reasons.append("DATASET_IDENTITY_UNKNOWN")
    elif a["datasets"] != b["datasets"]: common_reasons.append("DATASET_CHANGED")
    # Execution profiles affect operational eligibility; they do not redefine quality.
    protocols = [{k: v for k, v in m.get("protocol", {}).items() if k != "execution_profile"} for m in (a, b)]
    if not all(known(p) for p in protocols): common_reasons.append("PROTOCOL_UNKNOWN")
    elif protocols[0] != protocols[1]: common_reasons.append("EVALUATION_PROTOCOL_CHANGED")
    for run in runs:
        integrity = run.get("results", {}).get("source_integrity", {}).get("state", UNKNOWN)
        if integrity != "MATCH": common_reasons.append("SOURCE_INTEGRITY_" + integrity)
        if any(not row["completed"] for row in run.get("results", {}).get("executions", [])):
            common_reasons.append("INCOMPLETE_EXECUTION_EVIDENCE")
    aliases = {"factual_grounding": "functional_accuracy", "faithfulness": "hallucination"}
    definitions = {m["metric_id"]: m for m in METRICS}
    for row in rows:
        definition = definitions[row["metric_id"]]
        old_name = aliases.get(row["metric_id"], row["metric_id"])
        if definition["family"] == "OPERATIONAL":
            old_name = "tokens" if definition["unit"] == "tokens" else "latency"
        reason = common_reasons[0] if common_reasons else row["exclusion_reason"]
        if not reason and old_name not in old["eligible_metrics"]: reason = "M14_METRIC_INELIGIBLE"
        if reason: row.update(delta=None, eligibility="INELIGIBLE", exclusion_reason=reason)
    metrics = metric_rows(rows)
    dimensions = {"quality": "DETERMINISTIC", "semantic": "SEMANTIC", "safety": "SAFETY", "operational": "OPERATIONAL"}
    comparability = {}
    for name, family in dimensions.items():
        selected = [m for m in metrics if definitions[m["metric_name"]]["family"] == family]
        eligible = sum(m["eligible"] for m in selected)
        comparability[name] = "NOT_COMPARABLE" if not eligible else "COMPARABLE" if eligible == len(selected) else "PARTIALLY_COMPARABLE"
    variants = [variant(view) for view in (left, right)]
    limitations = set(common_reasons) | {m["reason"] for m in metrics if m["reason"]}
    limitations.update(old["reason_codes"])
    if any(v["model_revision_available"] is False for v in variants): limitations.add("PRODUCTION_REVISION_UNKNOWN")
    if a["fingerprints"].get("release_gates") != b["fingerprints"].get("release_gates"):
        limitations.add("GATE_DEFINITIONS_CHANGED")
    comparability["limitations"] = sorted(limitations)
    references = [dict(run_id=r["manifest"]["run_id"], manifest_digest=fingerprint("evaluation-run-manifest", r["manifest"]),
                       results_digest=fingerprint("evaluation-results", r["results"])) for r in runs]
    from src.agentguard.metric_registry import REGISTRY_VERSION
    comparison_id = fingerprint("model-prompt-comparison-v1", dict(references=references, metric_registry_version=REGISTRY_VERSION))[:32]
    gates = [gate_context(run) for run in runs]
    return dict(schema_version=1, comparison_id=comparison_id, created_at=now(),
                baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id,
                baseline_label=safe_label(baseline_label, runs), candidate_label=safe_label(candidate_label, runs),
                variant_identity=dict(baseline=variants[0], candidate=variants[1], change_type=change_type(*variants)),
                comparability=comparability, metric_comparisons=metrics,
                scenario_regressions=scenario_changes(left, right, metrics),
                baseline_gate_decision=gates[0]["decision"], candidate_gate_decision=gates[1]["decision"],
                gate_context=dict(baseline=gates[0], candidate=gates[1]), lineage_references=references,
                authority="DESCRIPTIVE_ONLY", performance_qualification=False)


def write_model_prompt_comparison(root, result):
    """Write-once technical comparison; repeat commands reuse its first display labels."""
    from src.agentguard.model_prompt_report import validate_report
    from src.agentguard.lineage import read_document
    validate_report(result)
    path = Path(root) / "reports/model_prompt_comparisons" / result["comparison_id"] / "comparison.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = read_document(path)
        validate_report(previous)
        display = {"created_at", "baseline_label", "candidate_label"}
        if {k: v for k, v in previous.items() if k not in display} != {k: v for k, v in result.items() if k not in display}:
            raise ValueError("COMPARISON_ARTIFACT_CONFLICT")
    else:
        publish(path, result)
    return path
