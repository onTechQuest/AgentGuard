"""Offline observations of actual routing/completeness/policy/execution branches."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

from agents.usage import Usage
import pytest

from src.agent import decision_evidence as evidence, support_agent as support
from src.agent.capability_router import SemanticCapabilityRouter
from src.agent.planning_completeness import RecoveryResult
from src.agent.request_budget import RequestBudget, RecoveryBudgetPolicy, RequestBudgetRejected
from src.agent.runtime_reliability import RuntimeReliabilityPolicy


def binding(capability="order_status", target="ORD-1001", clarify=False):
    return {"capability": capability, "order_id": target, "needs_clarification": clarify}


@pytest.fixture
def run(monkeypatch):
    def synthesize(agent, message, **kwargs):
        assert agent.tools == []
        return SimpleNamespace(final_output="offline", raw_responses=[],
            context_wrapper=SimpleNamespace(usage=Usage(), context=kwargs["context"]))
    synthesis = Mock(side_effect=synthesize)
    monkeypatch.setattr(support, "run_model", synthesis)

    def invoke(bindings=(), *, controls=(), confidence=.99, recovered=(), text='Please check order "  ORD-1001  ".',
               diagnostic=True, recovered_confidence=None, **kwargs):
        primary = Mock(return_value=SimpleNamespace(final_output={
            "capability_requests": list(bindings), "confidence": confidence,
            "control_signals": list(controls), "denied_disclosures": []}, context_wrapper=SimpleNamespace(usage=Usage())))
        router = SemanticCapabilityRouter(run=primary)
        payload = {"capability_requests": list(recovered)}
        if recovered_confidence is not None:
            payload["confidence"] = recovered_confidence
        planner = Mock(recover=Mock(return_value=RecoveryResult(payload, Usage())))
        result = support.run_support_agent_detailed(text, router=router, recovery_planner=planner,
                                                    diagnostic_mode=diagnostic, **kwargs)
        primary.assert_called_once()
        return result.context_wrapper.production_telemetry, planner, result
    invoke.synthesis = synthesis
    return invoke


def test_quoted_whitespace_authorized_lookup(run):
    data, recovery, result = run([binding()])
    chain = data.decision_evidence
    assert chain["router"]["recognized_targets"] == ["entity_1"]
    assert chain["router"]["bindings"][0]["target"] == "entity_1"
    assert chain["policy"]["binding_results"][0]["result"] == "GRANTED"
    assert chain["execution_plan"]["required_operations"] == [{"tool": "get_order_status", "arguments": {"order_id": "entity_1"}}]
    assert chain["execution_plan"]["empty_reason"] is None
    assert result.context_wrapper.context.executions[0].status == "completed"
    recovery.recover.assert_not_called()
    run.synthesis.assert_called_once()


def test_empty_with_controls_recovers_once(run):
    data, recovery, _ = run(controls=["tool_suppression_attempt"], recovered=[binding()])
    review = data.decision_evidence["completeness"]
    assert review["decision"] == "REVIEW_EMPTY_BINDINGS"
    assert review["recovered_bindings"][0]["capability"] == "order_status"
    assert review["recovery_admission"]["admitted"] is True
    assert review["plan_source"] == "recovered"
    recovery.recover.assert_called_once()


def test_empty_without_controls_is_unassessed_not_legitimate(run):
    data, recovery, _ = run(diagnostic=False)
    chain = data.decision_evidence
    assert chain["retention_reason"] == "EMPTY_PLAN"
    assert chain["completeness"]["decision"] == "NO_CONTROL_SIGNALS"
    assert chain["completeness"]["recovery_admission"]["reason"] == "NOT_REQUESTED"
    assert chain["execution_plan"]["empty_reason"] == "EMPTY_BINDINGS_UNASSESSED"
    assert evidence.classify(chain, expected_business_work=True) == ["ROUTER_OMISSION", "COMPLETENESS_SKIP"]
    assert "LEGITIMATE_NO_WORK" not in evidence.classify(chain)
    recovery.recover.assert_not_called()


@pytest.mark.parametrize("bindings,confidence,reason,result", [
    ([binding("unknown")], .99, "UNKNOWN_CAPABILITY", "DENIED"),
    ([binding(clarify=True)], .99, "AMBIGUOUS_BINDING", "CLARIFICATION"),
    ([binding(target=None)], .99, "MISSING_TARGET", "CLARIFICATION"),
    ([binding()], .79, "CONFIDENCE_BELOW_THRESHOLD", "DENIED"),
    ([binding("unsupported_action")], .99, "CAPABILITY_PERMITS_NO_TOOL", "DENIED"),
])
def test_policy_reasons_are_actual_branch_results(run, bindings, confidence, reason, result):
    data, recovery, _ = run(bindings, confidence=confidence,
                            **({"recovered": bindings, "recovered_confidence": confidence} if confidence < .8 else {}))
    chain = data.decision_evidence
    policy = chain["policy"]
    assert policy["minimum_confidence"] == .80
    assert policy["candidate_plan"]["confidence"] == confidence
    assert policy["binding_results"][0]["reason"] == reason
    assert policy["binding_results"][0]["result"] == result
    assert policy["authorized_grants"] == []
    assert not chain["execution_plan"]["required_operations"]
    if confidence < .8:
        recovery.recover.assert_called_once()
    else:
        recovery.recover.assert_not_called()


def test_exact_confidence_boundary_unchanged(run):
    data, _, _ = run([binding()], confidence=.80)
    assert data.decision_evidence["policy"]["result"] == "GRANTED"


def test_legitimate_no_work_requires_external_or_recovery_evidence(run):
    data, planner, _ = run(text="ORD-1001 is just an example reference.")
    assert "LEGITIMATE_NO_WORK" in evidence.classify(data.decision_evidence, expected_business_work=False)
    planner.recover.assert_not_called()
    confirmed, planner, _ = run(text="Format this example, do not execute ORD-1001", controls=["instruction_override"])
    assert confirmed.decision_evidence["execution_plan"]["empty_reason"] == "LEGITIMATE_NO_WORK"
    planner.recover.assert_called_once()


def test_budget_denial_is_not_completeness_skip(run):
    with pytest.raises(RequestBudgetRejected) as caught:
        run(controls=["tool_suppression_attempt"], request_budget=RequestBudget(2000, clock=lambda: 0),
            recovery_budget_policy=RecoveryBudgetPolicy(recovery_allowance_ms=3000),
            runtime_reliability_policy=RuntimeReliabilityPolicy.unbounded())
    chain = caught.value.production_telemetry.decision_evidence
    assert chain["retention_reason"] == "FAILURE"
    assert chain["completeness"]["review_triggered"]
    assert chain["completeness"]["recovery_admission"]["admitted"] is False
    assert chain["completeness"]["recovery_admission"]["reason"] == "INSUFFICIENT_ALLOWANCE"
    assert "RECOVERY_NOT_ADMITTED" in chain["categories"]
    assert "COMPLETENESS_SKIP" not in chain["categories"]
    run.synthesis.assert_not_called()


@pytest.mark.parametrize("drop_bindings", [False, True])
def test_authorized_empty_plan_bug_is_distinct(run, monkeypatch, drop_bindings):
    original = support.build_execution_plan
    monkeypatch.setattr(support, "build_execution_plan", lambda policy: replace(original(policy), operations=(),
        **({"authorized_bindings": ()} if drop_bindings else {})))
    data, _, _ = run([binding()], diagnostic=False)
    assert data.decision_evidence["execution_plan"]["empty_reason"] == "AUTHORIZED_EMPTY_PLAN_BUG"


def test_successful_production_telemetry_is_compact(run):
    data, _, _ = run([binding()], diagnostic=False)
    assert data.decision_evidence is None
    assert "_decision_chain" not in data.snapshot()
    assert data.decision_summary["empty_plan"] is False


def test_exception_retains_evidence_without_changing_exception(run):
    error = RuntimeError("secret-provider-message")
    run.synthesis.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        run([binding()], diagnostic=False)
    assert caught.value is error
    chain = error.production_telemetry.decision_evidence
    assert chain["retention_reason"] == "FAILURE"
    assert "secret-provider-message" not in json.dumps(chain)


def test_decision_text_is_allowlisted(run):
    data, _, _ = run([binding("sk-secret@example.com")], text="private address secret@example.com ORD-1001")
    serialized = json.dumps(data.decision_evidence)
    assert "secret" not in serialized and "@" not in serialized
    assert "ORD-1001" not in serialized
    assert "<unregistered>" in serialized


def test_observation_failure_does_not_change_business_behavior(run, monkeypatch):
    monkeypatch.setattr(evidence, "plan", Mock(side_effect=ValueError("broken observation")))
    data, _, result = run([binding()])
    assert data.observation_incomplete
    assert result.context_wrapper.context.executions[0].status == "completed"
    run.synthesis.assert_called_once()


def test_parallel_decision_chains_do_not_mix(run):
    ids = [f"ORD-{i}" for i in range(9100, 9125)]
    def request(index):
        data, _, _ = run([binding(target=ids[index])], text="References " + ", ".join(ids[:index+1]))
        return data.decision_evidence
    with ThreadPoolExecutor(max_workers=5) as pool:
        chains = list(pool.map(request, range(len(ids))))
    for index, chain in enumerate(chains):
        assert chain["router"]["recognized_targets"] == [f"entity_{n+1}" for n in range(index+1)]
        assert chain["policy"]["authorized_grants"][0]["target"] == f"entity_{index+1}"


def test_no_target_skip_is_distinct(run):
    data, planner, _ = run(text="Hello")
    assert data.decision_evidence["completeness"]["decision"] == "NO_TARGET"
    planner.recover.assert_not_called()


def test_diagnostic_mode_changes_only_retention(run):
    ordinary, _, first = run([binding()], diagnostic=False)
    detailed, _, second = run([binding()], diagnostic=True)
    calls = run.synthesis.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0].instructions == calls[1].args[0].instructions
    assert calls[0].args[0].model == calls[1].args[0].model
    assert first.context_wrapper.context.plan == second.context_wrapper.context.plan
    assert first.final_output == second.final_output
    assert ordinary.effective_runtime_policy == detailed.effective_runtime_policy
    assert ordinary.decision_evidence is None and detailed.decision_evidence is not None
