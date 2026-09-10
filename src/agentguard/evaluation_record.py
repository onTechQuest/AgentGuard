"""Capture one support-agent execution for reuse by multiple evaluators."""

from dataclasses import dataclass
import json
import time

from agents.items import ToolCallItem
from openai.types.responses import ResponseFunctionToolCall

from src.agent.support_agent import run_support_agent_detailed


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


def execute_scenario(scenario: dict) -> EvaluationRecord:
    """Execute once and capture calls, elapsed time, and aggregate SDK usage.

    JSON object arguments become dictionaries. Malformed or non-object JSON
    is preserved as its original string so evaluators can report it faithfully.
    """
    start = time.perf_counter()
    result = run_support_agent_detailed(scenario["input"])
    latency_ms = (time.perf_counter() - start) * 1000

    tool_calls = []
    for item in result.new_items:
        if not isinstance(item, ToolCallItem):
            continue
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

    usage = result.context_wrapper.usage
    return EvaluationRecord(
        scenario_id=scenario["id"],
        input=scenario["input"],
        final_output=result.final_output,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
        request_count=getattr(usage, "requests", None),
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )
