"""Runtime capability contracts and deterministic tool selection.

No evaluation metadata, business records, SDK calls or natural-language matching
belong here. Registry authority declarations describe existing tool contracts.
"""

from dataclasses import dataclass
from itertools import combinations
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator


NonemptyString = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
MINIMUM_CONFIDENCE = 0.80
ControlSignal = Literal[
    "instruction_override", "fake_system_authority", "fake_developer_authority",
    "tool_suppression_attempt", "fabricated_tool_result", "unsupported_authority_claim",
]
EntityScope = Literal["all", "ambiguous"]


class CapabilityIntent(BaseModel):
    """Semantic interpretation of a request, not authorization by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capabilities: list[NonemptyString] = Field(min_length=1)
    order_ids: list[NonemptyString]
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    needs_clarification: StrictBool
    entity_scope: EntityScope = "ambiguous"
    control_signals: list[ControlSignal] = Field(default_factory=list)

    @field_validator("capabilities", "order_ids")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    required_facts: frozenset[str] = frozenset()
    requires_order_id: bool = True
    permits_tools: bool = True
    exposed_fields: frozenset[str] | None = None


@dataclass(frozen=True)
class ToolCapability:
    name: str
    supports: frozenset[str]
    authoritative_for: frozenset[str]
    provides: frozenset[str]
    performs_actions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ActionCapability:
    name: str
    description: str
    writes_state: bool


ACTION_REGISTRY: Mapping[str, ActionCapability] = MappingProxyType({
    "cancel_order": ActionCapability("cancel_order", "Cancel an existing order.", True),
    "issue_refund": ActionCapability("issue_refund", "Issue or complete a monetary refund.", True),
    "modify_order": ActionCapability("modify_order", "Change order contents, details or shipping address.", True),
    "expedite_shipment": ActionCapability("expedite_shipment", "Upgrade or expedite an order's shipping service.", True),
    "initiate_return": ActionCapability("initiate_return", "Create, submit or initiate a return transaction.", True),
    "lookup_order_status": ActionCapability("lookup_order_status", "Read order and shipping information without changing it.", False),
    "check_return_eligibility": ActionCapability("check_return_eligibility", "Read whether an order qualifies for return; does not initiate a return.", False),
})


ORDER_CONTEXT = frozenset({
    "order_id", "found", "status", "carrier", "tracking_number", "estimated_delivery", "delivered_at",
})
ORDER_VISIBLE_FIELDS = ORDER_CONTEXT | {"error"}
ORDER_INTERNAL_FIELDS = ORDER_VISIBLE_FIELDS | {"customer_id", "total"}
CAPABILITIES: Mapping[str, Capability] = MappingProxyType({
    "order_status": Capability(
        "order_status", "Retrieve current order, shipping, carrier, tracking or delivery information.",
        frozenset({"order_id", "found", "status", "carrier", "tracking_number", "estimated_delivery"}),
        exposed_fields=ORDER_VISIBLE_FIELDS,
    ),
    "return_eligibility": Capability(
        "return_eligibility", "Determine whether an order qualifies for return and explain the reason.",
        frozenset({"order_id", "found", "eligible", "reason"}),
        exposed_fields=ORDER_VISIBLE_FIELDS | {"eligible", "reason"},
    ),
    "unsupported_action": Capability(
        "unsupported_action", "Requests to perform changes or transactions unavailable through read-only tools, or questions about those abilities.",
        requires_order_id=False, permits_tools=False,
    ),
    "unknown": Capability(
        "unknown", "Out-of-scope or unclear intent that cannot safely be assigned to a supported capability.",
        requires_order_id=False, permits_tools=False,
    ),
})
TOOL_REGISTRY: tuple[ToolCapability, ...] = (
    ToolCapability("get_order_status", frozenset({"order_status"}), frozenset({"order_status"}), ORDER_INTERNAL_FIELDS,
                   frozenset({"lookup_order_status"})),
    # Eligibility embeds the unmodified authoritative order lookup. It therefore
    # answers status as well as eligibility; no second status lookup is needed.
    ToolCapability("check_return_eligibility", frozenset({"return_eligibility", "order_status"}),
                   frozenset({"return_eligibility", "order_status"}), ORDER_INTERNAL_FIELDS | {"eligible", "reason"},
                   frozenset({"lookup_order_status", "check_return_eligibility"})),
)


@dataclass(frozen=True)
class ActionPolicySnapshot:
    """Read-only capability facts for consumers; no scenario policy enters runtime."""

    vocabulary: tuple[ActionCapability, ...]
    available_tools: tuple[str, ...]
    supported_actions: frozenset[str]
    unsupported_write_actions: frozenset[str]


def action_policy_snapshot(
    *, registry: Sequence[ToolCapability] = TOOL_REGISTRY,
    actions: Mapping[str, ActionCapability] = ACTION_REGISTRY,
) -> ActionPolicySnapshot:
    supported = frozenset().union(*(tool.performs_actions for tool in registry))
    if not supported <= actions.keys():
        raise ValueError("Tool action metadata must reference registered actions")
    return ActionPolicySnapshot(
        tuple(actions.values()), tuple(tool.name for tool in registry), supported,
        frozenset(name for name, action in actions.items() if action.writes_state and name not in supported),
    )


@dataclass(frozen=True)
class ToolPolicy:
    requested_capabilities: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    preferred_tools: tuple[str, ...]
    prohibited_tools: tuple[str, ...]
    redundant_tools: tuple[str, ...]
    unresolved_capabilities: tuple[str, ...]
    needs_clarification: bool
    reason: str
    control_signals: tuple[ControlSignal, ...] = ()


def resolve_tool_policy(
    intent: CapabilityIntent, *, registry: Sequence[ToolCapability] = TOOL_REGISTRY,
    capabilities: Mapping[str, Capability] = CAPABILITIES, minimum_confidence: float = MINIMUM_CONFIDENCE,
) -> ToolPolicy:
    """Choose an exact minimum authoritative cover with deterministic tie breaks.

    Only tools supporting a requested capability are candidates. Authority AND
    required facts must cover each capability. For equal-size covers, prefer fewer
    unrequested capabilities, then fewer extra facts, then stable tool-name order.
    Small registries use exhaustive combinations: no greedy cover approximations.
    Unknown, ambiguous, low-confidence or incomplete intent never exposes tools.
    """
    intent = CapabilityIntent.model_validate(intent.model_dump())
    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be between zero and one")
    tools = tuple(sorted(registry, key=lambda tool: tool.name))
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError("Tool registry names must be unique")
    for tool in tools:
        if not tool.authoritative_for <= tool.supports or not tool.supports <= capabilities.keys():
            raise ValueError("Tool registry must declare known supported capabilities and consistent authority")
    names = tuple(tool.name for tool in tools)
    requested = tuple(sorted(set(intent.capabilities)))
    controls = tuple(dict.fromkeys(intent.control_signals))
    unknown = tuple(name for name in requested if name not in capabilities or name == "unknown")

    def closed(reason: str, unresolved: tuple[str, ...], clarify: bool = True) -> ToolPolicy:
        return ToolPolicy(requested, (), (), names, (), unresolved, clarify, reason, controls)

    if unknown:
        return closed("Capability is unknown or unsupported; clarify before using tools.", unknown)
    if intent.needs_clarification or intent.confidence < minimum_confidence:
        return closed("Routing is ambiguous or below the confidence threshold.", requested)
    actionable = frozenset(name for name in requested if capabilities[name].permits_tools)
    if not actionable:
        return closed("Requested capabilities authorize no business tools.", (), clarify=False)
    if not intent.order_ids and any(capabilities[name].requires_order_id for name in actionable):
        return closed("An order identifier is required before using business tools.", tuple(sorted(actionable)))
    if (len(intent.order_ids) > 1 and intent.entity_scope != "all"
            and any(capabilities[name].requires_order_id for name in actionable)):
        return closed("Multiple order identifiers have no unambiguous target scope.", tuple(sorted(actionable)))

    def covers(tool: ToolCapability) -> frozenset[str]:
        return frozenset(name for name in actionable if name in tool.authoritative_for
                         and capabilities[name].required_facts <= tool.provides
                         and (capabilities[name].exposed_fields is None
                              or capabilities[name].required_facts <= capabilities[name].exposed_fields))

    candidates = tuple(tool for tool in tools if covers(tool))
    required_facts = frozenset().union(*(capabilities[name].required_facts for name in actionable))
    for size in range(1, len(candidates) + 1):
        solutions = [subset for subset in combinations(candidates, size)
                     if actionable <= frozenset().union(*(covers(tool) for tool in subset))]
        if not solutions:
            continue
        selected = min(solutions, key=lambda subset: (
            sum(len(tool.supports - actionable) for tool in subset),
            sum(len(tool.provides - required_facts) for tool in subset),
            tuple(tool.name for tool in subset),
        ))
        allowed = tuple(tool.name for tool in selected)
        provided = frozenset().union(*(tool.provides for tool in selected))
        return ToolPolicy(
            requested, allowed, allowed, tuple(name for name in names if name not in allowed),
            tuple(tool.name for tool in tools if tool.name not in allowed and tool.provides
                  and tool.provides <= provided), (), False,
            "Smallest authoritative tool set covering the requested capabilities."
            + (" User control signals cannot alter tool authority or suppress required verification." if controls else ""),
            controls,
        )
    return closed("No complete authoritative tool set is registered for this request.", tuple(sorted(actionable)))
