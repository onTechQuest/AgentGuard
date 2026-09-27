"""Test-only single-owner requests, controlled dispatch, and parent collection.

Runner is replaced before workers start. No provider/client is constructed. This
proves orchestration isolation, not SDK HTTP retry or shared-pool thread safety.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace

import httpx2
from agents.usage import Usage
from openai import RateLimitError

from src.agent import support_agent as support, telemetry, request_execution, retry_policy
from src.agent.request_budget import RequestBudget


WAIT = 10
ACTIVE = ContextVar("offline_concurrency_request", default=None)
COMPONENTS = {"Capability Router": "primary_router", "Planning Completeness Reviewer": "recovery_planner",
              "AgentGuard Support Agent": "synthesis"}
CONTEXTS = (telemetry._request, telemetry._span, telemetry._started, telemetry._failure, telemetry._budget,
            request_execution._active_budget, request_execution._stage_budget,
            request_execution._runtime_policy, request_execution._recovery_policy, retry_policy._state)


def context_values():
    return tuple(variable.get() for variable in CONTEXTS)


class Schedule:
    """Explicit happens-before edges; only this locked log is shared by workers."""

    def __init__(self, names, *, overlap=True, dependencies=None):
        self.barrier = Barrier(len(names)) if overlap and len(names) > 1 else None
        self.dependencies = dependencies or {}
        self.events = {(name, stage): Event() for name in names for stage in
                       ("router_start", "router_end", "tool", "synthesis_start", "synthesis_end")}
        self.log = []
        self.lock = Lock()

    def point(self, name, stage):
        key = (name, stage)
        for prerequisite in self.dependencies.get(key, ()):
            assert self.events[prerequisite].wait(WAIT), f"Missing checkpoint {prerequisite}"
        with self.lock:
            self.log.append(key)
        self.events[key].set()
        if stage == "router_start" and self.barrier:
            self.barrier.wait(WAIT)


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self):
        return self.now


@dataclass(frozen=True)
class Completed:
    """Only immutable values cross the worker-to-collector boundary."""

    payload: str
    identities: tuple[tuple[str, tuple[int, ...]], ...]
    worker: int

    def read(self):
        return json.loads(self.payload)


@dataclass
class Request:
    name: str
    orders: tuple[str, ...] = ("ORD-1001",)
    capability: str = "order_status"
    recovery: bool = False
    fault: str | None = None
    seed: int = 1
    schedule: Schedule | None = None
    callback: object | None = None
    clock: Clock = field(default_factory=Clock)
    budget: RequestBudget = field(init=False)
    dispatches: Counter = field(default_factory=Counter, init=False)
    calls: list = field(default_factory=list, init=False)
    issued: dict = field(default_factory=dict, init=False)
    owned: dict = field(default_factory=dict, init=False)
    trace: object | None = field(default=None, init=False)
    claimed: bool = field(default=False, init=False)
    claim_lock: Lock = field(default_factory=Lock, init=False)

    def __post_init__(self):
        self.budget = RequestBudget(20000, clock=self.clock)

    @property
    def message(self):
        return "Check " + ", ".join(self.orders) + "."

    def expected_fact(self, order_id):
        fact = {"found": True, "order_id": order_id, "status": "shipped" if self.seed % 2 else "processing",
                "tracking_number": f"TRACK-{self.name}-{order_id}"}
        if self.capability == "return_eligibility":
            fact.update(eligible=False, reason="Order has not been delivered.")
        return fact

    def checkpoint(self, stage):
        if self.schedule:
            self.schedule.point(self.name, stage)

    def dispatch(self, agent, message, **kwargs):
        component = COMPONENTS[agent.name]
        self.dispatches[component] += 1
        assert self.dispatches[component] == 1, "Controlled model dispatch replayed"
        assert kwargs["run_config"]["model_settings"].retry.max_retries == 0
        assert agent.tools == [] and kwargs["max_turns"] == 1
        self.owned.setdefault("telemetry", telemetry._request.get())
        self.owned.setdefault("budget", request_execution._active_budget.get())
        self.owned.setdefault("retry", retry_policy.current_retry_state())
        assert telemetry._request.get() is self.owned["telemetry"]
        assert request_execution._active_budget.get() is self.owned["budget"]
        assert retry_policy.current_retry_state() is self.owned["retry"]
        bindings = [{"capability": self.capability, "order_id": order, "needs_clarification": False}
                    for order in self.orders]
        if component == "primary_router":
            self.checkpoint("router_start")
            assert json.loads(message)["extracted_entities"]["order_ids"] == list(self.orders)
            if self.callback:
                self.callback(self)
            if self.fault == "router":
                raise RuntimeError("Scripted router failure")
            if self.fault == "rate_limit":
                raise RateLimitError("Scripted 429", response=httpx2.Response(429,
                    request=httpx2.Request("POST", "https://offline.invalid")), body=None)
            if self.fault == "deadline":
                self.clock.now = 21.0
            if self.fault == "cancel":
                child = self.budget.child_budget(cap_ms=1000)
                child.request_cancellation()
                assert self.budget.cancellation_requested
            output = {"capability_requests": [] if self.recovery else bindings, "confidence": 0.99,
                      "control_signals": ["fabricated_tool_result"] if self.recovery else [], "denied_disclosures": []}
            self.checkpoint("router_end")
        elif component == "recovery_planner":
            output = {"capability_requests": bindings}
        else:
            self.checkpoint("synthesis_start")
            trace = kwargs["context"]
            assert trace is self.trace and not trace.missing_required
            expected = [self.expected_fact(order) for order in self.orders]
            actual = [json.loads(item["output"]) for item in message if item.get("type") == "function_call_output"] if isinstance(message, list) else []
            assert actual == expected, "Synthesis received another request's facts"
            assert len(self.calls) == len(expected)
            assert all(item.status == "completed" for item in trace.executions)
            if self.fault == "synthesis":
                raise RuntimeError("Scripted synthesis failure")
            # A deterministic stand-in for the final answer, derived solely from
            # the actual projected history, compared to an independent oracle.
            output = json.dumps(actual, sort_keys=True)
            self.checkpoint("synthesis_end")
        tokens = self.seed * 10 + {"primary_router": 1, "recovery_planner": 2, "synthesis": 3}[component]
        self.issued[component] = tokens
        def usage():
            return Usage(requests=1, input_tokens=tokens - 1, output_tokens=1, total_tokens=tokens)
        return SimpleNamespace(final_output=output, new_items=[],
            context_wrapper=SimpleNamespace(usage=usage(), context=kwargs.get("context")),
            raw_responses=[SimpleNamespace(usage=usage())])

    def tool(self, name, order_id):
        assert name == ("check_return_eligibility" if self.capability == "return_eligibility" else "get_order_status")
        assert order_id in self.orders
        self.calls.append({"name": name, "arguments": {"order_id": order_id}})
        assert sum(call["arguments"]["order_id"] == order_id for call in self.calls) == 1
        self.checkpoint("tool")
        return {**self.expected_fact(order_id), "customer_id": f"PRIVATE-{self.name}"}

    def run(self):
        with self.claim_lock:
            assert not self.claimed, "Root request objects must not be reused"
            self.claimed = True
        previous = context_values()
        token = ACTIVE.set(self)
        result, error = None, None
        try:
            try:
                result = support.run_support_agent_detailed(self.message, request_budget=self.budget)
                self.owned["result"] = result
            except Exception as caught:
                error = caught
                self.owned["result"] = caught
            # Snapshot only after runtime completion and decorator teardown.
            assert all(before is after for before, after in zip(previous, context_values()))
            observed = self.owned["telemetry"]
            self.owned["spans"] = tuple(observed.component_spans)
            self.owned["attempts"] = tuple(a for span in observed.component_spans for a in span.attempts)
            if self.trace:
                self.owned.update(trace=self.trace, plan=self.trace.plan, planning=self.trace.planning,
                                  outputs=tuple(item.output for item in self.trace.executions))
            identities = tuple((key, tuple(id(v) for v in value) if isinstance(value, tuple) else (id(value),))
                               for key, value in self.owned.items())
            payload = {"name": self.name, "scenario_input": self.message, "orders": self.orders,
                "fault": self.fault, "recovery": self.recovery,
                "expected_facts": [self.expected_fact(order) for order in self.orders],
                "final_output": result.final_output if result else None,
                "error_type": type(error).__name__ if error else None,
                "tool_calls": self.calls, "execution": self.trace.snapshot() if self.trace else None,
                "telemetry": telemetry.snapshot(observed), "dispatches": self.dispatches,
                "issued_tokens": sum(self.issued.values()) if self.issued else None,
                "expected_tool": "check_return_eligibility" if self.capability == "return_eligibility" else "get_order_status",
                "result_tokens": result.context_wrapper.usage.total_tokens if result else None,
                "retry_consumed": self.owned["retry"].consumed,
                "cancelled": self.budget.cancellation_requested, "expired": self.budget.exhausted(),
                "root_request_id": self.budget.request_id}
            return Completed(json.dumps(payload), identities, get_ident())
        finally:
            ACTIVE.reset(token)


def install(monkeypatch):
    from agents.models import openai_provider
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("Live/shared transport is forbidden in isolation tests")
    monkeypatch.setattr(openai_provider, "shared_http_client", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(support.Runner, "run_sync", lambda *a, **k: ACTIVE.get().dispatch(*a, **k))
    original = support.execute_required
    def required(trace, resolve):
        ACTIVE.get().trace = trace
        return original(trace, resolve)
    monkeypatch.setattr(support, "execute_required", required)
    for name in ("get_order_status", "check_return_eligibility"):
        monkeypatch.setattr(support.orders, name, lambda order_id, _name=name: ACTIVE.get().tool(_name, order_id))


VIOLATIONS = ("request_id_collisions", "telemetry_contamination", "tool_result_contamination",
              "duplicate_operations", "cross_request_budget_contamination", "incorrect_final_answers",
              "failure_isolation_violations", "hidden_retry_amplification", "authorization_bypass", "artifact_collisions")


def assess(rows):
    report = dict.fromkeys(VIOLATIONS, 0)
    report["requests_executed"] = len(rows)
    seen_ids, seen_objects, seen_operations = set(), set(), set()
    expected_categories = {"router": "UNKNOWN_INTERNAL_FAILURE", "synthesis": "UNKNOWN_INTERNAL_FAILURE",
                           "rate_limit": "RATE_LIMIT", "deadline": "DEADLINE_EXHAUSTED", "cancel": "CANCELLED"}
    for completed in rows:
        row = completed.read()
        observed = row["telemetry"]
        request_id = observed["request_id"]
        report["request_id_collisions"] += request_id in seen_ids
        seen_ids.add(request_id)
        for key, objects in completed.identities:
            report["telemetry_contamination"] += bool(seen_objects.intersection(objects))
            seen_objects.update(objects)
        report["cross_request_budget_contamination"] += (request_id != row["root_request_id"] or
            row["cancelled"] != (row["fault"] == "cancel") or row["expired"] != (row["fault"] == "deadline") or row["retry_consumed"] != 0)
        spans = observed["component_spans"]
        actual_dispatches = {span["component"]: span["logical_model_calls"] for span in spans if span["logical_model_calls"]}
        report["hidden_retry_amplification"] += (actual_dispatches != row["dispatches"] or
            any(count != 1 for count in row["dispatches"].values()) or observed["retry_attempts_total"] != 0)
        report["hidden_retry_amplification"] += any(len(span["attempts"]) != span["logical_model_calls"]
                                                    for span in spans if span["logical_model_calls"])
        report["telemetry_contamination"] += observed["observed_usage"]["total_tokens"] != row["issued_tokens"]
        report["telemetry_contamination"] += observed["planning_summary"].get("recovery_count", 0) != int(row["recovery"])
        report["failure_isolation_violations"] += (observed["terminal_status"] != ("failed" if row["fault"] else "completed") or
            observed["terminal_failure_category"] != expected_categories.get(row["fault"]))
        if not row["fault"]:
            report["incorrect_final_answers"] += row["final_output"] != json.dumps(row["expected_facts"], sort_keys=True)
            report["telemetry_contamination"] += row["result_tokens"] != row["issued_tokens"]
        else:
            report["incorrect_final_answers"] += row["final_output"] is not None
        should_execute = row["fault"] in (None, "synthesis")
        expected_count = len(row["orders"]) if should_execute else 0
        report["duplicate_operations"] += len(row["tool_calls"]) != expected_count
        expected_calls = [{"name": row["expected_tool"], "arguments": {"order_id": order}}
                          for order in row["orders"]] if should_execute else []
        report["authorization_bypass"] += row["tool_calls"] != expected_calls
        execution = row["execution"]
        if execution:
            operations = execution["operations"]
            ids = [item["call_id"] for item in operations]
            report["duplicate_operations"] += bool(seen_operations.intersection(ids)) or len(ids) != len(set(ids))
            seen_operations.update(ids)
            report["tool_result_contamination"] += [item["output"] for item in operations] != row["expected_facts"]
            report["authorization_bypass"] += bool(execution["prohibited_operation_attempts"])
            report["duplicate_operations"] += bool(execution["missing_required_operations"])
    return report


class Campaign:
    def __init__(self, results, artifacts):
        self.parent = get_ident()
        self.results = results
        self.artifacts = artifacts
        self.requests = []  # Parent owns references; prevents identity reuse during the audit.

    def collect(self, rows):
        assert get_ident() == self.parent, "Only the parent coordinator may collect"
        assert all(isinstance(row, Completed) for row in rows)
        self.results.extend(rows)
        return rows

    def run(self, requests, *, workers=None):
        assert get_ident() == self.parent
        roots = [request.budget for request in requests]
        assert len({id(root) for root in roots}) == len(roots), "Independent requests must own distinct budgets"
        assert len({root.request_id for root in roots}) == len(roots), "Independent requests must own distinct IDs"
        self.requests.extend(requests)
        with ThreadPoolExecutor(max_workers=workers or len(requests)) as pool:
            futures = [pool.submit(request.run) for request in requests]
            rows = [future.result(timeout=WAIT * 3) for future in futures]
        return self.collect(rows)

    def publish(self, path, rows):
        assert get_ident() == self.parent, "Only the parent coordinator may publish"
        with path.open("x", encoding="utf-8") as output:
            json.dump([row.read() for row in rows], output)

    def collect_artifacts(self, paths):
        assert get_ident() == self.parent
        # Read only after all writers have returned and closed their files.
        for path in paths:
            item = json.loads(path.read_text(encoding="utf-8"))
            self.artifacts.append((str(path), item["request_id"], item["scenario_id"]))
