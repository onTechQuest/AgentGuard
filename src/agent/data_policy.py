"""Runtime disclosure projection; business records and decisions remain untouched."""

from collections.abc import Iterable, Mapping

from src.agentguard.tool_policy import CAPABILITIES


def permitted_fields(capabilities: Iterable[str]) -> frozenset[str]:
    fields = frozenset()
    for name in capabilities:
        capability = CAPABILITIES.get(name)
        if capability is None or capability.exposed_fields is None:
            raise ValueError("Capability has no disclosure contract")
        fields |= capability.exposed_fields
    return fields


def project_tool_result(result: Mapping, capabilities: Iterable[str]) -> dict:
    """Copy only declared scalar facts and the public order envelope.

    Unknown fields and nested values fail closed. No conversion, inferred facts,
    business-rule recomputation or null normalization occurs here.
    """
    allowed = permitted_fields(capabilities)

    def scalars(value):
        return {key: item for key, item in value.items()
                if key in allowed and (item is None or type(item) in (str, bool, int, float))}

    projected = scalars(result)
    if allowed and isinstance(result.get("order"), Mapping):
        projected["order"] = scalars(result["order"])
    return projected
