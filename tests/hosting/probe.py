"""Real production runtime + owned transports, only a local HTTP fixture."""
import asyncio
from dataclasses import asdict
import json
from io import BytesIO
from pathlib import Path
import sys
from threading import Event, Lock, Thread, get_ident

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents import set_trace_processors, set_tracing_disabled
from agents.models import openai_provider
from src.agent import telemetry, support_agent
from src.hosting.runtime import WorkerRuntime
from src.hosting.collector import CampaignCollector
from tests.production_transport.probe import BusinessHandler, LocalTracing, WarningEvidence
from tests.transport_ownership.probe import Audit, Endpoint, network_guard


class ControlledHandler(BusinessHandler):
    def do_POST(self):
        # Hold only after the complete dispatch arrived. Holding after headers
        # could let intentional cancellation truncate the body before parsing.
        wire = self.rfile
        size = int(self.headers["Content-Length"])
        body = wire.read(size)
        assert len(body) == size
        tag = self.headers["X-Qualification-Request"]
        item = self.server.requests[tag]
        if self.headers["X-Qualification-Component"] == "primary_router":
            item["arrived"].set()
            assert item["release"].wait(8)
        self.rfile = BytesIO(body)
        try:
            super().do_POST()
        finally:
            self.rfile = wire


def main():
    mode, destination = sys.argv[1:]
    sys.addaudithook(network_guard)
    set_trace_processors([LocalTracing()])
    set_tracing_disabled(True)
    observer = WarningEvidence()
    workers, capacity, total = (5, 10, 25) if mode in {"shape5", "mixed"} else (2, 4, 10) if mode == "shape2" else (1, 2, 3)
    endpoint = Endpoint()
    endpoint.RequestHandlerClass = ControlledHandler
    endpoint.requests = {}
    audit, stamps, tools, lock = Audit(endpoint), [], [], Lock()
    now = [0.0]

    def no_shared():
        raise AssertionError("Concurrency path fell back to shared HTTP client")
    openai_provider.shared_http_client = no_shared
    original = support_agent.orders.get_order_status
    def observed_tool(order_id):
        with lock:
            tools.append((telemetry._request.get().external_label, order_id))
        return original(order_id)
    support_agent.orders.get_order_status = observed_tool

    def factory():
        client = audit.client()
        owner = (get_ident(), id(asyncio.get_event_loop()))
        async def stamp(request):
            record = telemetry._request.get()
            tag = record.external_label
            item = endpoint.requests[tag]
            item["loop"] = asyncio.get_running_loop()
            request.headers["X-Qualification-Request"] = tag
            request.headers["X-Qualification-Component"] = telemetry._span.get().component
            assert (get_ident(), id(asyncio.get_running_loop())) == owner
            stamps.append({"tag": tag, "owner": owner, "request_id": record.request_id})
        audit.clients[-1].event_hooks["request"].append(stamp)
        return client

    service = WorkerRuntime(workers, capacity, client_factory=factory, clock=lambda: now[0])
    collector = CampaignCollector(clock=lambda: now[0], measure_rates=False)
    admissions, completions = [], []

    def offer(n, *, kind="ok", held=False):
        tag = f"request-{n}"
        item = {"kind": kind, "round": 1, "order_id": f"ORD-100{1+n%2}", "clock": now,
                "cancel_drained": Event(), "arrived": Event(), "release": Event()}
        if not held:
            item["release"].set()
        endpoint.requests[tag] = item
        admission = service.submit(f'Where is {item["order_id"]}?', request_label=tag)
        collector.observe(admission)
        admissions.append(admission)
        return admission

    def finish(admission):
        completed = admission.result(12)
        collector.record(completed)
        completions.append(completed)
        return completed

    before = None
    try:
        if mode == "mixed":
            offer(0, held=True)
            assert endpoint.requests["request-0"]["arrived"].wait(8)
            now[0] = 21  # Only the older ingress deadline expires.
            for n, kind in enumerate(["ok", "429", "500", "synthesis_failure"], 1):
                offer(n, kind=kind, held=True)
            assert all(endpoint.requests[f"request-{n}"]["arrived"].wait(8) for n in range(1, 5))
            cancelled = offer(5, held=True)
            before = service.status()
            for n in range(5):
                endpoint.requests[f"request-{n}"]["release"].set()
            assert endpoint.requests["request-5"]["arrived"].wait(8)
            assert service.cancel(cancelled.request_id)
            for admission in admissions:
                finish(admission)
            endpoint.requests["request-5"]["release"].set()
            finish(offer(6, kind="recovery"))
        elif mode in {"shape2", "shape5"}:
            for n in range(workers):
                offer(n, held=True)
            assert all(endpoint.requests[f"request-{n}"]["arrived"].wait(8) for n in range(workers))
            for n in range(workers, total):
                offer(n)
            before = service.status()
            now[0] = 0.1
            for item in endpoint.requests.values():
                item["release"].set()
            for admission in admissions:
                if admission.accepted:
                    finish(admission)
        elif mode in {"deadline", "queued_cancel", "shutdown", "drain"}:
            first = offer(0, held=True)
            assert endpoint.requests["request-0"]["arrived"].wait(8)
            second = offer(1)
            before = service.status()
            if mode == "deadline":
                now[0] = 21
            elif mode == "queued_cancel":
                assert service.cancel(second.request_id)
            elif mode == "shutdown":
                service.shutdown(timeout=4, cancel=True)
            elif mode == "drain":
                failures = []
                def drain():
                    try:
                        service.shutdown(timeout=8)
                    except BaseException as error:
                        failures.append(type(error).__name__)
                closer = Thread(target=drain)
                closer.start()
                with service._condition:
                    assert service._condition.wait_for(lambda: not service._accepting, 2)
                endpoint.requests["request-0"]["release"].set()
                closer.join(9)
                assert not closer.is_alive() and not failures
            endpoint.requests["request-0"]["release"].set()
            finish(first)
            finish(second)
            if mode not in {"shutdown", "drain"}:
                finish(offer(2))
        elif mode == "faults":
            for n, kind in enumerate(["ok", "429", "ok", "500", "ok", "synthesis_failure", "ok", "ok", "recovery"]):
                admission = offer(n, kind=kind, held=n == 7)
                if n == 7:
                    assert endpoint.requests[f"request-{n}"]["arrived"].wait(8)
                    assert service.cancel(admission.request_id)
                finish(admission)
                endpoint.requests[f"request-{n}"]["release"].set()
        else:
            for n in range(3):
                finish(offer(n, kind="recovery" if n == 1 else "ok"))
    finally:
        for item in endpoint.requests.values():
            item["release"].set()
            item["cancel_drained"].set()
        service.shutdown(timeout=8)
        alive = endpoint.finish()
    status = service.status()
    denied = service.submit("after shutdown")
    assert not denied.accepted
    # Admission-after-shutdown is a separate check, outside the measured campaign.
    report = collector.snapshot(status)
    publication = collector.publish(Path(destination).parent, status, filename=Path(destination).name)
    evidence = {"report": str(publication), "campaign": report, "before_release": before,
                "completed": [asdict(r) for r in completions], "http": endpoint.records, "stamps": stamps,
                "tools": tools, "server_errors": endpoint.errors, "server_threads_alive": alive,
                "closed_clients": [c.is_closed for c in audit.clients],
                "pool_connections": [len(c._transport._pool.connections) for c in audit.clients],
                **observer.finish()}
    Path(str(destination) + ".evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
