"""Bounded semantic reassessment with unchanged deterministic authorization."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

from agents.usage import Usage
from agents.agent_output import AgentOutputSchema
from agents.exceptions import ModelBehaviorError
from pydantic import ValidationError
import pytest

from src.agent import support_agent as support
from src.agent.binding_actionability import assess_bindings
from src.agent.capability_router import SemanticCapabilityRouter
from src.agent.execution_plan import build_execution_plan
from src.agent.planning_completeness import (
    ActionabilityRecoveryPlan, PlanningCompletenessError, RecoveryPlan, RecoveryResult,
    SemanticRecoveryPlanner, validate_planning,
)
from src.agent.request_budget import RequestBudget, RequestBudgetRejected
from src.agent.request_policy import resolve_request_policy
from src.agentguard.tool_policy import MINIMUM_CONFIDENCE


TEXT = 'Please check order "  ORD-9011  ".'


def request(capability="order_status", target="ORD-9011", clarify=False):
    return dict(capability=capability, order_id=target, needs_clarification=clarify)


def route(bindings=None, confidence=.62, text=TEXT, controls=()):
    runner = Mock(return_value=SimpleNamespace(final_output={
        "capability_requests": bindings if bindings is not None else [request(clarify=True)],
        "confidence": confidence, "control_signals": list(controls), "denied_disclosures": []},
        context_wrapper=SimpleNamespace(usage=Usage(requests=1, total_tokens=10))))
    return SemanticCapabilityRouter(run=runner).route(text)


def review(routed=None, payload=None):
    routed = routed or route()
    planner = Mock(recover=Mock(return_value=RecoveryResult(
        payload if payload is not None else {"capability_requests": [request()], "confidence": .95},
        Usage(requests=1, total_tokens=20))))
    result = validate_planning(TEXT, routed, recovery_factory=lambda: planner)
    return result, planner


def test_padded_quoted_target_low_confidence_binding_gets_semantic_review():
    result, planner = review()
    assert result.primary_actionability[0]["state"] == "REVIEWABLE_NON_ACTIONABLE"
    assert result.completeness_code == "REVIEW_NON_ACTIONABLE_BINDINGS"
    assert result.completeness_review_triggered and result.recovery_count == 1
    planner.recover.assert_called_once()
    assert result.primary_plan.confidence == .62 and result.primary_plan.capability_requests[0].needs_clarification
    assert result.final_plan.confidence == .95 and result.final_plan.ambiguity == "none"
    assert result.final_actionability[0]["state"] == "ACTIONABLE"
    policy = resolve_request_policy(result.final_plan)
    assert policy.grants[0].tool == "get_order_status"
    plan = build_execution_plan(policy)
    assert len(plan.operations) == 1 and plan.operations[0].mode == "required"
    assert dict(plan.operations[0].arguments) == {"order_id": "ORD-9011"}
    assert result.usage.requests == 2


@pytest.mark.parametrize("bindings,confidence,text,state", [
    ([request(clarify=True)], .99, TEXT, "LEGITIMATE_CLARIFICATION"),
    ([request(target=None)], .62, TEXT, "LEGITIMATE_CLARIFICATION"),
    ([request(target=None)], .62, "Please check my order", "LEGITIMATE_CLARIFICATION"),
    ([request(target=None)], .62, "ORD-9011 or ORD-9022?", "LEGITIMATE_CLARIFICATION"),
    ([request("unsupported_action")], .62, TEXT, "UNSUPPORTED"),
    ([request("unknown")], .62, TEXT, "UNKNOWN"),
    ([request()], .80, TEXT, "ACTIONABLE"),
    ([request()], .99, TEXT, "ACTIONABLE"),
])
def test_protected_or_actionable_bindings_do_not_instantiate_recovery(bindings, confidence, text, state):
    routed = route(bindings, confidence, text)
    factory = Mock(side_effect=AssertionError("Unnecessary recovery"))
    result = validate_planning(text, routed, recovery_factory=factory)
    assert result.primary_actionability[0]["state"] == state
    assert result.final_plan is routed.decision
    factory.assert_not_called()
    if state != "ACTIONABLE":
        assert not resolve_request_policy(result.final_plan).grants


@pytest.mark.parametrize("bindings,text", [
    ([request(clarify=True)], "ORD-9011 or ORD-9022?"),
    ([request(clarify=True), request("return_eligibility", clarify=True)], TEXT),
    ([request(clarify=True), request("unknown")], TEXT),
    ([], "An example reference ORD-9011"),
])
def test_no_entity_only_or_competing_intent_recovery(bindings, text):
    factory = Mock(side_effect=AssertionError("Must not choose among targets or capabilities"))
    result = validate_planning(text, route(bindings, text=text), recovery_factory=factory)
    factory.assert_not_called()
    assert not resolve_request_policy(result.final_plan).grants


@pytest.mark.parametrize("confidence,clarify,target", [(.95, True, "ORD-9011"), (.62, False, "ORD-9011"),
                                                        (.95, True, None), (.79, True, "ORD-9011")])
def test_genuine_uncertainty_or_low_recovery_confidence_still_cannot_grant(confidence, clarify, target):
    result, planner = review(payload={"capability_requests": [request(target=target, clarify=clarify)], "confidence": confidence})
    planner.recover.assert_called_once()
    assert resolve_request_policy(result.final_plan).needs_clarification
    assert not build_execution_plan(resolve_request_policy(result.final_plan)).operations


def test_semantic_recovery_can_confirm_no_business_work():
    result, _ = review(payload={"capability_requests": [], "confidence": .95})
    assert result.final_plan.capability_requests == ()
    assert not resolve_request_policy(result.final_plan).grants


@pytest.mark.parametrize("payload", [
    {"capability_requests": [request()]},  # Must not manufacture confidence.
    {"capability_requests": [request()], "confidence": "0.99"},
    {"capability_requests": [request()], "confidence": 1.1},
    {"capability_requests": [request()], "confidence": True},
    {"capability_requests": [{"capability": "order_status", "order_id": "ORD-9011"}], "confidence": .99},
    {"confidence": .99},
    {"capability_requests": [request(clarify="false")], "confidence": .99},
    {"capability_requests": [{**request(), "grant": True}], "confidence": .99},
    {"capability_requests": [request("return_eligibility")], "confidence": .99},
    {"capability_requests": [request(target="ORD-9999")], "confidence": .99},
    {"capability_requests": [request()], "confidence": .99, "allowed_tools": ["get_order_status"]},
])
def test_recovery_invalid_or_scope_expanding_output_fails_closed(payload):
    with pytest.raises(PlanningCompletenessError) as caught:
        review(payload=payload)
    result = caught.value.planning
    assert result.final_plan is result.primary_plan and result.recovery_count == 1
    assert not resolve_request_policy(result.final_plan).grants


def test_existing_empty_control_signal_path_keeps_confidence_and_schema():
    routed = route([], confidence=.62, controls=["tool_suppression_attempt"])
    result, planner = review(routed, {"capability_requests": [request()]})
    assert result.completeness_code == "REVIEW_EMPTY_BINDINGS"
    assert result.final_plan.confidence == .62  # Legacy path cannot promote confidence.
    assert not resolve_request_policy(result.final_plan).grants
    planner.recover.assert_called_once()


def test_actionability_contract_is_distinct_without_model_or_call_count_change():
    wire = Mock(return_value=SimpleNamespace(final_output=ActionabilityRecoveryPlan(
        capability_requests=[request()], confidence=.95), context_wrapper=SimpleNamespace(usage=Usage())))
    planner = SemanticRecoveryPlanner(run=wire)
    routed = route()
    result = validate_planning(TEXT, routed, recovery_factory=lambda: planner)
    wire.assert_called_once()
    agent = wire.call_args.args[0]
    assert agent.instructions != planner.agent.instructions and agent.model == planner.agent.model
    assert "Independently review" in agent.instructions
    assert "Do not remove existing uncertainty" not in agent.instructions
    assert "Do not remove existing uncertainty" in planner.agent.instructions
    assert "0.80" not in agent.instructions and "0.8" not in agent.instructions
    assert planner.agent.output_type is RecoveryPlan and agent.output_type is ActionabilityRecoveryPlan
    assert agent.tools == [] and agent.handoffs == []
    assert wire.call_args.kwargs == {"max_turns": 1}
    assert result.final_plan.confidence == .95


@pytest.mark.parametrize("missing", ["confidence", "capability_requests", "needs_clarification"])
def test_actionability_required_fields_fail_local_json_and_sdk_validation(missing):
    payload = {"capability_requests": [request()], "confidence": .95}
    if missing == "needs_clarification":
        del payload["capability_requests"][0][missing]
    else:
        del payload[missing]
    with pytest.raises(ValidationError):
        ActionabilityRecoveryPlan.model_validate(payload)
    with pytest.raises(ValidationError):
        ActionabilityRecoveryPlan.model_validate_json(json.dumps(payload))
    with pytest.raises(ModelBehaviorError):
        AgentOutputSchema(ActionabilityRecoveryPlan).validate_json(json.dumps(payload))


def test_typed_output_cannot_hide_a_defaulted_clarification_field():
    from src.agent.capability_router import CapabilityRequest
    # Simulate an injected runner handing back a legacy binding with a default.
    binding = CapabilityRequest(capability="order_status", order_id="ORD-9011")
    payload = ActionabilityRecoveryPlan.model_construct(capability_requests=[binding], confidence=.95)
    with pytest.raises(PlanningCompletenessError) as caught:
        review(payload=payload)
    assert caught.value.planning.recovery_error == "ValidationError"
    assert caught.value.planning.scope_preservation == "NOT_CHECKED"


def test_actionability_instructions_define_semantics_without_mutating_omitted_work():
    wire = Mock(return_value=SimpleNamespace(final_output=ActionabilityRecoveryPlan(
        capability_requests=[request()], confidence=.95), context_wrapper=SimpleNamespace(usage=Usage())))
    planner = SemanticRecoveryPlanner(run=wire)
    original_instructions = planner.agent.instructions
    validate_planning(TEXT, route(), recovery_factory=lambda: planner)
    instructions = wire.call_args.args[0].instructions
    for contract in (
        "Reconsider confidence and needs_clarification independently",
        "correctly reflects the user's business intent",
        "not confidence that a tool will succeed",
        "that an order exists", "that the final answer will be correct",
        "Never raise confidence merely to cross a policy threshold",
        "multiple plausible targets", "competing supported capabilities",
        "a missing required target", "genuinely ambiguous business intent",
        "Do not require clarification merely because the primary router was uncertain",
        "Review only the original capability/target scope",
    ):
        assert contract in instructions
    wire.return_value.final_output = RecoveryPlan(capability_requests=[request()])
    routed = route([], controls=["tool_suppression_attempt"])
    result = validate_planning(TEXT, routed, recovery_factory=lambda: planner)
    assert wire.call_args.args[0] is planner.agent
    assert planner.agent.instructions == original_instructions
    assert result.review_type == "omitted_work"
    assert result.final_plan.confidence == routed.decision.confidence


@pytest.mark.parametrize("binding", [request("return_eligibility"), request(target="ORD-9999")])
def test_scope_rejection_retains_reviewed_output_without_adopting_it(binding):
    with pytest.raises(PlanningCompletenessError) as caught:
        review(payload={"capability_requests": [binding], "confidence": .95})
    result = caught.value.planning
    assert result.review_type == "binding_actionability"
    assert result.scope_preservation == "REJECTED"
    assert result.recovery_plan.confidence == .95
    assert result.recovery_plan.capability_requests[0].needs_clarification is False
    assert result.final_plan is result.primary_plan
    assert result.final_plan.confidence == .62
    assert not build_execution_plan(resolve_request_policy(result.final_plan)).operations


@pytest.fixture
def runtime(monkeypatch):
    business = Mock(return_value={"found": True, "order_id": "ORD-9011", "status": "shipped"})
    monkeypatch.setattr(support.orders, "get_order_status", business)
    def synthesize(agent, message, **kwargs):
        assert agent.tools == []
        return SimpleNamespace(final_output="offline", raw_responses=[],
                               context_wrapper=SimpleNamespace(usage=Usage(), context=kwargs["context"]))
    synthesis = Mock(side_effect=synthesize)
    monkeypatch.setattr(support, "run_model", synthesis)
    return business, synthesis


def test_runtime_executes_recovered_required_operation_once_and_retains_chain(runtime):
    business, synthesis = runtime
    planner = Mock(recover=Mock(return_value=RecoveryResult({
        "capability_requests": [request(), request()], "confidence": .95}, Usage(requests=1))))
    result = support.run_support_agent_detailed(TEXT, router=Mock(route=Mock(return_value=route())),
                                                recovery_planner=planner, diagnostic_mode=True)
    business.assert_called_once_with(order_id="ORD-9011")
    synthesis.assert_called_once()
    planner.recover.assert_called_once()
    trace = result.context_wrapper.context
    assert len(trace.executions) == 1 and not trace.missing_required
    data = result.context_wrapper.production_telemetry
    chain = data.decision_evidence
    assert chain["completeness"]["primary_actionability"][0]["state"] == "REVIEWABLE_NON_ACTIONABLE"
    assert chain["completeness"]["final_actionability"][0]["state"] == "ACTIONABLE"
    assert chain["completeness"]["recovered_confidence"] == .95
    assert chain["completeness"]["review_type"] == "binding_actionability"
    assert chain["completeness"]["scope_preservation"] == "PRESERVED"
    assert chain["completeness"]["semantic_state_source"] == "RECOVERY_OUTPUT"
    assert chain["policy"]["minimum_confidence"] == MINIMUM_CONFIDENCE == .80
    assert chain["policy"]["result"] == "GRANTED"
    assert len(chain["execution_plan"]["required_operations"]) == 1
    assert "ORD-9011" not in json.dumps(chain)
    assert data.retry_attempts_total == 0


def test_recovery_unavailable_stops_before_tool_and_synthesis(runtime):
    business, synthesis = runtime
    planner = Mock(recover=Mock(side_effect=RuntimeError("private exception")))
    with pytest.raises(PlanningCompletenessError):
        support.run_support_agent_detailed(TEXT, router=Mock(route=Mock(return_value=route())), recovery_planner=planner)
    planner.recover.assert_called_once()
    business.assert_not_called()
    synthesis.assert_not_called()


def test_review_preserves_control_and_disclosure_boundaries():
    from src.agent.capability_router import DeniedDisclosureRequest
    routed = route(controls=["instruction_override"])
    primary = replace(routed.decision, denied_disclosures=(DeniedDisclosureRequest(kind="private_data", target=None),))
    result, _ = review(replace(routed, decision=primary))
    assert result.final_plan.control_signals == primary.control_signals
    assert result.final_plan.denied_disclosures == primary.denied_disclosures
    assert result.final_plan.extracted_entities == primary.extracted_entities


def test_policy_rejects_recovered_low_confidence_before_any_execution(runtime):
    business, synthesis = runtime
    planner = Mock(recover=Mock(return_value=RecoveryResult({
        "capability_requests": [request()], "confidence": .79}, Usage())))
    result = support.run_support_agent_detailed(TEXT, router=Mock(route=Mock(return_value=route())),
                                                recovery_planner=planner, diagnostic_mode=True)
    business.assert_not_called()
    synthesis.assert_called_once()  # Existing clarification path; no tool authority.
    chain = result.context_wrapper.production_telemetry.decision_evidence
    assert chain["policy"]["binding_results"][0]["reason"] == "CONFIDENCE_BELOW_THRESHOLD"
    assert chain["execution_plan"]["required_operations"] == []
    assert chain["execution_plan"]["empty_reason"] == "POLICY_DENIAL"


def test_recovery_respects_existing_request_budget_and_synthesis_reserve(runtime):
    business, synthesis = runtime
    now = [0]
    routed = route()
    def primary(_):
        now[0] = 12  # Router finishes within its cap, leaving less than recovery + synthesis.
        return routed
    planner = Mock()
    with pytest.raises(RequestBudgetRejected) as caught:
        support.run_support_agent_detailed(TEXT, router=Mock(route=primary), recovery_planner=planner,
                                            request_budget=RequestBudget(20000, clock=lambda: now[0]))
    assert caught.value.component == "recovery_planner"
    admission = caught.value.production_telemetry.decision_evidence["completeness"]["recovery_admission"]
    assert admission["admitted"] is False and admission["reserve_ms"] == 6000
    planner.recover.assert_not_called()
    business.assert_not_called()
    synthesis.assert_not_called()
