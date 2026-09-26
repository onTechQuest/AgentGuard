"""Planning review candidates, not tool grants or natural-language intent rules."""
from src.agentguard.tool_policy import CAPABILITIES, MINIMUM_CONFIDENCE


def assess_bindings(plan, capabilities=CAPABILITIES):
    """A supported, bound read with low intent confidence can merit a review.

Confident clarification and unresolved targets remain clarification. An ID by
itself never creates a candidate. ACTIONABLE denotes planning viability only;
the deterministic resolver still owns authority and authoritative tool coverage.
"""
    rows = []
    for index, request in enumerate(plan.capability_requests or ()):
        capability = capabilities.get(request.capability)
        if capability is None or request.capability == "unknown":
            state, reason = "UNKNOWN", "UNKNOWN_CAPABILITY"
        elif not capability.permits_tools:
            state, reason = "UNSUPPORTED", "CAPABILITY_PERMITS_NO_TOOL"
        elif request.order_id is None or request.order_id not in plan.extracted_entities.order_ids:
            state, reason = "LEGITIMATE_CLARIFICATION", "UNRESOLVED_TARGET"
        elif plan.ambiguity in {"missing_order_id", "multiple_order_ids"}:
            state, reason = "LEGITIMATE_CLARIFICATION", "UNRESOLVED_TARGET_SCOPE"
        elif plan.confidence < MINIMUM_CONFIDENCE:
            state, reason = "REVIEWABLE_NON_ACTIONABLE", "SUPPORTED_BOUND_READ_WITH_LOW_CONFIDENCE"
        elif request.needs_clarification or plan.ambiguity == "business_intent":
            state, reason = "LEGITIMATE_CLARIFICATION", "EXPLICIT_INTENT_UNCERTAINTY"
        else:
            state, reason = "ACTIONABLE", "CLEAR_SUPPORTED_BINDING"
        rows.append({"binding_index": index, "state": state, "reason": reason})
    return rows


def reviewable_plan(plan, assessment):
    """Conservative scope: one identified business outcome and one parsed target.

Do not replace mixed work, choose among competing capabilities or infer roles
across multiple entities. Identical duplicate bindings do not expand the scope.
"""
    return bool(assessment) and all(r["state"] == "REVIEWABLE_NON_ACTIONABLE" for r in assessment) and (
        len(plan.extracted_entities.order_ids) == 1 and
        len({(r.capability, r.order_id) for r in plan.capability_requests}) == 1)
