"""Isolated local TCP experiment. Run with -W always; warnings also reach stderr.

Only the provider configuration seam and transparent tool observers are injected.
Runner, business implementations, policy, clocks/budget semantics and prompts are
real. Private loop/pool attributes are read for diagnostics, never mutated.
"""
import asyncio
from contextvars import ContextVar
import inspect
import json
from pathlib import Path
import sys
from threading import Lock, Thread, get_ident
import warnings
import weakref

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents import Agent, Runner, set_trace_processors, set_tracing_disabled
from agents.tracing import TracingProcessor
from agents.models.openai_provider import OpenAIProvider
from tests.transport_ownership.probe import Audit, Endpoint, Handler, KEY, network_guard, response, error_snapshot
from src.agent import model_execution, support_agent, telemetry, request_execution, retry_policy
from src.agent.request_budget import RequestBudget

CURRENT = ContextVar("qualification_request", default=None)


class LocalTracing(TracingProcessor):
    def __init__(self):
        self.lock, self.active, self.starts = Lock(), set(), 0

    def on_trace_start(self, trace):
        with self.lock:
            self.active.add(trace.trace_id)
            self.starts += 1

    def on_trace_end(self, trace):
        with self.lock:
            self.active.remove(trace.trace_id)

    def on_span_start(self, span):
        with self.lock:
            self.active.add(span.span_id)

    def on_span_end(self, span):
        with self.lock:
            self.active.remove(span.span_id)

    def shutdown(self):
        assert not self.active

    def force_flush(self):
        pass


class WarningEvidence:
    """Observe and forward every warning; weak references cannot keep generators alive."""
    def __init__(self):
        self.rows, self.refs = [], []
        self.original = warnings.showwarning
        warnings.showwarning = self.observe

    def observe(self, message, category, filename, lineno, file=None, line=None):
        frame = inspect.currentframe()
        generator = None
        try:
            while frame:
                if frame.f_code.co_name == "_asyncgen_firstiter_hook":
                    generator = frame.f_locals.get("agen")
                    break
                frame = frame.f_back
            item = CURRENT.get() or {}
            self.rows.append({"message": str(message), "category": category.__name__,
                              "filename": filename, "lineno": lineno,
                              "request": item.get("tag"), "phase": item.get("phase"),
                              "generator": generator.ag_code.co_qualname if generator else None})
            if generator is not None:
                self.refs.append(weakref.ref(generator))
        finally:
            del frame, generator
        self.original(message, category, filename, lineno, file=file, line=line)

    def finish(self):
        # No gc.collect: prove state before any diagnostic collection/forced cleanup.
        open_generators = sum(ref() is not None and ref().ag_frame is not None for ref in self.refs)
        for row in self.rows:
            row["disposition"] = ("SDK_WARNING_WITH_PROVEN_CLEAN_RESOURCE_STATE"
                                  if row["category"] == "ResourceWarning" and row["generator"]
                                  and "was scheduled after loop.shutdown_asyncgens()" in row["message"]
                                  and open_generators == 0 else "UNKNOWN")
        return {"warnings": self.rows, "open_warned_generators": open_generators}


class BusinessHandler(Handler):
    def do_POST(self):
        assert self.path == "/v1/responses"
        assert self.headers["Authorization"] == "Bearer " + KEY
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tag = self.headers["X-Qualification-Request"]
        item = self.server.requests[tag]
        schema = body.get("text", {}).get("format", {}).get("schema", {}).get("properties", {})
        component = ("primary_router" if "confidence" in schema else
                     "recovery_planner" if "capability_requests" in schema else "synthesis")
        item["phase"] = component
        record = {"tag": tag, "component": component, "connection": self.connection_id,
                  "retry": self.headers.get("x-stainless-retry-count"), "body": body}
        with self.server.lock:
            self.server.records.append(record)
        if component == "primary_router" and item["round"] == 0 and self.server.overlap:
            self.server.overlap.wait(10)
        failure = item["kind"]
        if component == "primary_router" and failure == "cancel":
            # Cancel the actual in-flight Runner task only after HTTP arrival.
            def cancel():
                for task in asyncio.all_tasks(item["loop"]):
                    task.cancel()
            item["loop"].call_soon_threadsafe(cancel)
            assert item["cancel_drained"].wait(10)
        if component == "primary_router" and failure == "late":
            # Same production policy; advance only this request's test clock.
            item["clock"][0] += 21.0
        status = 200
        if component == "primary_router" and failure in {"429", "500"}:
            status = int(failure)
        if component == "synthesis" and failure == "synthesis_failure":
            status = 500
        binding = {"capability": "order_status", "order_id": item["order_id"], "needs_clarification": False}
        if component == "primary_router":
            output = json.dumps({"capability_requests": [] if failure == "recovery" else [binding],
                                 "confidence": 0.99, "control_signals": ["fabricated_tool_result"]
                                 if failure == "recovery" else [], "denied_disclosures": []})
        elif component == "recovery_planner":
            output = json.dumps({"capability_requests": [binding]})
        else:
            facts = [json.loads(entry["output"]) for entry in body["input"]
                     if entry.get("type") == "function_call_output"]
            assert len(facts) == 1
            output = f'{item["order_id"]}: {facts[0]["order"]["status"]}'
        payload = response(body["model"], output) if status == 200 else {
            "error": {"message": "local injected failure", "type": "server_error"}}
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            record["cancelled_peer_closed"] = True


def context_clean():
    variables = [telemetry._request, telemetry._span, telemetry._started, telemetry._failure,
                 telemetry._budget, request_execution._active_budget, request_execution._stage_budget,
                 request_execution._runtime_policy, request_execution._recovery_policy, retry_policy._state]
    return all(variable.get() is None for variable in variables)


def close_worker(loop, client):
    steps = ["stop_admission"]
    pending = list(asyncio.all_tasks(loop))
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    steps.append("finish_or_cancel_work")
    loop.run_until_complete(client.close())
    steps.append("close_client_on_owner")
    loop.run_until_complete(loop.shutdown_asyncgens())
    steps.append("drain_async_generators")
    loop.run_until_complete(loop.shutdown_default_executor())
    steps.append("shutdown_executor")
    row = {"pending_before_close": len(pending), "pending_tasks": len(asyncio.all_tasks(loop)),
           "open_generators": sum(g.ag_frame is not None for g in loop._asyncgens),
           "client_closed": client.is_closed(), "pool_connections": len(client._client._transport._pool.connections)}
    loop.close()
    asyncio.set_event_loop(None)
    steps.append("close_loop")
    row.update(steps=steps, loop_closed=loop.is_closed())
    return row


def production(workers, faults, tracing):
    from threading import Event
    endpoint = Endpoint(workers)
    endpoint.RequestHandlerClass = BusinessHandler
    endpoint.requests = {}
    audit = Audit(endpoint)
    rows, lifecycles, errors, loop_errors = [], [], [], []
    original_config = model_execution.model_run_config
    original_tool = support_agent.orders.get_order_status

    def config():
        item = CURRENT.get()
        item["phase"] = telemetry._span.get().component
        return {**original_config(), "model_provider": item["provider"], "tracing_disabled": not tracing}

    def observed_tool(order_id):
        CURRENT.get()["tools"].append(order_id)
        return original_tool(order_id)

    model_execution.model_run_config = config
    support_agent.orders.get_order_status = observed_tool
    kinds = (["ok", "429", "ok", "500", "ok", "cancel", "ok", "synthesis_failure", "ok", "late", "ok", "recovery", "ok"]
             if faults else ["ok", "recovery", "ok"])

    def worker(number):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(lambda loop, context: loop_errors.append(str(context)))
        client = audit.client()
        owner = (get_ident(), id(loop))
        async def ownership(request):
            assert (get_ident(), id(asyncio.get_running_loop())) == owner
            CURRENT.get().setdefault("dispatch_owners", []).append(owner)
        audit.clients[-1].event_hooks["request"].append(ownership)
        try:
            for index, kind in enumerate(kinds):
                tag = f"worker-{number}-request-{index}"
                item = {"tag": tag, "round": index, "kind": kind, "order_id": f"ORD-100{1 + (number + index) % 2}",
                        "clock": [0.0], "loop": loop, "phase": "admission", "tools": [], "cancel_drained": Event()}
                endpoint.requests[tag] = item
                item["provider"] = OpenAIProvider(openai_client=client.with_options(default_headers={"X-Qualification-Request": tag}))
                budget = RequestBudget(20000, clock=lambda: item["clock"][0])
                token = CURRENT.set(item)
                row = {"tag": tag, "kind": kind, "worker": number, "owner": owner, "order_id": item["order_id"],
                       "budget_id": budget.request_id, "deadline": budget.deadline_monotonic, "error": None}
                try:
                    result = support_agent.run_support_agent_detailed(f'Where is {item["order_id"]}?', request_budget=budget,
                                                                      request_label=tag)
                    observed = result.context_wrapper.production_telemetry
                    row.update(output=result.final_output, execution=result.context_wrapper.context.snapshot())
                except BaseException as error:
                    row["error"] = type(error).__name__
                    row["error_chain"] = error_snapshot(error)
                    observed = getattr(error, "production_telemetry", None)
                finally:
                    item["cancel_drained"].set()
                    CURRENT.reset(token)
                row.update(telemetry=telemetry.snapshot(observed), tools=item["tools"], context_clean=context_clean(),
                           dispatch_owners=item.get("dispatch_owners", []),
                           client_open=not client.is_closed(), loop_reused=asyncio.get_event_loop() is loop,
                           deadline_after=budget.deadline_monotonic, pending_tasks=len(asyncio.all_tasks(loop)))
                rows.append(row)
        except BaseException as error:
            errors.append(repr(error))
        finally:
            try:
                lifecycles.append(close_worker(loop, client))
            except BaseException as error:
                errors.append(repr(error))

    threads = [Thread(target=worker, args=(n,), daemon=True) for n in range(workers)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(25)
    finally:
        model_execution.model_run_config = original_config
        support_agent.orders.get_order_status = original_tool
        alive = endpoint.finish()
    return {"rows": rows, "lifecycle": lifecycles, "errors": errors, "loop_errors": loop_errors,
            "workers_alive": sum(t.is_alive() for t in threads), "server_threads_alive": alive,
            "server_errors": endpoint.errors, "http": endpoint.records,
            "clients": len(audit.clients), "pools": len({id(c._transport._pool) for c in audit.clients})}


def control(mode, calls, tracing):
    endpoint = Endpoint()
    audit = Audit(endpoint)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = audit.client()
    config = audit.config(client)
    config.tracing_disabled = not tracing
    rows = []
    try:
        if mode == "async":
            async def tasks():
                endpoint.gate("held")
                held = asyncio.create_task(audit.invoke("held", config))
                assert await asyncio.to_thread(endpoint.arrivals["held"].wait, 4)
                held.cancel()
                await asyncio.gather(held, return_exceptions=True)
                endpoint.gates["held"].set()
                for wave in range(2):
                    await asyncio.gather(*(audit.invoke(f"control-{wave}-{n}", config) for n in range(calls)))
            loop.run_until_complete(tasks())
            rows = audit.rows
        else:
            for n in range(calls):
                token = CURRENT.set({"tag": str(n), "phase": mode})
                try:
                    if mode == "direct":
                        loop.run_until_complete(audit.invoke(f"direct-{n}", config, direct_client=client))
                    else:
                        result = Runner.run_sync(Agent(name="local control", tools=[]), f"runner-{n}", run_config=config)
                        assert result.final_output == f"runner-{n}"
                    rows.append({"call": n, "client_open": not client.is_closed()})
                finally:
                    CURRENT.reset(token)
    finally:
        lifecycle = close_worker(loop, client)
        alive = endpoint.finish()
    return {"rows": rows, "lifecycle": [lifecycle], "http": endpoint.records,
            "server_errors": endpoint.errors, "server_threads_alive": alive}


def main():
    mode, count, tracing, path = sys.argv[1:]
    sys.addaudithook(network_guard)
    processor = LocalTracing()
    set_trace_processors([processor])  # Public local processor; no exporter exists.
    set_tracing_disabled(tracing != "on")
    warnings_observer = WarningEvidence()
    result = (production(int(count), mode == "faults", tracing == "on") if mode in {"production", "faults"}
              else control(mode, int(count), tracing == "on"))
    result.update(warnings_observer.finish(), mode=mode, count=int(count), tracing=tracing,
                  trace_starts=processor.starts, active_trace_items=len(processor.active))
    processor.shutdown()
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)


if __name__ == "__main__":
    main()
