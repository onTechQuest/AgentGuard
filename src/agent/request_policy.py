"""Compose independent request components into target-scoped tool grants."""

from dataclasses import dataclass
from src.agent import decision_evidence

from src.agent.capability_router import CapabilityRequest, RequestPlan
from src.agentguard.tool_policy import CAPABILITIES, TOOL_REGISTRY, MINIMUM_CONFIDENCE, CapabilityIntent, resolve_tool_policy


@dataclass(frozen=True)
class ToolGrant:
    tool: str
    order_id: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class RequestToolPolicy:
    grants: tuple[ToolGrant, ...]
    needs_clarification: bool

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(sorted({grant.tool for grant in self.grants}))


def resolve_request_policy(plan: RequestPlan) -> RequestToolPolicy:
    """Authorize clear components independently; disclosure requests grant nothing.

    Each target gets its own minimum authoritative cover. A tool for one target
    never authorizes the same operation on another mentioned order.
    """
    decision_evidence.policy_start(plan, MINIMUM_CONFIDENCE)
    if plan.capability_requests is None:
        # Older injected routers express uniform all-entity intent. Preserve their
        # existing ambiguity checks rather than guessing a role assignment.
        legacy = resolve_tool_policy(plan.intent)
        if not legacy.allowed_tools:
            result = RequestToolPolicy((), legacy.needs_clarification)
            decision_evidence.policy_result(result, "LEGACY_NO_GRANT")
            return result
        requests = [CapabilityRequest(capability=name, order_id=order_id)
                    for order_id in plan.extracted_entities.order_ids for name in plan.business_capabilities]
    else:
        requests = list(plan.capability_requests)

    if not MINIMUM_CONFIDENCE <= plan.confidence <= 1.0:
        for request in requests:
            decision_evidence.policy_binding(request, "DENIED", "CONFIDENCE_BELOW_THRESHOLD")
        result = RequestToolPolicy((), True)
        decision_evidence.policy_result(result, "CONFIDENCE_OUTSIDE_ALLOWED_RANGE")
        return result
    targets: dict[str, set[str]] = {}
    clarify = False
    for value in requests:
        request = CapabilityRequest.model_validate(value.model_dump())
        if request.order_id is not None and request.order_id not in plan.extracted_entities.order_ids:
            raise ValueError("Capability binding references an unparsed entity")
        capability = CAPABILITIES.get(request.capability)
        if capability is None or request.capability == "unknown":
            decision_evidence.policy_binding(request, "DENIED", "UNKNOWN_CAPABILITY")
            clarify = True
            continue
        if not capability.permits_tools:
            decision_evidence.policy_binding(request, "DENIED", "CAPABILITY_PERMITS_NO_TOOL")
            continue
        if request.needs_clarification or request.order_id is None:
            decision_evidence.policy_binding(request, "CLARIFICATION", "AMBIGUOUS_BINDING" if request.needs_clarification else "MISSING_TARGET")
            clarify = True
            continue
        targets.setdefault(request.order_id, set()).add(request.capability)

    grants = []
    registry = {tool.name: tool for tool in TOOL_REGISTRY}
    for order_id, names in targets.items():
        policy = resolve_tool_policy(CapabilityIntent(
            capabilities=sorted(names), order_ids=[order_id], confidence=plan.confidence,
            needs_clarification=False, control_signals=list(plan.control_signals),
        ))
        clarify |= policy.needs_clarification
        for request in requests:
            if request.order_id == order_id and request.capability in names and not request.needs_clarification:
                decision_evidence.policy_binding(request, "GRANTED" if policy.allowed_tools else "DENIED",
                                                 "AUTHORITATIVE_COVER" if policy.allowed_tools else "NO_AUTHORITATIVE_COVER")
        for tool in policy.allowed_tools:
            grants.append(ToolGrant(tool, order_id, tuple(sorted(names & registry[tool].authoritative_for))))
    result = RequestToolPolicy(tuple(grants), clarify)
    decision_evidence.policy_result(result)
    return result
