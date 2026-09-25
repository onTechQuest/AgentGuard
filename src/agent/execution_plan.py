"""Runtime obligations derived from authorized bindings, independent of evaluation.

Required operations must finish before answer synthesis. Optional operations may
be explicitly invoked but do not block completion. Anything outside the plan (or
explicitly prohibited) is denied. The current policy emits only required reads;
this layer never infers additional business intent from a message or identifier.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import time
from typing import Callable
from uuid import uuid4

from src.agent import telemetry
from src.agent.data_policy import project_tool_result
from src.agent.request_policy import RequestToolPolicy, ToolGrant
from src.agentguard.tool_policy import action_policy_snapshot


class OperationMode(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class Operation:
    tool: str
    arguments: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]
    mode: OperationMode = OperationMode.REQUIRED


@dataclass(frozen=True)
class ExecutionPlan:
    authorized_bindings: tuple[ToolGrant, ...]
    operations: tuple[Operation, ...]
    prohibited_actions: tuple[str, ...]


def build_execution_plan(policy: RequestToolPolicy) -> ExecutionPlan:
    """Keep the resolver's minimum authoritative cover and per-target grants."""
    bindings = tuple(dict.fromkeys(policy.grants))
    return ExecutionPlan(
        bindings,
        tuple(Operation(grant.tool, (("order_id", grant.order_id),), grant.capabilities)
              for grant in bindings),
        tuple(sorted(action_policy_snapshot().unsupported_write_actions)),
    )


@dataclass
class OperationExecution:
    operation: Operation
    call_id: str = field(default_factory=lambda: "runtime_" + uuid4().hex)
    status: str = "pending"
    invoked: bool = False
    output: dict | None = None
    error: str | None = None
    error_type: str | None = None
    latency_ms: float | None = None


@dataclass
class ExecutionTrace:
    plan: ExecutionPlan
    executions: list[OperationExecution] = field(init=False)
    prohibited_attempts: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.executions = [OperationExecution(operation) for operation in self.plan.operations]

    @property
    def missing_required(self) -> list[OperationExecution]:
        return [item for item in self.executions
                if item.operation.mode == OperationMode.REQUIRED and item.status != "completed"]

    def snapshot(self) -> dict:
        """Only projected outputs and sanitized errors enter observable telemetry."""
        required = [item for item in self.executions if item.operation.mode == OperationMode.REQUIRED]
        return {
            "plan": asdict(self.plan),
            "operations": [asdict(item) for item in self.executions],
            "required_operations": [item.call_id for item in required],
            "completed_required_operations": [item.call_id for item in required if item.status == "completed"],
            "missing_required_operations": [item.call_id for item in self.missing_required],
            "optional_operations": [item.call_id for item in self.executions
                                    if item.operation.mode == OperationMode.OPTIONAL],
            "prohibited_operation_attempts": list(self.prohibited_attempts),
        }

    def model_input(self, user_message: str) -> str | list[dict]:
        """Replay actual orchestrator calls as public SDK function-call history.

        These are input history, not model-generated RunResult.new_items. The
        runtime trace is the source for capturing them in an evaluation record.
        """
        if self.missing_required:
            raise ExecutionFailure(self, "Required operation unresolved")
        items = [{"role": "user", "content": user_message}]
        for item in self.executions:
            if item.status != "completed":
                continue
            items.extend([
                {"type": "function_call", "call_id": item.call_id, "name": item.operation.tool,
                 "arguments": json.dumps(dict(item.operation.arguments))},
                {"type": "function_call_output", "call_id": item.call_id,
                 "output": json.dumps(item.output, ensure_ascii=False)},
            ])
        return items if len(items) > 1 else user_message


class ExecutionFailure(RuntimeError):
    """An explicit terminal failure; callers can inspect the runtime trace."""

    def __init__(self, trace: ExecutionTrace, message: str):
        super().__init__(message)
        self.trace = trace
        self.usage = None


def execute_operation(trace: ExecutionTrace, operation: Operation,
                      resolve_implementation: Callable[[str], Callable | None]) -> None:
    """At most one attempt, within the immutable authorized plan; no retries."""
    item = next((item for item in trace.executions if item.operation == operation), None)
    if item is None or operation.mode == OperationMode.PROHIBITED:
        trace.prohibited_attempts.append({"name": operation.tool, "arguments": dict(operation.arguments),
                                          "reason": "Operation is not authorized for execution"})
        raise ExecutionFailure(trace, "Prohibited operation attempted")
    if item.status != "pending":
        return  # Completed results are reused; failed attempts are never retried.
    with telemetry.observe("tool"):
        item.status = "running"
        started = time.perf_counter()
        try:
            implementation = resolve_implementation(operation.tool)
            if not callable(implementation):
                item.status, item.error = "failed", "missing_implementation"
                return
            item.invoked = True
            internal = implementation(**dict(operation.arguments))
            with telemetry.observe("projection"):
                item.output = project_tool_result(internal, operation.capabilities)
            item.status = "completed"
        except Exception as error:
            telemetry.fail(error)
            # Neither the internal payload nor exception text may bypass projection.
            item.status, item.error, item.error_type = "failed", "execution_failed", type(error).__name__
        finally:
            item.latency_ms = (time.perf_counter() - started) * 1000

            telemetry.operation(item)


def execute_required(trace: ExecutionTrace, resolve_implementation: Callable[[str], Callable | None]) -> None:
    for item in trace.executions:
        if item.operation.mode == OperationMode.REQUIRED:
            execute_operation(trace, item.operation, resolve_implementation)
    if trace.missing_required:
        raise ExecutionFailure(trace, "Required operation unresolved")
