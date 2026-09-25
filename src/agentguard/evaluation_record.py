"""Capture one support-agent execution for reuse by multiple evaluators."""

from dataclasses import dataclass, field
import json
import time

from agents.items import ToolCallItem, ToolCallOutputItem
from openai.types.responses import ResponseFunctionToolCall

from src.agent.support_agent import run_support_agent_detailed
from src.agent.execution_plan import ExecutionFailure, ExecutionTrace
from src.agent.planning_completeness import PlanningCompletenessError


@dataclass
class EvaluationRecord:
    scenario_id: str
    input: str
    final_output: str
    tool_calls: list[dict]
    latency_ms: float
    request_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    tool_outputs: list[dict] = field(default_factory=list)
    execution: dict | None = None
    execution_error: str | None = None
    planning: dict | None = None


def execute_scenario(scenario: dict) -> EvaluationRecord:
    """Execute once and capture calls, outputs, elapsed time, and SDK usage.

    JSON object arguments become dictionaries. Malformed or non-object JSON
    is preserved as its original string so evaluators can report it faithfully.
    Outputs contain name, call_id, and output; unmatched names/IDs are None.
    Valid JSON output strings are decoded; other returned values are preserved.
    Runtime failures have no accepted final output; their explicit error and
    obligation snapshot are retained alongside any actual calls and results.
    """
    start = time.perf_counter()
    execution_error = None
    planning = None
    try:
        result = run_support_agent_detailed(scenario["input"])
    except PlanningCompletenessError as error:
        planning = error.planning.snapshot()
        trace, usage = None, error.planning.usage
        execution_error, final_output, new_items = str(error), "", []
    except ExecutionFailure as error:
        # Preserve actual attempts and explicit failure without re-running either
        # the router or support model, or inventing an authoritative tool result.
        trace, usage = error.trace, error.usage
        execution_error = str(error)  # Runtime-defined messages contain no payloads.
        final_output = ""  # No accepted support answer; do not manufacture one.
        new_items = []
    else:
        trace = getattr(result.context_wrapper, "context", None)
        usage = result.context_wrapper.usage
        final_output = result.final_output
        new_items = result.new_items
    latency_ms = (time.perf_counter() - start) * 1000

    tool_calls = []
    tool_names = {}
    for item in new_items:
        if not isinstance(item, ToolCallItem):
            continue
        if item.call_id is not None:
            tool_names[item.call_id] = item.tool_name
        raw = item.raw_item
        if not isinstance(raw, ResponseFunctionToolCall):
            continue
        arguments = raw.arguments
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(parsed, dict):
                arguments = parsed
        tool_calls.append({"name": raw.name, "arguments": arguments})

    tool_outputs = []
    for item in new_items:
        if not isinstance(item, ToolCallOutputItem):
            continue
        output = item.output
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                pass
        tool_outputs.append({
            "name": tool_names.get(item.call_id),
            "call_id": item.call_id,
            "output": output,
        })

    execution = None
    if isinstance(trace, ExecutionTrace):
        planning_result = getattr(trace, "planning", None)
        if planning_result is not None:
            planning = planning_result.snapshot()
        execution = trace.snapshot()
        # Direct runtime calls are input history, not model-generated new_items.
        # Read the actual execution trace rather than inferring calls from text.
        tool_calls = [
            {"name": item.operation.tool, "arguments": dict(item.operation.arguments)}
            for item in trace.executions if item.invoked
        ] + tool_calls
        tool_outputs = [
            {"name": item.operation.tool, "call_id": item.call_id, "output": item.output}
            for item in trace.executions if item.status == "completed"
        ] + tool_outputs
    return EvaluationRecord(
        scenario_id=scenario["id"],
        input=scenario["input"],
        final_output=final_output,
        tool_calls=tool_calls,
        tool_outputs=tool_outputs,
        latency_ms=latency_ms,
        request_count=getattr(usage, "requests", None),
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        execution=execution,
        execution_error=execution_error,
        planning=planning,
    )
