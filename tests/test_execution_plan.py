"""Offline obligation enforcement across runtime, SDK synthesis and capture."""

from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents import Model, RunConfig
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from src.agent import support_agent as support
from src.agent.capability_router import SemanticCapabilityRouter
from src.agent.planning_completeness import RecoveryPlan, RecoveryResult
from src.agent.execution_plan import (
    ExecutionFailure, ExecutionTrace, Operation, OperationMode, build_execution_plan,
    execute_operation, execute_required,
)
from src.agent.request_policy import RequestToolPolicy, ToolGrant
from src.agentguard import evaluation_record


def router(requests, **changes):
    result = {"capability_requests": requests, "confidence": 0.99,
              "control_signals": [], "denied_disclosures": [], **changes}
    return SemanticCapabilityRouter(run=Mock(return_value=SimpleNamespace(
        final_output=result, context_wrapper=SimpleNamespace(
            usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)))))


def binding(capability="order_status", target="ORD-1001", **changes):
    return {"capability": capability, "order_id": target, **changes}


class AnswerModel(Model):
    def __init__(self, before_answer=None, attempt=None):
        self.inputs = []
        self.before_answer = before_answer
        self.attempt = attempt

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        assert not tools
        if self.before_answer:
            self.before_answer()
        self.inputs.append(input)
        output = [self.attempt] if self.attempt else [ResponseOutputMessage(
            type="message", id="answer", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text="Answer without electing to call a tool.", annotations=[])])]
        return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=20, output_tokens=5, total_tokens=25),
                             response_id=None)

    def stream_response(self, *args, **kwargs):
        raise AssertionError("No streaming")


def offline_sdk(monkeypatch, model):
    original = support.Runner.run_sync
    monkeypatch.setattr(support, "support_agent", support.support_agent.clone(model=model))
    monkeypatch.setattr(support.Runner, "run_sync", lambda agent, message, **kwargs:
                        original(agent, message, run_config=RunConfig(tracing_disabled=True), **kwargs))


@pytest.mark.parametrize("capabilities,tool", [
    (["order_status"], "get_order_status"),
    (["return_eligibility"], "check_return_eligibility"),
    (["order_status", "return_eligibility"], "check_return_eligibility"),
])
def test_required_read_completed_before_model_can_terminate(monkeypatch, capabilities, tool):
    business = Mock(return_value={"found": True, "eligible": True,
        "order": {"order_id": "ORD-1001", "status": "delivered", "customer_id": "private", "total": 999}})
    other = "check_return_eligibility" if tool == "get_order_status" else "get_order_status"
    forbidden = Mock(side_effect=AssertionError("Redundant business lookup"))
    monkeypatch.setattr(support.orders, tool, business)
    monkeypatch.setattr(support.orders, other, forbidden)
    model = AnswerModel(before_answer=lambda: business.assert_called_once_with(order_id="ORD-1001"))
    offline_sdk(monkeypatch, model)
    routed = router([binding(capability) for capability in capabilities])
    result = support.run_support_agent_detailed("Check ORD-1001", router=routed)
    forbidden.assert_not_called()
    assert len(model.inputs) == 1
    trace = result.context_wrapper.context
    assert trace.plan.operations[0].capabilities == tuple(sorted(capabilities))
    assert not trace.missing_required
    assert trace.snapshot()["required_operations"] == trace.snapshot()["completed_required_operations"]
    assert len(trace.executions) == 1
    assert "customer_id" not in json.dumps(model.inputs)
    assert "private" not in json.dumps(trace.snapshot())
    assert "total" not in json.dumps(model.inputs)
    # Router cost added exactly once; deterministic tools consume no model usage.
    assert result.context_wrapper.usage.requests == 2
    assert result.context_wrapper.usage.total_tokens == 40
    assert len(result.raw_responses) == 1


@pytest.mark.parametrize("requests,changes", [
    ([binding(needs_clarification=True)], {}),
    ([binding(target=None)], {}),
    ([binding("unsupported_action")], {}),
    ([], {"denied_disclosures": ["private_data"]}),
    ([], {}),  # Entity presence alone still does not trigger recovery.
    ([], {"control_signals": ["fake_system_authority"]}),
    ([binding()], {"confidence": 0.5}),
    ([binding("unknown")], {}),
])
def test_no_obligations_without_valid_authorized_business_binding(monkeypatch, requests, changes):
    business = Mock(side_effect=AssertionError("Unrequested tool execution"))
    monkeypatch.setattr(support.orders, "get_order_status", business)
    monkeypatch.setattr(support.orders, "check_return_eligibility", business)
    model = AnswerModel()
    offline_sdk(monkeypatch, model)
    recovery = Mock(recover=Mock(return_value=RecoveryResult(RecoveryPlan(capability_requests=[]), Usage())))
    result = support.run_support_agent_detailed("Request involving ORD-1001", router=router(requests, **changes),
                                               recovery_planner=recovery)
    business.assert_not_called()
    assert result.context_wrapper.context.plan.operations == ()
    assert model.inputs == [[{"role": "user", "content": "Request involving ORD-1001"}]]


def test_unclear_component_does_not_erase_clear_required_work(monkeypatch):
    business = Mock(return_value={"found": False, "error": "Order not found"})
    monkeypatch.setattr(support.orders, "get_order_status", business)
    model = AnswerModel()
    offline_sdk(monkeypatch, model)
    result = support.run_support_agent_detailed("Request ORD-1001 or ORD-1002", router=router([
        binding(), binding("return_eligibility", None, needs_clarification=True),
    ], denied_disclosures=["private_data"]))
    business.assert_called_once_with(order_id="ORD-1001")
    assert not result.context_wrapper.context.missing_required  # found=False is a completed read.


def one_operation():
    return ExecutionTrace(build_execution_plan(RequestToolPolicy((
        ToolGrant("get_order_status", "ORD-1001", ("order_status",)),
    ), False)))


@pytest.mark.parametrize("failure", ["exception", "missing", "malformed_result"])
def test_required_failure_is_explicit_and_bounded(monkeypatch, failure):
    tool = Mock(side_effect=RuntimeError("private exception text")) if failure == "exception" else (
        Mock(return_value="malformed result") if failure == "malformed_result" else None)
    monkeypatch.setattr(support.orders, "get_order_status", tool)
    model = Mock()
    monkeypatch.setattr(support.Runner, "run_sync", model)
    with pytest.raises(ExecutionFailure, match="Required operation unresolved") as caught:
        support.run_support_agent_detailed("Check ORD-1001", router=router([binding()]))
    model.assert_not_called()
    trace = caught.value.trace
    assert trace.executions[0].output is None
    assert trace.executions[0].status == "failed"
    assert trace.executions[0].invoked == (failure != "missing")
    assert len(trace.missing_required) == 1
    assert caught.value.usage.requests == 1
    assert "private exception text" not in json.dumps(trace.snapshot())
    with pytest.raises(ExecutionFailure):
        execute_required(trace, lambda name: tool)
    with pytest.raises(ExecutionFailure):
        trace.model_input("Cannot accept an answer with missing obligations")
    if tool:
        tool.assert_called_once_with(order_id="ORD-1001")


def test_completed_operation_is_never_reexecuted_and_optional_does_not_block():
    trace = one_operation()
    optional = replace(trace.plan.operations[0], arguments=(("order_id", "ORD-1002"),), mode=OperationMode.OPTIONAL)
    trace = ExecutionTrace(replace(trace.plan, operations=(*trace.plan.operations, optional)))
    tool = Mock(return_value={"found": True})
    execute_required(trace, lambda name: tool)
    execute_required(trace, lambda name: tool)
    tool.assert_called_once_with(order_id="ORD-1001")
    snapshot = trace.snapshot()
    assert len(snapshot["completed_required_operations"]) == 1
    assert len(snapshot["optional_operations"]) == 1
    assert not snapshot["missing_required_operations"]
    execute_operation(trace, optional, lambda name: tool)
    assert tool.call_count == 2
    assert not trace.missing_required


@pytest.mark.parametrize("explicit", [False, True])
def test_prohibited_attempt_separate_from_missing_required(explicit):
    trace = one_operation()
    operation = Operation("issue_refund", (("order_id", "ORD-1001"),), (), OperationMode.PROHIBITED)
    if explicit:
        trace = ExecutionTrace(replace(trace.plan, operations=(*trace.plan.operations, operation)))
    resolver = Mock()
    with pytest.raises(ExecutionFailure, match="Prohibited"):
        execute_operation(trace, operation, resolver)
    resolver.assert_not_called()
    snapshot = trace.snapshot()
    assert len(snapshot["missing_required_operations"]) == 1
    assert not snapshot["completed_required_operations"]
    assert snapshot["prohibited_operation_attempts"][0]["name"] == "issue_refund"
    assert "issue_refund" in trace.plan.prohibited_actions


@pytest.mark.parametrize("failure", [False, True])
def test_evaluation_capture_records_actual_runtime_calls_once(monkeypatch, failure):
    tool = Mock(side_effect=RuntimeError("secret")) if failure else Mock(return_value={"found": False, "error": "Missing"})
    monkeypatch.setattr(support.orders, "get_order_status", tool)
    model = AnswerModel()
    offline_sdk(monkeypatch, model)
    routed = router([binding()])
    run = Mock(side_effect=lambda text: support.run_support_agent_detailed(text, router=routed))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", run)
    record = evaluation_record.execute_scenario({"id": "offline", "input": "Check ORD-1001"})
    run.assert_called_once()
    assert record.tool_calls == [{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}]
    assert len(record.tool_outputs) == int(not failure)
    assert record.execution["missing_required_operations"] if failure else not record.execution["missing_required_operations"]
    assert record.execution_error == ("Required operation unresolved" if failure else None)
    if failure:
        assert record.final_output == ""
    assert record.request_count == (1 if failure else 2)
    assert "secret" not in json.dumps(asdict(record))


def test_model_cannot_retry_read_or_perform_unsupported_action(monkeypatch):
    tool = Mock(return_value={"found": True})
    monkeypatch.setattr(support.orders, "get_order_status", tool)
    model = AnswerModel(attempt=ResponseFunctionToolCall(
        type="function_call", name="get_order_status", arguments='{"order_id":"ORD-1001"}', call_id="retry"))
    offline_sdk(monkeypatch, model)
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed",
                        lambda text: support.run_support_agent_detailed(text, router=router([binding()])))
    record = evaluation_record.execute_scenario({"id": "offline", "input": "Check ORD-1001"})
    tool.assert_called_once()
    assert len(model.inputs) == 1
    assert len(record.tool_calls) == 1
    assert len(record.execution["prohibited_operation_attempts"]) == 1
    assert record.execution["missing_required_operations"] == []
    assert "Prohibited operation attempted" in record.execution_error
    assert record.request_count == 2


def test_distinct_targets_and_duplicate_grants_preserve_minimum_operations():
    first = ToolGrant("get_order_status", "ORD-1001", ("order_status",))
    second = replace(first, order_id="ORD-1002")
    trace = ExecutionTrace(build_execution_plan(RequestToolPolicy((first, first, second), False)))
    tool = Mock(return_value={"found": True})
    execute_required(trace, lambda name: tool)
    assert [call.kwargs for call in tool.call_args_list] == [{"order_id": "ORD-1001"}, {"order_id": "ORD-1002"}]
    assert len({item.call_id for item in trace.executions}) == 2


def test_runtime_layer_has_no_evaluation_dependencies():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/agent/execution_plan.py").read_text(encoding="utf-8")
    forbidden = {"datasets", "evaluation_record", "scoring", "safety_evaluator", "semantic_evaluator"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not set((node.module or "").split(".")) & forbidden
    assert "scenario_id" not in source
