"""Compact model contract preserves runtime authorization and isolation offline."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents.agent_output import AgentOutputSchema
from agents.usage import Usage

from src.agent import capability_router as routing
from src.agent.request_policy import resolve_request_policy


def binding(capability="order_status", order_id="ORD-7011", clarify=False):
    return {"capability": capability, "order_id": order_id, "needs_clarification": clarify}


def route(monkeypatch, requests, *, confidence=0.99, denied=(), controls=(), typed=False):
    output = dict(capability_requests=requests, confidence=confidence,
                  denied_disclosures=list(denied), control_signals=list(controls))
    if typed:
        output = routing.CapabilityPlanOutput.model_validate(output)
    response = SimpleNamespace(final_output=output, context_wrapper=SimpleNamespace(usage=Usage(requests=1)))
    run = Mock(return_value=response)
    # Exercise the production contract, not the legacy injected-router adapter.
    monkeypatch.setattr(routing.Runner, "run_sync", run)
    result = routing.SemanticCapabilityRouter().route("ORD-7011 and ORD-7022")
    run.assert_called_once()
    assert set(run.call_args.kwargs) == {"max_turns", "run_config"}
    assert run.call_args.kwargs["max_turns"] == 1
    config = run.call_args.kwargs["run_config"]
    assert set(config) == {"model_settings"}
    assert config["model_settings"].retry.max_retries == 0
    assert result.usage is response.context_wrapper.usage
    return result.decision


@pytest.mark.parametrize("requests,expected", [
    ([binding()], {("get_order_status", "ORD-7011")}),
    ([binding("return_eligibility")], {("check_return_eligibility", "ORD-7011")}),
    ([binding(), binding("return_eligibility")], {("check_return_eligibility", "ORD-7011")}),
    ([binding(), binding("return_eligibility", "ORD-7022")],
     {("get_order_status", "ORD-7011"), ("check_return_eligibility", "ORD-7022")}),
    ([binding(), binding(order_id="ORD-7022")],
     {("get_order_status", "ORD-7011"), ("get_order_status", "ORD-7022")}),
    ([binding("unsupported_action", None)], set()),
    ([binding("unknown", None)], set()),
    ([binding(order_id=None)], set()),
    ([binding(clarify=True)], set()),
    ([binding(), binding("return_eligibility", None, True)], {("get_order_status", "ORD-7011")}),
])
def test_compact_bindings_preserve_minimum_cover_and_role_scope(monkeypatch, requests, expected):
    plan = route(monkeypatch, requests, typed=True)
    assert plan.business_capabilities == tuple(dict.fromkeys(item["capability"] for item in requests))
    policy = resolve_request_policy(plan)
    assert {(grant.tool, grant.order_id) for grant in policy.grants} == expected


@pytest.mark.parametrize("confidence", [0.79, 0.0])
def test_low_confidence_still_blocks_tool_access(monkeypatch, confidence):
    policy = resolve_request_policy(route(monkeypatch, [binding()], confidence=confidence))
    assert policy.grants == () and policy.needs_clarification


@pytest.mark.parametrize("controls", [["tool_suppression_attempt"], ["fake_system_authority", "fabricated_tool_result"]])
def test_control_signals_and_disclosure_denial_preserve_permitted_lookup(monkeypatch, controls):
    plan = route(monkeypatch, [binding()], denied=["private_data"], controls=controls)
    assert plan.control_signals == tuple(controls)
    assert plan.denied_disclosures[0].kind == "private_data"
    assert plan.denied_disclosures[0].target is None
    policy = resolve_request_policy(plan)
    assert [(grant.tool, grant.order_id) for grant in policy.grants] == [("get_order_status", "ORD-7011")]
    assert not policy.needs_clarification


def test_empty_disclosure_only_plan_does_not_invent_business_intent(monkeypatch):
    plan = route(monkeypatch, [], denied=["internal_record"])
    assert plan.capability_requests == () and plan.denied_disclosures
    assert resolve_request_policy(plan).grants == ()


def test_unknown_binding_entity_fails_before_authorization(monkeypatch):
    with pytest.raises(routing.CapabilityRoutingError):
        route(monkeypatch, [binding(order_id="ORD-7999")])


def test_live_contract_rejects_legacy_and_malformed_fields(monkeypatch):
    output = routing.SemanticRoutingAnalysis(
        business_capabilities=["order_status"], confidence=0.99,
        business_intent_ambiguous=False, entity_scope="all", control_signals=[],
    ).model_dump()
    run = Mock(return_value=SimpleNamespace(final_output=output))
    monkeypatch.setattr(routing.Runner, "run_sync", run)
    with pytest.raises(routing.CapabilityRoutingError, match="ValidationError"):
        routing.SemanticCapabilityRouter().route("ORD-7011")
    run.assert_called_once()


def test_compact_schema_has_no_global_summaries_or_disclosure_targets():
    schema = AgentOutputSchema(routing.CapabilityPlanOutput).json_schema()
    assert set(schema["properties"]) == {
        "capability_requests", "confidence", "control_signals", "denied_disclosures",
    }
    assert schema["additionalProperties"] is False
    assert "target" not in json.dumps(schema)
    assert schema["$defs"]["CapabilityRequest"]["additionalProperties"] is False
    assert set(schema["$defs"]["CapabilityRequest"]["required"]) == {
        "capability", "order_id", "needs_clarification",
    }
