"""Metric-specific offline compatibility; no statistical or causal claims."""
from src.agentguard.lineage import UNKNOWN, DISABLED

DETERMINISTIC = {"functional_accuracy", "tool_accuracy", "argument_accuracy"}
SEMANTIC = {"answer_relevancy", "correctness", "hallucination", "safety_pass_rate"}
OPERATIONS = {"latency", "tokens"}


def diff_scenarios(before, after):
    def index(rows):
        result = {r["scenario_id"]: r for r in rows}
        if len(result) != len(rows):
            raise ValueError("Duplicate scenario ID")
        return result
    left, right = index(before), index(after)
    result = []
    for sid in sorted(left.keys() | right.keys()):
        changed = []
        if sid not in left:
            status = "ADDED"
        elif sid not in right:
            status = "REMOVED"
        else:
            changed = [part for part in ("content", "metadata", "expected_behavior")
                       if left[sid][part + "_fingerprint"] != right[sid][part + "_fingerprint"]]
            status = "MODIFIED" if changed else "UNCHANGED"
        result.append(dict(scenario_id=sid, classification=status, changed_partitions=changed))
    return result


def compare_runs(before, after):
    a, b = before.get("manifest", {}), after.get("manifest", {})
    reasons, changed = [], []
    eligible = DETERMINISTIC | SEMANTIC | OPERATIONS
    partial = False
    def change(name, reason):
        changed.append(name)
        reasons.append(reason)
    if not a or not b:
        return dict(classification="NOT_COMPARABLE", reason_codes=["CRITICAL_LINEAGE_MISSING"],
                    changed_components=[], eligible_metrics=[], eligible_scenario_ids=[], scenario_diff=[])
    if any(r.get("completion_state") == "INCOMPLETE" for r in (before, after)):
        return dict(classification="NOT_COMPARABLE", reason_codes=["RUN_INCOMPLETE"],
                    changed_components=[], eligible_metrics=[], eligible_scenario_ids=[], scenario_diff=[])
    if any(m.get("suite") not in {"smoke", "full"} for m in (a, b)):
        eligible &= OPERATIONS
    for manifest in (a, b):
        production = manifest.get("production_models")
        missing = (not isinstance(production, dict) or any(
            production.get(role, {}).get("resolved", UNKNOWN) == UNKNOWN for role in ("router", "recovery", "synthesis")))
        if missing or any(manifest.get("source", {}).get(k, UNKNOWN) == UNKNOWN for k in ("git_commit", "source_fingerprint")):
            reasons.append("CRITICAL_LINEAGE_MISSING")
            partial = True
        if any(v == UNKNOWN for v in manifest.get("libraries", {}).values()):
            reasons.append("CRITICAL_LINEAGE_MISSING")
            partial = True
        missing_sources = [f for f in manifest.get("unavailable_fields", []) if f.startswith(("src/", "config/", "evals/", "data/"))]
        if missing_sources:
            reasons.append("CRITICAL_LINEAGE_MISSING")
            partial = True
            if any(f.startswith("src/agentguard/") for f in missing_sources):
                eligible = set()
    differences = diff_scenarios(before.get("scenarios", []), after.get("scenarios", []))
    ids = [r["scenario_id"] for r in differences if r["classification"] == "UNCHANGED"]
    if any(r["classification"] != "UNCHANGED" for r in differences):
        change("datasets", "DATASET_CHANGED")
        partial = True
    if any(r["classification"] == "MODIFIED" for r in differences):
        reasons.append("SCENARIO_MODIFIED")
    if a.get("execution_mode", UNKNOWN) != b.get("execution_mode", UNKNOWN):
        change("execution_mode", "EXECUTION_MODE_CHANGED")
        eligible = set()
    if a.get("production_models") != b.get("production_models"):
        change("production_models", "PRODUCTION_MODEL_CHANGED")
    if a.get("source") != b.get("source"):
        change("source", "CODE_CHANGED")
    fa, fb = a.get("fingerprints", {}), b.get("fingerprints", {})
    if a.get("datasets") != b.get("datasets"):
        change("datasets", "DATASET_CHANGED")
    if a.get("execution_order_fingerprint") != b.get("execution_order_fingerprint"):
        change("execution_order", "EXECUTION_ORDER_CHANGED")
        eligible -= OPERATIONS
        partial = True
    for key, reason in (("prompt_bundle", "PROMPT_CHANGED"), ("tool_contract", "TOOL_CONTRACT_CHANGED"),
                        ("runtime_policy", "RUNTIME_POLICY_CHANGED"), ("safety_policy", "SAFETY_POLICY_CHANGED"),
                        ("release_gates", "GATE_CHANGED"), ("business_fixtures", "BUSINESS_FIXTURE_CHANGED"),
                        ("production_model_settings", "PRODUCTION_MODEL_CHANGED"), ("slo_spec", "SLO_CHANGED")):
        if fa.get(key) != fb.get(key):
            change(key, reason)
    for key, metrics in (("deterministic_evaluator", DETERMINISTIC), ("semantic_evaluator", SEMANTIC - {"safety_pass_rate"}),
                         ("safety_evaluator", {"safety_pass_rate"}), ("evaluator_prompts", SEMANTIC)):
        if fa.get(key, UNKNOWN) == UNKNOWN or fb.get(key, UNKNOWN) == UNKNOWN:
            reasons.append("CRITICAL_LINEAGE_MISSING")
            eligible -= metrics
            partial = True
        elif fa[key] != fb[key]:
            change(key, "EVALUATOR_CHANGED")
            eligible -= metrics
            partial = True
    role_metrics = {"answer_relevancy": {"answer_relevancy"}, "correctness": {"correctness"},
        "hallucination": {"hallucination"}, "prompt_injection": {"safety_pass_rate"}, "action_claim": {"safety_pass_rate"}}
    for role, metrics in role_metrics.items():
        ja, jb = a.get("judge_models", {}).get(role, {}), b.get("judge_models", {}).get(role, {})
        if any(j.get("resolved", UNKNOWN) == UNKNOWN or UNKNOWN in j.values() for j in (ja, jb)):
            reasons.append("JUDGE_IDENTITY_UNKNOWN")
            eligible -= metrics
            partial = True
        elif ja.get("resolved") == DISABLED or jb.get("resolved") == DISABLED:
            eligible -= metrics
        elif ja != jb:
            change("judge_models." + role, "JUDGE_CHANGED")
            eligible -= metrics
            partial = True
    if a.get("hosting") != b.get("hosting") or fa.get("evaluation_config") != fb.get("evaluation_config"):
        change("measurement_protocol", "MEASUREMENT_PROTOCOL_CHANGED")
        eligible -= OPERATIONS
        partial = True
    if a.get("libraries") != b.get("libraries"):
        change("libraries", "LIBRARY_CHANGED")
        eligible -= OPERATIONS
        partial = True
    if fa.get("aggregation", UNKNOWN) == UNKNOWN or fb.get("aggregation", UNKNOWN) == UNKNOWN:
        reasons.append("CRITICAL_LINEAGE_MISSING")
        eligible = set()
    elif fa["aggregation"] != fb["aggregation"]:
        change("aggregation", "EVALUATOR_CHANGED")
        eligible = set()
    if fa.get("runtime_policy") != fb.get("runtime_policy"):
        eligible -= OPERATIONS
        partial = True
    if any(m.get("execution_mode", UNKNOWN) == UNKNOWN or m.get("scenario_set_fingerprint", UNKNOWN) == UNKNOWN
           for m in (a, b)):
        reasons.append("CRITICAL_LINEAGE_MISSING")
        eligible = set()
    if not ids:
        eligible = set()
    return dict(classification="NOT_COMPARABLE" if not eligible else "PARTIALLY_COMPARABLE" if partial else "DIRECTLY_COMPARABLE",
                reason_codes=sorted(set(reasons)), changed_components=sorted(set(changed)),
                eligible_metrics=sorted(eligible), eligible_scenario_ids=ids if eligible else [], scenario_diff=differences,
                gate_decisions_comparable=bool(eligible) and not partial and
                    fa.get("release_gates", UNKNOWN) != UNKNOWN and fa.get("release_gates") == fb.get("release_gates"))
