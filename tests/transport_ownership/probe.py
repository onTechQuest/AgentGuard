"""Subprocess-contained experiment using the installed SDK and real TCP pool.

No mock HTTP transport: only the remote Responses endpoint is replaced by a
loopback HTTP/1.1 server. Unsafe lifecycle cases are observations, not fixes.
"""

import asyncio
from collections import Counter
from contextvars import ContextVar
import gc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import time
from threading import Barrier, Event, Lock, Thread, get_ident
import warnings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents import Agent, RunConfig, Runner, set_tracing_disabled
from agents.models import openai_provider
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from src.agent.model_execution import model_run_config


KEY = "offline-ownership-test-key"
BOUND = 4
TAG = ContextVar("transport_probe_tag", default=None)


def local_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def network_guard(event, args):
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or not local_host(address[0]):
            raise RuntimeError("External socket destination denied")
    elif event == "socket.getaddrinfo":
        if not local_host(args[0]):
            raise RuntimeError("External DNS destination denied")


def validate_target(base_url, key):
    from urllib.parse import urlsplit
    target = urlsplit(base_url)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port or key != KEY:
        raise ValueError("Only loopback HTTP and the fixed fake key are permitted")


def error_snapshot(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        chain.append({"type": type(error).__name__, "message": str(error)[:240]})
        error = error.__cause__ or error.__context__
    return chain


def response(model, tag):
    return {"id": "resp_" + tag, "object": "response", "created_at": 1, "status": "completed",
            "model": model, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"id": "msg_" + tag, "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": tag, "annotations": []}]}],
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}


class Endpoint(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # Local fixture backlog; client pool defaults are untouched.

    def __init__(self, overlap=1):
        self.lock = Lock()
        self.records, self.connections, self.handlers, self.errors, self.disconnects = [], [], [], [], []
        self.truncated_requests = []
        self.gates, self.arrivals = {}, {}
        self.overlap = Barrier(overlap) if overlap > 1 else None
        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_port}/v1"
        self.thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def gate(self, tag):
        self.gates[tag], self.arrivals[tag] = Event(), Event()

    def finish(self):
        for gate in self.gates.values():
            gate.set()
        self.shutdown()
        self.thread.join(BOUND)
        self.server_close()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for handler in self.handlers:
            handler.join(BOUND)
        return sum(thread.is_alive() for thread in [self.thread, *self.handlers])

    def process_request_thread(self, request, client_address):
        from threading import current_thread
        with self.lock:
            self.handlers.append(current_thread())
        super().process_request_thread(request, client_address)

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        with self.lock:
            target = self.disconnects if isinstance(error, (ConnectionError, TimeoutError)) else self.errors
            target.append(error_snapshot(error))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(BOUND)
        with self.server.lock:
            self.connection_id = len(self.server.connections)
            self.server.connections.append(self.connection)

    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/v1/responses"
        assert self.headers.get("Authorization") == "Bearer " + KEY
        expected_bytes = int(self.headers["Content-Length"])
        received = self.rfile.read(expected_bytes)
        if len(received) != expected_bytes:
            # Broken cross-loop transports can close after headers. Retain the
            # actual incomplete dispatch as transport evidence, not a JSON bug.
            with self.server.lock:
                self.server.truncated_requests.append({"connection": self.connection_id,
                                                       "expected_bytes": expected_bytes,
                                                       "received_bytes": len(received)})
            self.close_connection = True
            return
        body = json.loads(received)
        message = body["input"]
        if isinstance(message, list):
            content = next(item["content"] for item in message if item.get("role") == "user")
            message = content if isinstance(content, str) else content[0]["text"]
        tag = message
        record = {"tag": tag, "connection": self.connection_id, "sent": False, "write_error": None}
        with self.server.lock:
            self.server.records.append(record)
        if self.server.overlap and tag.endswith("-0"):
            self.server.overlap.wait(BOUND)
        if tag in self.server.gates:
            self.server.arrivals[tag].set()
            if not self.server.gates[tag].wait(BOUND):
                record["gate_timeout"] = True
        status = 429 if tag.startswith("rate-") else 200
        payload = {"error": {"message": "local rate limit", "type": "rate_limit_error"}} if status == 429 else response(body["model"], tag)
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            record["sent"] = True
        except OSError as error:
            record["write_error"] = type(error).__name__


class Audit:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.rows, self.clients, self.lifecycle, self.cleanup_errors = [], [], [], []
        self.lock = Lock()
        self.initialization_races = 0
        self.worker_join_timeouts = 0
        self.worker_errors = []

    def http_client(self):
        # Same installed default class and pool limits as production. Only proxy
        # inheritance and fixture timeouts change; retries remain off at SDK layers.
        async def destination_guard(request):
            validate_target(str(request.url), request.headers.get("authorization", "").removeprefix("Bearer "))
            if request.url.port != self.endpoint.server_port:
                raise ValueError("Only the allocated fixture port is permitted")
        client = DefaultAsyncHttpx2Client(trust_env=False, timeout=BOUND,
                                         event_hooks={"request": [destination_guard]})
        with self.lock:
            self.clients.append(client)
        return client

    def client(self):
        validate_target(self.endpoint.url, KEY)
        return AsyncOpenAI(api_key=KEY, base_url=self.endpoint.url, max_retries=0,
                           http_client=self.http_client())

    def config(self, client=None):
        config = {**model_run_config(), "tracing_disabled": True}
        if client is not None:
            assert client.max_retries == 0
            return RunConfig(**config, model_provider=OpenAIProvider(openai_client=client))
        return config

    async def invoke(self, tag, config, *, direct_client=None):
        token = TAG.set(tag)
        row = {"tag": tag, "thread": get_ident(), "loop": id(asyncio.get_running_loop()),
               "completed": False, "cancelled": False, "error": [], "mapping_error": False}
        with self.lock:
            self.rows.append(row)
        try:
            if direct_client is not None:
                result = await direct_client.responses.create(model="offline-model", input=tag)
                output = result.output_text
            else:
                result = await Runner.run(Agent(name=tag, model="offline-model", tools=[]), tag,
                                          run_config=config, max_turns=1)
                output = result.final_output
            row["completed"] = True
            row["mapping_error"] = output != tag or TAG.get() != tag
        except asyncio.CancelledError:
            row["cancelled"] = True
            raise
        except Exception as error:
            row["error"] = error_snapshot(error)
        finally:
            TAG.reset(token)
        return row

    async def close(self, client):
        try:
            await asyncio.wait_for(client.close(), BOUND)
        except Exception as error:
            with self.lock:
                self.cleanup_errors.append(error_snapshot(error))

    def record_loop(self, loop, **extra):
        tasks = list(asyncio.all_tasks(loop))
        with self.lock:
            self.lifecycle.append({"thread": get_ident(), "loop": id(loop), "pending_tasks": len(tasks), **extra})

    def summary(self):
        counts = Counter(row["tag"] for row in self.endpoint.records)
        errors = [row["error"] for row in self.rows if row["error"]]
        loop_errors = sum(any(any(marker in e["message"].lower() for marker in
                                  ("event loop", "different loop", "closed loop")) for e in chain)
                          for chain in errors + self.cleanup_errors)
        return {"truncated_http_requests": len(self.endpoint.truncated_requests),
                "truncated_http_evidence": list(self.endpoint.truncated_requests),
                "requests_attempted": len(self.rows), "requests_completed": sum(row["completed"] for row in self.rows),
                "request_mapping_errors": sum(row["mapping_error"] for row in self.rows),
                "transport_exceptions": len(errors), "loop_affinity_errors": loop_errors,
                "initialization_races": self.initialization_races,
                "cleanup_failures": len(self.cleanup_errors) + self.worker_join_timeouts + len(self.worker_errors),
                "pending_tasks_at_close": sum(row["pending_tasks"] for row in self.lifecycle),
                "clients_left_open": sum(not client.is_closed for client in self.clients),
                "pooled_connections_after_cleanup": sum(len(client._transport._pool.connections) for client in self.clients),
                "hidden_retries": sum(max(0, count - 1) for count in counts.values()),
                "http_request_count": len(self.endpoint.records), "client_count": len(self.clients),
                "pool_count": len({id(client._transport._pool) for client in self.clients}),
                "pool_limits": [{"max_connections": client._transport._pool._max_connections,
                                 "max_keepalive_connections": client._transport._pool._max_keepalive_connections}
                                for client in self.clients],
                "server_connection_count": len(self.endpoint.connections), "rows": self.rows,
                "worker_join_timeouts": self.worker_join_timeouts, "worker_errors": self.worker_errors,
                "http_records": self.endpoint.records, "lifecycle": self.lifecycle, "cleanup_errors": self.cleanup_errors}


async def same_loop(audit, count):
    client = audit.client()
    config = audit.config(client)
    await asyncio.wait_for(asyncio.gather(*(audit.invoke(f"task-{i}-0", config) for i in range(count))), BOUND * 2)
    audit.lifecycle.append({"pending_tasks": len([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]),
                            "client_open_after_request": not client.is_closed()})
    # A second sequential wave observes reuse of existing pooled TCP connections.
    for i in range(count):
        await audit.invoke(f"task-{i}-1", config)
    await audit.close(client)


def threaded(audit, count, shared):
    barrier = Barrier(count)
    def worker(index):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = None if shared else audit.client()
        config = audit.config(client)
        try:
            for repetition in range(2):
                barrier.wait(BOUND)
                tag = f"worker-{index}-{repetition}"
                row = {"tag": tag, "thread": get_ident(), "loop": id(loop), "completed": False,
                       "mapping_error": False, "cancelled": False, "error": []}
                with audit.lock:
                    audit.rows.append(row)
                try:
                    result = Runner.run_sync(Agent(name=tag, model="offline-model", tools=[]), tag,
                                             run_config=config, max_turns=1)
                    row["completed"] = True
                    row["mapping_error"] = result.final_output != tag
                except Exception as error:
                    row["error"] = error_snapshot(error)
                assert asyncio.get_event_loop() is loop and not loop.is_closed()
            if client:
                loop.run_until_complete(audit.close(client))
        finally:
            audit.record_loop(loop, client_open=not client.is_closed() if client else None)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_default_executor(), BOUND))
            loop.close()
            asyncio.set_event_loop(None)
    def bounded_worker(index):
        try:
            worker(index)
        except BaseException as error:
            with audit.lock:
                audit.worker_errors.append(error_snapshot(error))
    threads = [Thread(target=bounded_worker, args=(index,), daemon=True) for index in range(count)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + BOUND * 3
    for thread in threads:
        thread.join(max(0, end - time.monotonic()))
    audit.worker_join_timeouts = sum(thread.is_alive() for thread in threads)
    # Preserve evidence before any potentially broken cross-loop cleanup. A
    # parent subprocess timeout can retain this as an explicitly partial sample.
    if shared:
        print(json.dumps({**audit.summary(), "partial": True}), flush=True)
    if audit.worker_join_timeouts:
        return  # Unsupported ownership is recorded, never repaired/retried.
    if shared:
        # Observe the actual cross-loop cleanup; do not repair pool ownership.
        async def close_shared():
            try:
                await asyncio.wait_for(openai_provider._http_client.aclose(), BOUND)
            except Exception as error:
                audit.cleanup_errors.append(error_snapshot(error))
        asyncio.run(close_shared())


def closed_owner(audit):
    client = audit.client()
    config = audit.config(client)
    def owner():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(audit.invoke("owner-0", config))
        audit.record_loop(loop, client_open=not client.is_closed(),
                          pooled_connections=len(client._client._transport._pool.connections))
        loop.close()  # Deliberately wrong lifecycle, confined to this subprocess.
    worker = Thread(target=owner, daemon=True)
    worker.start()
    worker.join(BOUND * 2)
    assert not worker.is_alive(), "Owner failed to exit"
    async def foreign():
        await audit.invoke("foreign-1", config)
        await audit.close(client)
    asyncio.run(foreign())


def initialization_race(audit, count):
    barrier = Barrier(count)
    created, returned = [], []
    real_factory = openai_provider.DefaultAsyncHttpx2Client
    def construct():
        client = audit.http_client()
        with audit.lock:
            created.append(client)
        # Force every caller to pass the SDK's None check before assignment.
        barrier.wait(BOUND)
        return client
    openai_provider.DefaultAsyncHttpx2Client = construct
    def first_use():
        client = openai_provider.shared_http_client()
        with audit.lock:
            returned.append(client)
    threads = [Thread(target=first_use, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(BOUND * 2)
    assert not any(thread.is_alive() for thread in threads)
    openai_provider.DefaultAsyncHttpx2Client = real_factory
    audit.initialization_races = max(0, len(created) - 1)
    audit.lifecycle.append({"pending_tasks": 0, "created": len(created), "returned_clients": len({id(c) for c in returned}),
                            "authoritative_clients": sum(c is openai_provider._http_client for c in created),
                            "non_authoritative_open_clients": sum(c is not openai_provider._http_client and not c.is_closed for c in created)})
    async def cleanup():
        for client in created:
            await client.aclose()
    asyncio.run(cleanup())


async def cancellation(audit, direct=False):
    client = audit.client()
    config = audit.config(client)
    delayed = asyncio.create_task(audit.invoke("held", config, direct_client=client if direct else None))
    assert await asyncio.to_thread(audit.endpoint.arrivals["held"].wait, BOUND)
    assert not next(row for row in audit.endpoint.records if row["tag"] == "held")["sent"]
    delayed.cancel()
    try:
        await asyncio.wait_for(delayed, BOUND)
    except asyncio.CancelledError:
        pass
    audit.endpoint.gates["held"].set()
    await audit.invoke("healthy", config)
    await audit.close(client)
    audit.lifecycle.append({"pending_tasks": len([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]),
                            "local_cancelled": delayed.cancelled(), "remote_outcome_unknown_at_cancel": True})


async def delayed_response(audit):
    client = audit.client()
    config = audit.config(client)
    delayed = asyncio.create_task(audit.invoke("held", config))
    assert await asyncio.to_thread(audit.endpoint.arrivals["held"].wait, BOUND)
    await audit.invoke("peer", config)
    assert not delayed.done()
    audit.endpoint.gates["held"].set()
    await asyncio.wait_for(delayed, BOUND)
    await audit.close(client)
    audit.lifecycle.append({"pending_tasks": len([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]),
                            "delayed_completed": True, "peer_completed_before_release": True})


def in_flight_shutdown(audit):
    client = audit.client()
    loop = asyncio.new_event_loop()
    task = loop.create_task(audit.invoke("held", audit.config(client)))
    async def received():
        assert await asyncio.to_thread(audit.endpoint.arrivals["held"].wait, BOUND)
    loop.run_until_complete(received())
    # Cooperative shutdown sequence: cancel/drain tasks, then client, then loop.
    task.cancel()
    loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    audit.endpoint.gates["held"].set()
    loop.run_until_complete(audit.close(client))
    audit.record_loop(loop, local_cancelled=task.cancelled(), client_closed=client.is_closed())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()


def runner_creation(audit):
    asyncio.set_event_loop(None)
    client = audit.client()
    config = audit.config(client)
    loops = []
    for tag in ("created-0", "rate-1", "created-2"):
        row = {"tag": tag, "completed": False, "mapping_error": False, "error": []}
        try:
            result = Runner.run_sync(Agent(name=tag, model="offline-model"), tag, run_config=config, max_turns=1)
            row["completed"] = True
            row["mapping_error"] = result.final_output != tag
        except Exception as error:
            row["error"] = error_snapshot(error)
        audit.rows.append(row)
        loop = asyncio.get_event_loop()
        loops.append(id(loop))
        audit.record_loop(loop, open_after_run=not loop.is_closed())
    assert len(set(loops)) == 1
    async def nested_sync():
        try:
            Runner.run_sync(Agent(name="invalid nested", model="offline-model"), "must-not-dispatch", run_config=config)
        except RuntimeError as error:
            assert "event loop is already running" in str(error)
        else:
            raise AssertionError("Nested synchronous Runner unexpectedly ran")
    loop.run_until_complete(nested_sync())
    loop.run_until_complete(audit.close(client))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(asyncio.wait_for(loop.shutdown_default_executor(), BOUND))
    loop.close()
    asyncio.set_event_loop(None)


def main(mode, count):
    sys.addaudithook(network_guard)
    set_tracing_disabled(True)
    os.environ["OPENAI_API_KEY"] = KEY
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    endpoint = Endpoint(count if mode in {"same", "shared", "owned"} else 1)
    os.environ["OPENAI_BASE_URL"] = endpoint.url
    audit = Audit(endpoint)
    # Actual SDK shared-client factory, with test-only proxy and timeout controls.
    openai_provider.DefaultAsyncHttpx2Client = audit.http_client
    # Even base OpenAI wrappers have retries off; the SDK setting independently
    # suppresses its own replay paths. No production module is edited.
    real_openai = openai_provider.AsyncOpenAI
    def local_openai(**kwargs):
        validate_target(kwargs.get("base_url") or endpoint.url, KEY)
        return real_openai(**{**kwargs, "api_key": KEY, "max_retries": 0})
    openai_provider.AsyncOpenAI = local_openai
    if mode in {"cancel", "cancel_direct", "shutdown", "delayed"}:
        endpoint.gate("held")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        try:
            if mode == "same":
                asyncio.run(same_loop(audit, count))
            elif mode in {"shared", "owned"}:
                if mode == "shared":
                    openai_provider.shared_http_client()  # Separate initialization race experiment.
                threaded(audit, count, mode == "shared")
            elif mode == "closed":
                closed_owner(audit)
            elif mode == "init":
                initialization_race(audit, count)
            elif mode in {"cancel", "cancel_direct"}:
                asyncio.run(cancellation(audit, mode == "cancel_direct"))
            elif mode == "delayed":
                asyncio.run(delayed_response(audit))
            elif mode == "shutdown":
                in_flight_shutdown(audit)
            elif mode == "runner":
                runner_creation(audit)
            elif mode == "guard":
                async def rejected():
                    client = audit.client()
                    # The HTTP request hook must reject this BEFORE any connect
                    # (including Windows Proactor paths using ConnectEx).
                    foreign = client.with_options(base_url="http://203.0.113.1:12345/v1")
                    await audit.invoke("denied", audit.config(client), direct_client=foreign)
                    await audit.close(client)
                asyncio.run(rejected())
            else:
                raise ValueError("Unknown probe mode")
        finally:
            server_threads = endpoint.finish()
        gc.collect()
        report = audit.summary()
        report.update(mode=mode, count=count, server_threads_alive=server_threads,
                      observation_complete=not audit.worker_join_timeouts,
                      server_errors=endpoint.errors,
                      server_disconnects=endpoint.disconnects,
                      resource_warnings=[str(w.message) for w in caught if issubclass(w.category, ResourceWarning)],
                      versions={"python": sys.version.split()[0], **{name: version(name) for name in
                                ("openai-agents", "openai", "httpx2", "httpcore2", "anyio")}})
    print(json.dumps(report))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
