"""Request-local, allowlisted observations. Never authorize or recover work.

Detailed evidence is transient on ordinary successful requests. No input text,
model prose, disclosure payloads, tool results, or exception messages are copied.
"""
from copy import deepcopy
import re

from src.agent import telemetry
from src.agentguard.tool_policy import CAPABILITIES, TOOL_REGISTRY


TOOLS = frozenset(t.name for t in TOOL_REGISTRY)
CONTROLS = frozenset({"instruction_override", "fake_system_authority", "fake_developer_authority",
                      "tool_suppression_attempt", "fabricated_tool_result", "unsupported_authority_claim"})


def target(value):
    if not isinstance(value, str) or not re.fullmatch(r"ORD-[0-9]+", value, re.I):
        return None
    record = telemetry._request.get()
    if record is None:
        return None
    if not hasattr(record, "_decision_targets"):
        record._decision_targets = {}
    targets = record._decision_targets
    normalized = value.upper()
    if normalized not in targets:
        targets[normalized] = f"entity_{len(targets) + 1}"
    return targets[normalized]


def binding(value):
    return {"capability": value.capability if value.capability in CAPABILITIES else "<unregistered>",
            "target": target(value.order_id), "needs_clarification": value.needs_clarification,
            "capability_outcome": ("UNKNOWN" if value.capability not in CAPABILITIES or value.capability == "unknown"
                                   else "SUPPORTED" if CAPABILITIES[value.capability].permits_tools else "UNSUPPORTED")}


def plan(value):
    recognized = [target(t) for t in value.extracted_entities.order_ids if target(t)]
    return {"bindings": [binding(b) for b in value.capability_requests] if value.capability_requests is not None else None,
            "recognized_targets": recognized,
            "confidence": value.confidence, "ambiguity": value.ambiguity,
            "control_signals": [c for c in value.control_signals if c in CONTROLS]}


def _chain():
    record = telemetry._request.get()
    if record is None:
        return None
    if not hasattr(record, "_decision_chain"):
        record._decision_chain = {"schema_version": 1, "entity_reference_scheme": "request_local_extraction_order",
                                  "router": None, "completeness": None,
                                  "policy": None, "execution_plan": None}
    return record._decision_chain


@telemetry.best_effort
def router(value):
    chain = _chain()
    if chain is not None:
        chain["router"] = plan(value)


@telemetry.best_effort
def completeness(value):
    chain = _chain()
    if chain is not None:
        chain["completeness"] = {
            "input": plan(value.primary_plan), "decision": value.completeness_code,
            "primary_actionability": value.primary_actionability,
            "final_actionability": value.final_actionability,
            "recovered_confidence": getattr(value.recovery_plan, "confidence", None),
            "review_triggered": value.completeness_review_triggered,
            "recovery_entered": value.recovery_attempted, "recovery_count": value.recovery_count,
            "recovery_result": ("FAILED" if value.recovery_error else
                                "COMPLETED" if value.recovery_plan is not None else "NOT_EXECUTED"),
            "recovered_bindings": ([binding(b) for b in value.recovery_plan.capability_requests]
                                   if value.recovery_plan is not None else None),
            "final": plan(value.final_plan), "plan_source": value.plan_source,
        }


@telemetry.best_effort
def policy_start(value, minimum_confidence):
    chain = _chain()
    if chain is not None:
        chain["policy"] = {"candidate_plan": plan(value), "minimum_confidence": minimum_confidence,
                           "binding_results": [], "result": "IN_PROGRESS", "authorized_grants": []}


@telemetry.best_effort
def policy_binding(value, result, reason):
    chain = _chain()
    if chain is not None and chain["policy"] is not None:
        chain["policy"]["binding_results"].append({**binding(value), "result": result, "reason": reason})


def grants(values):
    return [{"tool": g.tool if g.tool in TOOLS else "<unregistered>", "target": target(g.order_id),
             "capabilities": [c for c in g.capabilities if c in CAPABILITIES]} for g in values]


@telemetry.best_effort
def policy_result(value, reason=None):
    chain = _chain()
    if chain is not None and chain["policy"] is not None:
        chain["policy"].update(authorized_grants=grants(value.grants),
            result="GRANTED" if value.grants else "CLARIFICATION_REQUIRED" if value.needs_clarification else "NO_GRANTS",
            needs_clarification=value.needs_clarification, reason=reason)


@telemetry.best_effort
def execution(value):
    chain = _chain()
    if chain is not None:
        chain["execution_plan"] = {"authorized_grants": grants(value.authorized_bindings),
            "required_operations": [{"tool": o.tool if o.tool in TOOLS else "<unregistered>",
                                     "arguments": {"order_id": target(dict(o.arguments).get("order_id"))}}
                                    for o in value.operations if o.mode == "required"]}


def classify(evidence, *, expected_business_work=None):
    """Observations plus optional *evaluation* contract; never infer intent from IDs.

ROUTER_OMISSION means a binding was omitted relative to the supplied contract,
not an independently proven semantic error. No-work needs affirmative evidence.
"""
    result = []
    router = evidence.get("router") or {}
    review = evidence.get("completeness") or {}
    policy = evidence.get("policy") or {}
    execution = evidence.get("execution_plan")
    if expected_business_work is True and router.get("bindings") == []:
        result.append("ROUTER_OMISSION")
    if review and not review.get("review_triggered"):
        result.append("COMPLETENESS_SKIP")
    if review.get("recovery_admission", {}).get("admitted") is False:
        result.append("RECOVERY_NOT_ADMITTED")
    if any(b["result"] == "DENIED" for b in policy.get("binding_results", [])):
        result.append("POLICY_DENIAL")
    if policy.get("needs_clarification"):
        result.append("CLARIFICATION_REQUIRED")
    if execution is not None and not execution["required_operations"]:
        if execution["authorized_grants"] or policy.get("authorized_grants"):
            result.append("AUTHORIZED_EMPTY_PLAN_BUG")
        elif not policy.get("needs_clarification") and (expected_business_work is False or
                review.get("recovered_bindings") == []):
            result.append("LEGITIMATE_NO_WORK")
    return result


@telemetry.best_effort
def finish(record, error):
    chain = getattr(record, "_decision_chain", None)
    if chain is None:
        return
    review = chain.get("completeness")
    if review is not None:
        admission = record.recovery_admission
        review["recovery_admission"] = ({"admitted": admission.admitted,
            "reason": admission.denial_reason.value if admission.denial_reason else None,
            "remaining_ms": admission.remaining_budget_ms, "minimum_ms": admission.minimum_work_ms,
            "reserve_ms": admission.required_downstream_reserve_ms} if admission is not None else
            {"admitted": None, "reason": "NOT_OBSERVED" if review["review_triggered"] else "NOT_REQUESTED"})
    execution = chain.get("execution_plan")
    empty = execution is not None and not execution["required_operations"]
    categories = classify(chain)
    if empty:
        policy = chain.get("policy") or {}
        execution["empty_reason"] = (
            "AUTHORIZED_EMPTY_PLAN_BUG" if "AUTHORIZED_EMPTY_PLAN_BUG" in categories else
            "POLICY_DENIAL" if "POLICY_DENIAL" in categories else
            "CLARIFICATION_REQUIRED" if policy.get("needs_clarification") else
            "LEGITIMATE_NO_WORK" if "LEGITIMATE_NO_WORK" in categories else
            "EMPTY_BINDINGS_UNASSESSED")
    elif execution is not None:
        execution["empty_reason"] = None
    chain["categories"] = categories
    record.decision_summary = {"categories": categories, "empty_plan": empty}
    if error is not None or empty or getattr(record, "_diagnostic_mode", False):
        chain["retention_reason"] = "FAILURE" if error is not None else "DIAGNOSTIC" if getattr(record, "_diagnostic_mode", False) else "EMPTY_PLAN"
        record.decision_evidence = deepcopy(chain)
