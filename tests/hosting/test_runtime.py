from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace
import time

import pytest

from src.hosting.collector import CampaignCollector
from src.hosting.runtime import WorkerRuntime, AdmissionRejected, ShutdownTimeout, StartupFailure
from src.hosting import runtime as runtime_module


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    directory = Path(os.environ["AGENTGUARD_HOSTING_REPORT_DIR"]) if os.environ.get("AGENTGUARD_HOSTING_REPORT_DIR") else tmp_path_factory.mktemp("hosting")
    directory.mkdir(parents=True, exist_ok=True)
    cache = {}
    def run(mode):
        if mode not in cache:
            path = directory / f"{mode}.json"
            subprocess.run([sys.executable, "-W", "always", str(Path(__file__).with_name("probe.py")), mode, str(path)],
                           check=True, timeout=50)
            cache[mode] = json.loads(Path(str(path) + ".evidence.json").read_text())
        return cache[mode]
    return run


def clean(result):
    assert not result["server_errors"] and result["server_threads_alive"] == 0
    assert all(result["closed_clients"]) and not any(result["pool_connections"])
    assert result["open_warned_generators"] == 0
    assert all(w["disposition"] == "SDK_WARNING_WITH_PROVEN_CLEAN_RESOURCE_STATE" for w in result["warnings"])
    status = result["campaign"]["runtime"]
    assert status["queue_depth"] == status["active_workers"] == 0
    assert all(w["closed"] and not w["alive"] and w["error"] is None for w in status["workers"])
    assert all(w["lifecycle"] == ["stop_admission", "drain_work", "close_client", "drain_generators",
                                  "shutdown_executor", "close_loop"] for w in status["workers"])
    assert result["campaign"]["consumption"]["extra_retry_attempts"] == 0
    assert result["campaign"]["consumption"]["provider_dispatches"] == len(result["http"])
    assert all(v == 0 for v in result["campaign"]["isolation"].values())
    assert all(r["retry"] == "0" for r in result["http"])


@pytest.mark.parametrize("mode,workers,capacity,total", [("shape2", 2, 4, 10), ("shape5", 5, 10, 25)])
def test_bounded_campaign(mode, workers, capacity, total, experiment):
    result = experiment(mode)
    clean(result)
    campaign = result["campaign"]
    assert campaign["admission"] == {"offered": total, "admitted": workers + capacity,
                                      "rejected": total - workers - capacity, "max_observed_queue_depth": capacity}
    assert campaign["throughput"]["successful_completions"] == workers + capacity
    assert result["before_release"]["active_workers"] == workers
    assert result["before_release"]["queue_depth"] == capacity
    assert campaign["runtime"]["peak_active_requests"] == workers
    assert len(result["http"]) == 2 * (workers + capacity)
    assert len(result["tools"]) == workers + capacity
    assert len({tuple(s["owner"]) for s in result["stamps"]}) == workers
    owners = {}
    for stamp in result["stamps"]:
        owners.setdefault(stamp["request_id"], set()).add(tuple(stamp["owner"]))
    assert all(len(values) == 1 for values in owners.values())
    assert any(r["queue_wait_ms"] == 100 for r in result["completed"])
    assert all(r["deadline_monotonic"] == 20 for r in result["completed"])
    assert len({r["connection"] for r in result["http"]}) == workers


def test_one_worker_reuses_client_and_connection(experiment):
    result = experiment("reuse")
    clean(result)
    assert len(result["completed"]) == 3 and len(result["http"]) == 7
    assert len({r["connection"] for r in result["http"]}) == 1
    assert len(result["tools"]) == 3
    assert result["campaign"]["consumption"]["recovery_count"] == 1


def test_queued_deadline_uses_ingress_budget_and_dispatches_nothing(experiment):
    result = experiment("deadline")
    clean(result)
    first, second, after = result["completed"]
    assert first["outcome"] == second["outcome"] == "deadline_rejection"
    assert second["queue_wait_ms"] == 21000 and second["deadline_monotonic"] == 20
    assert json.loads(second["dispatches_json"]) == []
    assert not any(r["tag"] == "request-1" for r in result["http"])
    assert not any(tag == "request-1" for tag, _ in result["tools"])
    assert after["outcome"] == "success" and after["deadline_monotonic"] == 41
    assert result["campaign"]["reliability"]["late_result"] == 1


@pytest.mark.parametrize("mode", ["queued_cancel", "shutdown"])
def test_cancellation_and_bounded_shutdown(mode, experiment):
    result = experiment(mode)
    clean(result)
    assert result["completed"][1]["outcome"] == "cancellation"
    assert json.loads(result["completed"][1]["dispatches_json"]) == []
    if mode == "shutdown":
        assert result["completed"][0]["outcome"] == "cancellation"
    else:
        assert result["completed"][-1]["outcome"] == "success"


def test_faults_do_not_poison_worker(experiment):
    result = experiment("faults")
    clean(result)
    outcomes = [r["outcome"] for r in result["completed"]]
    assert outcomes == ["success", "rate_limit", "success", "provider_failure", "success",
                        "provider_failure", "success", "cancellation", "success"]
    assert len({r["request_id"] for r in result["completed"]}) == 9


def test_graceful_shutdown_drains_active_and_queued_requests(experiment):
    result = experiment("drain")
    clean(result)
    assert [r["outcome"] for r in result["completed"]] == ["success", "success"]
    assert len(result["http"]) == 4


def test_overlapping_failures_remain_request_local(experiment):
    result = experiment("mixed")
    clean(result)
    assert result["before_release"]["active_workers"] == 5
    assert [r["outcome"] for r in result["completed"]] == ["deadline_rejection", "success", "rate_limit",
        "provider_failure", "provider_failure", "cancellation", "success"]
    assert result["completed"][0]["deadline_monotonic"] == 20
    assert all(r["deadline_monotonic"] == 41 for r in result["completed"][1:])


@pytest.mark.parametrize("workers,capacity", [(0, 1), (-1, 1), (True, 1), (1, -1), (1, 1.5)])
def test_invalid_capacity_is_rejected_before_start(workers, capacity):
    with pytest.raises(ValueError):
        WorkerRuntime(workers, capacity)


def test_collector_rejects_foreign_thread():
    collector = CampaignCollector()
    errors = []
    def foreign():
        try:
            collector.snapshot({})
        except RuntimeError as error:
            errors.append(str(error))
    thread = Thread(target=foreign)
    thread.start()
    thread.join(2)
    assert len(errors) == 1 and not thread.is_alive()


def test_publication_is_valid_atomic_and_never_overwritten(tmp_path):
    collector = CampaignCollector()
    path = collector.publish(tmp_path, {}, filename="campaign.json")
    original = path.read_bytes()
    assert json.loads(original)["schema_version"] == 1
    with pytest.raises(FileExistsError):
        collector.publish(tmp_path, {}, filename="campaign.json")
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


class LifecycleClient:
    def __init__(self):
        self.owner = get_ident()
        self.closed = False
        self._client = SimpleNamespace(event_hooks={"request": []})

    def is_closed(self):
        return self.closed

    async def close(self):
        assert get_ident() == self.owner
        self.closed = True


def test_shutdown_timeout_retains_ownership_until_blocking_work_releases(monkeypatch):
    entered, release = Event(), Event()
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("test-only synchronous blockage")
    monkeypatch.setattr(runtime_module, "run_support_agent_detailed", blocking)
    service = WorkerRuntime(1, 0, client_factory=LifecycleClient)
    accepted = service.submit("offline")
    try:
        assert entered.wait(2)
        rejected = service.submit("full")
        assert not rejected.accepted
        with pytest.raises(AdmissionRejected):
            rejected.result()
        for _ in range(100):
            assert service.cancel(accepted.request_id)
        # Blocking synchronous work has not pumped the loop. Repeated cancellation
        # must not create an unbounded second queue of loop-control callbacks.
        assert len(service._workers[0].loop._ready) == 1
        started = time.monotonic()
        with pytest.raises(ShutdownTimeout) as caught:
            service.shutdown(timeout=0.05, cancel=True)
        assert caught.value.runtime is service
        assert time.monotonic() - started < 0.5
        assert service.status()["workers"][0]["alive"]
    finally:
        release.set()
        service.shutdown(timeout=2)
    assert not service.status()["workers"][0]["alive"]
    result = accepted.result()
    with pytest.raises(FrozenInstanceError):
        result.outcome = "modified"


def test_factory_failure_joins_workers_without_admitting_work():
    def broken():
        raise ValueError("offline initialization fault")
    with pytest.raises(StartupFailure) as caught:
        WorkerRuntime(2, 1, client_factory=broken)
    assert not any(w["alive"] for w in caught.value.runtime.status()["workers"])
    assert not caught.value.runtime.submit("stopped").accepted


def test_partial_thread_start_failure_closes_already_started_workers(monkeypatch):
    class StartFailureThread(Thread):
        def start(self):
            if self.name.endswith("-1"):
                raise RuntimeError("offline thread creation failure")
            super().start()
    monkeypatch.setattr(runtime_module, "Thread", StartFailureThread)
    with pytest.raises(StartupFailure) as caught:
        WorkerRuntime(2, 1, client_factory=LifecycleClient, startup_timeout=2)
    assert all(w["closed"] and not w["alive"] for w in caught.value.runtime.status()["workers"])


def test_factory_cannot_share_a_transport_between_workers():
    clients, lock = [], Lock()
    def shared():
        with lock:
            if not clients:
                clients.append(LifecycleClient())
            return clients[0]
    with pytest.raises(StartupFailure) as caught:
        WorkerRuntime(2, 1, client_factory=shared)
    assert all(not w["alive"] for w in caught.value.runtime.status()["workers"])
    assert clients[0].closed


def test_owned_provider_scope_rejects_foreign_loop_and_restores_default():
    import asyncio
    from src.agent.model_execution import model_run_config
    from src.agent.owned_transport import OwnedTransport
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    loop, foreign = asyncio.new_event_loop(), asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = LifecycleClient()
    owner = OwnedTransport(client, loop)
    try:
        assert "model_provider" not in model_run_config()
        with owner.request_scope():
            assert model_run_config()["model_provider"] is owner.provider
            asyncio.set_event_loop(foreign)
            with pytest.raises(RuntimeError, match="owning"):
                model_run_config()
            asyncio.set_event_loop(loop)
        assert "model_provider" not in model_run_config()
        loop.run_until_complete(client.close())
    finally:
        loop.close()
        foreign.close()
        asyncio.set_event_loop(previous)


def test_collector_detects_corrupted_isolation_evidence_and_unknown_usage():
    from concurrent.futures import Future
    from src.hosting.runtime import Admission, CompletedRequest
    collector = CampaignCollector(clock=lambda: 1)
    admission = Admission("request-a", True, None, 0, 0, 20, Future())
    collector.observe(admission)
    operation = {"invoked": True, "operation": {"tool": "read", "arguments": [["order_id", "A"]]},
                 "output": {"order": {"order_id": "B"}}}
    result = CompletedRequest("request-a", 0, "unknown_failure", 2, 3, 5, 20, None,
                              json.dumps({"request_id": "request-b", "usage_completeness": "UNAVAILABLE"}),
                              json.dumps({"operations": [operation, operation]}),
                              json.dumps([{"request_id": "request-c"}]))
    collector.record(result)
    report = collector.snapshot({})
    assert report["isolation"] == {"request_id_collision": 0, "mixed_tool_result": 2,
                                   "duplicate_operation": 1, "telemetry_contamination": 2}
    assert report["consumption"]["tokens_per_second"] is None
    assert report["consumption"]["average_known_tokens_per_request"] is None
    assert report["latency"]["end_to_end_ms"]["average"] == 5
    with pytest.raises(ValueError, match="Duplicate"):
        collector.observe(admission)
    with pytest.raises(ValueError, match="unique"):
        collector.record(result)
