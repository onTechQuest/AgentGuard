"""Pure runtime policy tests; no agent, model, dataset or business lookup needed."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from src.agentguard.tool_policy import (
    CAPABILITIES, TOOL_REGISTRY, Capability, CapabilityIntent, ToolCapability, resolve_tool_policy,
)


def intent(*capabilities, **overrides):
    return CapabilityIntent(**{
        "capabilities": list(capabilities), "order_ids": ["ORD-7001"], "confidence": 0.99,
        "needs_clarification": False, **overrides,
    })


@pytest.mark.parametrize("capabilities, allowed", [
    (["order_status"], ("get_order_status",)),
    (["return_eligibility"], ("check_return_eligibility",)),
    (["order_status", "return_eligibility"], ("check_return_eligibility",)),
    (["unsupported_action"], ()),
    (["unsupported_action", "order_status"], ("get_order_status",)),
])
def test_minimal_authoritative_selection(capabilities, allowed):
    policy = resolve_tool_policy(intent(*capabilities))
    assert policy.allowed_tools == policy.preferred_tools == allowed
    assert not policy.needs_clarification
    assert set(policy.prohibited_tools) == {tool.name for tool in TOOL_REGISTRY} - set(allowed)
    assert not policy.unresolved_capabilities


def test_redundant_status_tool_is_excluded_from_combined_request():
    policy = resolve_tool_policy(intent("return_eligibility", "order_status"))
    assert policy.redundant_tools == ("get_order_status",)
    assert policy.prohibited_tools == ("get_order_status",)


def test_return_result_already_provides_all_status_context():
    policy = resolve_tool_policy(intent("return_eligibility"))
    assert policy.redundant_tools == ("get_order_status",)


@pytest.mark.parametrize("requested", [["unknown"], ["unregistered_capability"], ["unknown", "order_status"]])
def test_unknown_capability_does_not_grant_partial_access(requested):
    policy = resolve_tool_policy(intent(*requested))
    assert policy.allowed_tools == ()
    assert policy.needs_clarification and policy.unresolved_capabilities


@pytest.mark.parametrize("changes", [
    {"confidence": 0.79}, {"needs_clarification": True}, {"order_ids": []},
])
def test_uncertain_or_incomplete_routing_exposes_no_tools(changes):
    policy = resolve_tool_policy(intent("order_status", **changes))
    assert policy.allowed_tools == ()
    assert policy.needs_clarification


def test_confidence_boundary_and_capability_without_order_id():
    assert resolve_tool_policy(intent("order_status", confidence=0.8)).allowed_tools == ("get_order_status",)
    assert not resolve_tool_policy(intent("unsupported_action", order_ids=[])).needs_clarification


def test_multiple_identifiers_do_not_require_multiple_tool_types():
    policy = resolve_tool_policy(intent("return_eligibility", order_ids=["ORD-7001", "ORD-7002"], entity_scope="all"))
    assert policy.allowed_tools == ("check_return_eligibility",)


def test_multiple_entities_require_explicit_scope_in_policy_itself():
    policy = resolve_tool_policy(intent("order_status", order_ids=["ORD-7001", "ORD-7002"]))
    assert not policy.allowed_tools
    assert policy.needs_clarification


def test_control_plane_tool_suppression_cannot_remove_business_authority():
    policy = resolve_tool_policy(intent("order_status", control_signals=["tool_suppression_attempt"]))
    assert policy.allowed_tools == ("get_order_status",)
    assert policy.control_signals == ("tool_suppression_attempt",)
    assert "cannot alter" in policy.reason


def test_registry_and_input_order_do_not_change_the_decision():
    assert resolve_tool_policy(intent("order_status", "return_eligibility")) == resolve_tool_policy(
        intent("return_eligibility", "order_status"), registry=tuple(reversed(TOOL_REGISTRY)),
    )


def test_supporting_a_capability_without_authority_is_insufficient():
    tools = tuple(replace(tool, authoritative_for=frozenset()) for tool in TOOL_REGISTRY)
    policy = resolve_tool_policy(intent("return_eligibility"), registry=tools)
    assert not policy.allowed_tools and policy.needs_clarification


def test_authority_without_required_facts_is_insufficient():
    tools = tuple(replace(tool, provides=frozenset()) for tool in TOOL_REGISTRY)
    assert not resolve_tool_policy(intent("order_status"), registry=tools).allowed_tools


def test_incomplete_coverage_does_not_allow_a_partial_tool_set():
    policy = resolve_tool_policy(intent("order_status", "return_eligibility"), registry=TOOL_REGISTRY[:1])
    assert not policy.allowed_tools and policy.needs_clarification


def test_new_capability_and_tool_need_no_resolver_branch():
    capabilities = dict(CAPABILITIES, invoice=Capability("invoice", "Read an invoice.", frozenset({"invoice"})))
    invoice_tool = ToolCapability("read_invoice", frozenset({"invoice"}), frozenset({"invoice"}), frozenset({"invoice"}))
    policy = resolve_tool_policy(intent("invoice", "order_status"), registry=(*TOOL_REGISTRY, invoice_tool), capabilities=capabilities)
    assert policy.allowed_tools == ("get_order_status", "read_invoice")


def test_exact_cover_avoids_greedy_selection_trap():
    capabilities = {name: Capability(name, name, frozenset({name}), requires_order_id=False) for name in "abcdef"}

    def tool(name, letters):
        facts = frozenset(letters)
        return ToolCapability(name, facts, facts, facts)

    # Greedy a_large would need both d_e and e_f after taking four capabilities.
    registry = [tool("a_large", "abcd"), tool("b_left", "abe"), tool("c_right", "cdf"),
                tool("d_e", "e"), tool("e_f", "f")]
    policy = resolve_tool_policy(intent(*"abcdef", order_ids=[]), registry=registry, capabilities=capabilities)
    assert policy.allowed_tools == ("b_left", "c_right")


def test_equal_tools_have_stable_name_tie_break():
    original = TOOL_REGISTRY[0]
    registry = [replace(original, name="z_status"), replace(original, name="a_status")]
    assert resolve_tool_policy(intent("order_status"), registry=registry).allowed_tools == ("a_status",)


def test_duplicate_tool_names_and_invalid_authority_are_configuration_errors():
    with pytest.raises(ValueError, match="unique"):
        resolve_tool_policy(intent("order_status"), registry=[TOOL_REGISTRY[0]] * 2)
    with pytest.raises(ValueError, match="consistent authority"):
        resolve_tool_policy(intent("order_status"), registry=[replace(TOOL_REGISTRY[0], supports=frozenset())])


@pytest.mark.parametrize("updates", [
    {"capabilities": []}, {"capabilities": "order_status"}, {"order_ids": [""]},
    {"confidence": -1.0}, {"confidence": 1.1}, {"confidence": float("nan")},
    {"confidence": "0.99"}, {"needs_clarification": "false"}, {"scenario_id": "irrelevant"},
])
def test_invalid_intent_schema_is_rejected(updates):
    with pytest.raises(ValidationError):
        intent("order_status", **updates)


def test_policy_cannot_depend_on_mutated_invalid_router_output():
    request = intent("return_eligibility")
    request.capabilities.clear()
    with pytest.raises(ValidationError):
        resolve_tool_policy(request)
