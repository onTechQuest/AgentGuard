"""Presentation for explicit M14 run comparisons, never a second scoring engine."""
import re

from src.agentguard.lineage import UNKNOWN
from src.agentguard.metric_registry import METRICS
from src.agentguard.safety_incidents import Redactor
from src.agentguard.synthesis_qualification import identity, number, flag


def safe_label(value, sources):
    if value is None: return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}", value):
        raise ValueError("INVALID_DISPLAY_LABEL")
    # Labels are display-only, but must not echo content from free-text artifacts.
    private = []
    def collect(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if re.fullmatch(r"(?:raw_)?(?:prompt|response|input|output|user_input|model_response|tool_output|tool_payload|payload|exception_message|failures)", key, re.I): private.append(child)
                else: collect(child)
        elif isinstance(node, list):
            for child in node: collect(child)
    collect(sources)
    if Redactor([*sources, {"credentials": private}]).clean(value) != value: raise ValueError("UNSAFE_DISPLAY_LABEL")
    return value


def variant(view):
    manifest = view["manifest"]
    redactor = Redactor([view])
    def safe(value):
        return value if identity(value) and redactor.clean(value) == value else UNKNOWN
    production = manifest["production_models"]
    models = {role: {key: safe(production.get(role, {}).get(key)) if isinstance(production, dict) else UNKNOWN
                     for key in ("resolved", "provider", "immutable_revision")} for role in ("router", "recovery", "synthesis")}
    observed = sorted({(safe(m["model"]), safe(m["immutable_revision"])) for m in view["observed_production_models"]})
    prompt = manifest["fingerprints"].get("prompt_bundle", UNKNOWN)
    if not re.fullmatch(r"[a-f0-9]{64}", prompt) or any(p.startswith(("runtime_contracts", "src/agent/capability_router.py", "src/agent/planning_completeness.py", "src/agent/support_agent.py")) for p in manifest["unavailable_fields"]):
        prompt = UNKNOWN
    return dict(production_models=models, observed_models=[dict(model=m, immutable_revision=r) for m, r in observed],
                prompt_fingerprint=prompt, model_revision_available=all(m["immutable_revision"] != UNKNOWN for m in models.values()))


def change_type(left, right):
    models = [v["production_models"] for v in (left, right)]
    if any(v["prompt_fingerprint"] == UNKNOWN for v in (left, right)) or any(
            m[role][key] == UNKNOWN for m in models for role in m for key in ("resolved", "provider")):
        return UNKNOWN
    # Known resolved identities can be compared without claiming immutable version equality.
    changed = any(models[0][role][key] != models[1][role][key] for role in models[0] for key in ("resolved", "provider"))
    for role in models[0]:
        a, b = models[0][role]["immutable_revision"], models[1][role]["immutable_revision"]
        if a != UNKNOWN and b != UNKNOWN and a != b: changed = True
        elif (a == UNKNOWN) != (b == UNKNOWN) and not changed: return UNKNOWN
    if left["observed_models"] and right["observed_models"]:
        if any(m["model"] == UNKNOWN for v in (left, right) for m in v["observed_models"]): return UNKNOWN
        names = [{m["model"] for m in v["observed_models"]} for v in (left, right)]
        changed |= names[0] != names[1]
        for name in names[0] & names[1]:
            revisions = [{m["immutable_revision"] for m in v["observed_models"] if m["model"] == name} for v in (left, right)]
            if all(UNKNOWN not in r for r in revisions): changed |= revisions[0] != revisions[1]
            elif revisions[0] != revisions[1] and not changed: return UNKNOWN
    prompt = left["prompt_fingerprint"] != right["prompt_fingerprint"]
    return "MODEL_AND_PROMPT_CHANGE" if changed and prompt else "MODEL_CHANGE" if changed else "PROMPT_CHANGE" if prompt else "NO_MODEL_PROMPT_CHANGE"


def metric_rows(rows):
    definitions = {m["metric_id"]: m for m in METRICS}
    result = []
    for row in rows:
        definition = definitions[row["metric_id"]]
        delta, direction = row["delta"], definition["direction"]
        reason = row["exclusion_reason"]
        eligible = row["eligibility"] == "ELIGIBLE"
        if not eligible:
            status = "UNAVAILABLE" if reason == "NO_KNOWN_PAIRED_OBSERVATIONS" else "NOT_COMPARABLE"
        elif delta == 0: status = "UNCHANGED"
        elif direction == "INFORMATIONAL":
            status, reason = "UNAVAILABLE", "REGISTRY_DIRECTION_INFORMATIONAL"
        else:
            improved = delta > 0 if direction == "HIGHER_IS_BETTER" else delta < 0
            status = "IMPROVED" if improved else "REGRESSED"
        result.append(dict(metric_name=row["metric_id"], metric_version=definition["version"], population=row["population"],
                           baseline_value=row["baseline_raw_value"], candidate_value=row["candidate_raw_value"], absolute_delta=delta,
                           relative_delta=delta / abs(row["baseline_raw_value"]) if delta is not None and row["baseline_raw_value"] else None,
                           direction=direction, unit=definition["unit"], comparison_status=status, eligible=eligible, reason=reason,
                           baseline_denominator=row["baseline_denominator"], candidate_denominator=row["candidate_denominator"]))
    return result


def scenario_changes(left, right, metrics):
    """Compare individual existing boolean checks, with matched repetition identity."""
    from src.agentguard.continuous_comparison import metric_value
    boolean_metrics = {m["metric_id"] for m in METRICS if m["family"] in {"DETERMINISTIC", "SAFETY"}}
    eligible = {(r["metric_name"], r["population"]) for r in metrics if r["eligible"] and r["metric_name"] in boolean_metrics}
    a = {(o["scenario_id"], o["repetition"]): o for o in left["observations"] if o["completed"]}
    b = {(o["scenario_id"], o["repetition"]): o for o in right["observations"] if o["completed"]}
    result = dict(new_failures=[], resolved_failures=[], unchanged_failures=[])
    redactor = Redactor([left, right])
    for key in sorted(a.keys() & b.keys()):
        sid, rep = key
        if not identity(sid) or redactor.clean(sid) != sid: continue
        for metric in METRICS:
            if metric["family"] not in {"DETERMINISTIC", "SAFETY"} or (metric["metric_id"], a[key]["population"]) not in eligible: continue
            before, after = metric_value(a[key], metric), metric_value(b[key], metric)
            if type(before) is not bool or type(after) is not bool: continue
            category = "new_failures" if before and not after else "resolved_failures" if not before and after else "unchanged_failures" if not before and not after else None
            if category: result[category].append(dict(scenario_id=sid, repetition=rep, metric_name=metric["metric_id"]))
    return dict(**result, scope="paired_comparable_boolean_checks", available=bool(eligible))


def gate_context(run):
    from src.agentguard.baselines import quality
    from src.agentguard.quality_gate import REQUIREMENTS
    checks = []
    for check in run.get("results", {}).get("aggregate_results", {}).get("quality_result", {}).get("checks", []):
        if not isinstance(check, dict) or check.get("metric") not in REQUIREMENTS: continue
        checks.append(dict(metric=check["metric"], actual=number(check.get("actual")), threshold=number(check.get("threshold")),
                           comparison=check.get("comparison") if check.get("comparison") in {">=", "<="} else None,
                           passed=flag(check.get("passed")), enforced=flag(check.get("enforced"))))
    return dict(decision=quality(run), checks=checks, failed_gate_names=[c["metric"] for c in checks if c["passed"] is False],
                checks_available=bool(checks))


def validate_report(report):
    from src.agentguard.artifact_validation import schema
    schema(report, "model-prompt-comparison.schema.json")
    from src.agentguard.lineage import fingerprint
    from src.agentguard.metric_registry import REGISTRY_VERSION
    expected = fingerprint("model-prompt-comparison-v1", dict(references=report["lineage_references"], metric_registry_version=REGISTRY_VERSION))[:32]
    if report["comparison_id"] != expected: raise ValueError("COMPARISON_ID_MISMATCH")
    if [r["run_id"] for r in report["lineage_references"]] != [report["baseline_run_id"], report["candidate_run_id"]]:
        raise ValueError("COMPARISON_REFERENCE_MISMATCH")
    if Redactor([report]).clean(report) != report: raise ValueError("UNSAFE_COMPARISON_ARTIFACT")
