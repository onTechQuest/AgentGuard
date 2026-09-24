from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents import Agent
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from src.agentguard import evaluation_record


def make_call(agent, arguments, call_id="call-1", name="get_order_status"):
    return ToolCallItem(
        agent=agent,
        raw_item=ResponseFunctionToolCall(
            type="function_call", name=name,
            arguments=arguments, call_id=call_id,
        ),
    )


def mock_execution(monkeypatch, items, usage):
    run = Mock(return_value=SimpleNamespace(
        final_output="Order ORD-1001 is shipped.",
        new_items=items,
        context_wrapper=SimpleNamespace(usage=usage),
    ))
    monkeypatch.setattr(evaluation_record, "run_support_agent_detailed", run)
    monkeypatch.setattr(evaluation_record.time, "perf_counter", Mock(side_effect=[10, 10.125]))
    return run


def test_captures_one_execution(monkeypatch):
    agent = Agent(name="Offline test")
    call = make_call(agent, '{"order_id": "ord-1001"}')
    output = ToolCallOutputItem(
        agent=agent, raw_item={"type": "function_call_output", "call_id": "call-1", "output": "shipped"},
        output={"status": "shipped"},
    )
    run = mock_execution(
        monkeypatch, [call, output, call],
        Usage(requests=2, input_tokens=100, output_tokens=20, total_tokens=120),
    )
    scenario = {"id": "status-1", "input": "Where is order ORD-1001?"}

    record = evaluation_record.execute_scenario(scenario)

    run.assert_called_once_with(scenario["input"])
    assert record.scenario_id == "status-1"
    assert record.input == scenario["input"]
    assert record.final_output == "Order ORD-1001 is shipped."
    assert record.tool_calls == [
        {"name": "get_order_status", "arguments": {"order_id": "ord-1001"}},
    ] * 2
    assert record.tool_outputs == [{
        "name": "get_order_status", "call_id": "call-1", "output": {"status": "shipped"},
    }]
    assert record.latency_ms == pytest.approx(125)
    assert (record.request_count, record.input_tokens, record.output_tokens, record.total_tokens) == (2, 100, 20, 120)


@pytest.mark.parametrize("arguments", ["invalid-json", "[]", "null"])
def test_preserves_non_object_arguments(monkeypatch, arguments):
    agent = Agent(name="Offline test")
    mock_execution(monkeypatch, [make_call(agent, arguments)], None)

    record = evaluation_record.execute_scenario({"id": "bad-arguments", "input": "Status?"})

    assert record.tool_calls == [{"name": "get_order_status", "arguments": arguments}]
    assert record.tool_outputs == []
    assert (record.request_count, record.input_tokens, record.output_tokens, record.total_tokens) == (None, None, None, None)


def test_no_calls_and_zero_usage(monkeypatch):
    mock_execution(monkeypatch, [], Usage())

    record = evaluation_record.execute_scenario({"id": "no-tools", "input": "Hello"})

    assert record.tool_calls == []
    assert record.tool_outputs == []
    assert (record.request_count, record.input_tokens, record.output_tokens, record.total_tokens) == (0, 0, 0, 0)


@pytest.mark.parametrize("output, expected", [
    ({"status": "shipped"}, {"status": "shipped"}),
    ([{"status": "shipped"}], [{"status": "shipped"}]),
    ('{"status": "shipped"}', {"status": "shipped"}),
    ('["UPS", "FedEx"]', ["UPS", "FedEx"]),
    ('"shipped"', "shipped"),
    ("null", None),
    ("false", False),
    ("0", 0),
    (None, None),
    ("Order has shipped.", "Order has shipped."),
    ('{"status":', '{"status":'),
    ("{'status': 'shipped'}", "{'status': 'shipped'}"),
    ("", ""),
])
def test_captures_output_values(monkeypatch, output, expected):
    agent = Agent(name="Offline test")
    item = ToolCallOutputItem(
        agent=agent,
        raw_item={"type": "function_call_output", "call_id": "call-1", "output": str(output)},
        output=output,
    )
    run = mock_execution(monkeypatch, [make_call(agent, "{}"), item], None)

    record = evaluation_record.execute_scenario({"id": "outputs", "input": "Status?"})

    assert record.tool_outputs == [{
        "name": "get_order_status", "call_id": "call-1", "output": expected,
    }]
    assert item.output == output
    run.assert_called_once_with("Status?")


@pytest.mark.parametrize("raw_ids, expected_id", [
    ({"call_id": "unknown"}, "unknown"),
    ({"id": "output-id"}, "output-id"),
    ({}, None),
])
def test_preserves_unmatched_output(monkeypatch, raw_ids, expected_id):
    agent = Agent(name="Offline test")
    item = ToolCallOutputItem(
        agent=agent,
        raw_item={"type": "function_call_output", "output": "diagnostic", **raw_ids},
        output="diagnostic",
    )
    mock_execution(monkeypatch, [item], None)

    record = evaluation_record.execute_scenario({"id": "unmatched", "input": "Status?"})

    assert record.tool_outputs == [{"name": None, "call_id": expected_id, "output": "diagnostic"}]


def test_associates_outputs_by_id_and_preserves_output_order(monkeypatch):
    agent = Agent(name="Offline test")
    first = make_call(agent, "{}")
    second = make_call(agent, "{}", call_id="call-2", name="check_return_eligibility")
    outputs = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={"type": "function_call_output", "call_id": call_id, "output": value},
            output=value,
        )
        for call_id, value in [("call-2", "eligible"), ("call-1", "shipped")]
    ]
    mock_execution(monkeypatch, [*outputs, first, second], None)

    record = evaluation_record.execute_scenario({"id": "multiple", "input": "Status?"})

    assert record.tool_outputs == [
        {"name": "check_return_eligibility", "call_id": "call-2", "output": "eligible"},
        {"name": "get_order_status", "call_id": "call-1", "output": "shipped"},
    ]


def test_output_defaults_are_independent_for_existing_record_constructors():
    args = ("id", "input", "answer", [], 0.0, None, None, None, None)
    first = evaluation_record.EvaluationRecord(*args)
    second = evaluation_record.EvaluationRecord(*args)

    first.tool_outputs.append({"output": "diagnostic"})

    assert second.tool_outputs == []
