"""Real TCP/Runner/production orchestration, bounded isolated offline processes."""
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import socket
import sys

import pytest

OBSERVATIONS = []


def test_truncated_http_dispatch_is_retained_as_transport_failure():
    from tests.transport_ownership.probe import Endpoint, KEY
    endpoint = Endpoint()
    try:
        with socket.create_connection(("127.0.0.1", endpoint.server_port), timeout=4) as connection:
            connection.sendall(("POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                f"Authorization: Bearer {KEY}\r\nContent-Length: 10\r\n\r\n{{").encode())
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(1) == b""
        assert endpoint.truncated_requests == [{"connection": 0, "expected_bytes": 10, "received_bytes": 1}]
        assert not endpoint.records and not endpoint.errors
    finally:
        assert endpoint.finish() == 0


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    directory, cached = tmp_path_factory.mktemp("production_transport"), {}

    def run(mode, count=1, tracing="off"):
        key = (mode, count, tracing)
        if key not in cached:
            path = directory / f"{mode}-{count}-{tracing}.json"
            # Inherit stderr: no redirect, catch_warnings, or suppression. -W always
            # enables ResourceWarnings normally ignored by Python's default filter.
            subprocess.run([sys.executable, "-W", "always", str(Path(__file__).with_name("probe.py")),
                            mode, str(count), tracing, str(path)], check=True, timeout=60)
            report = json.loads(path.read_text(encoding="utf-8"))
            cached[key] = report
            OBSERVATIONS.append(report)
        return cached[key]
    yield run
    target = os.environ.get("AGENTGUARD_PRODUCTION_TRANSPORT_REPORT")
    if target:
        with Path(target).open("x", encoding="utf-8") as stream:
            json.dump(OBSERVATIONS, stream, indent=2)


def clean(report):
    assert report["server_threads_alive"] == report["active_trace_items"] == report["open_warned_generators"] == 0
    assert not report["server_errors"]
    assert not report.get("errors") and not report.get("loop_errors") and not report.get("workers_alive")
    for row in report["lifecycle"]:
        assert row["pending_tasks"] == row["pending_before_close"] == row["open_generators"] == row["pool_connections"] == 0
        assert row["client_closed"] and row["loop_closed"]
        assert row["steps"] == ["stop_admission", "finish_or_cancel_work", "close_client_on_owner",
                                 "drain_async_generators", "shutdown_executor", "close_loop"]
    assert all(w["disposition"] == "SDK_WARNING_WITH_PROVEN_CLEAN_RESOURCE_STATE" for w in report["warnings"])


@pytest.mark.parametrize("workers", [1, 2, 5])
def test_production_path_worker_ownership_and_isolation(experiment, workers):
    report = experiment("production", workers)
    clean(report)
    assert report["clients"] == report["pools"] == workers
    assert len(report["rows"]) == workers * 3
    assert len(report["http"]) == workers * 7
    assert len({r["budget_id"] for r in report["rows"]}) == workers * 3
    call_ids, operation_ids = [], []
    for row in report["rows"]:
        assert row["error"] is None
        assert row["context_clean"] and row["client_open"] and row["loop_reused"]
        assert row["deadline"] == row["deadline_after"] == 20
        assert row["pending_tasks"] == 0
        assert row["tools"] == [row["order_id"]]
        trace = row["execution"]
        assert len(trace["required_operations"]) == len(trace["completed_required_operations"]) == 1
        assert not trace["missing_required_operations"]
        assert row["output"] == f'{row["order_id"]}: {trace["operations"][0]["output"]["order"]["status"]}'
        operation_ids.extend(trace["required_operations"])
        assert row["telemetry"]["request_id"] == row["budget_id"]
        assert row["telemetry"]["external_label"] == row["tag"]
        attempts = [a for span in row["telemetry"]["component_spans"] for a in span["attempts"]]
        assert len(attempts) == (3 if row["kind"] == "recovery" else 2)
        assert len(row["dispatch_owners"]) == len(attempts)
        assert all(owner == row["owner"] for owner in row["dispatch_owners"])
        assert all(a["attempt_number"] == 1 and not a["retry_performed"] for a in attempts)
        call_ids.extend(a["logical_call_id"] for a in attempts)
        dispatches = [r for r in report["http"] if r["tag"] == row["tag"]]
        assert Counter(r["component"] for r in dispatches) == dict.fromkeys(
            ["primary_router", "synthesis"] + (["recovery_planner"] if row["kind"] == "recovery" else []), 1)
        assert all(r["retry"] == "0" and r["body"]["tools"] == [] for r in dispatches)
        synthesis = dispatches[-1]["body"]["input"]
        assert sum(i.get("type") == "function_call_output" for i in synthesis) == 1
    assert len(set(call_ids)) == len(call_ids)
    assert len(set(operation_ids)) == len(operation_ids)
    for worker in range(workers):
        rows = [r for r in report["rows"] if r["worker"] == worker]
        assert len({tuple(r["owner"]) for r in rows}) == 1
        tags = {r["tag"] for r in rows}
        assert len({r["connection"] for r in report["http"] if r["tag"] in tags}) == 1
    assert len({tuple(r["owner"]) for r in report["rows"]}) == workers


@pytest.mark.parametrize("kind,error,dispatches", [
    ("429", "RateLimitError", 1), ("500", "InternalServerError", 1),
    ("cancel", "CancelledError", 1), ("synthesis_failure", "InternalServerError", 2),
    ("late", "RequestDeadlineExceeded", 1),
])
def test_fault_then_success_on_same_client(experiment, kind, error, dispatches):
    report = experiment("faults")
    clean(report)
    rows = report["rows"]
    index = next(i for i, r in enumerate(rows) if r["kind"] == kind)
    failed, after = rows[index:index + 2]
    assert error in [e["type"] for e in failed["error_chain"]] and after["error"] is None
    assert failed["owner"] == after["owner"]
    assert failed["budget_id"] != after["budget_id"]
    assert all(r["context_clean"] and r["pending_tasks"] == 0 for r in rows)
    assert len([r for r in report["http"] if r["tag"] == failed["tag"]]) == dispatches
    assert failed["tools"] == ([failed["order_id"]] if kind == "synthesis_failure" else [])
    assert after["tools"] == [after["order_id"]]
    assert all(r["retry"] == "0" for r in report["http"])
    assert max(Counter((r["tag"], r["component"]) for r in report["http"]).values()) == 1
    assert all(r["deadline"] == r["deadline_after"] == 20 for r in rows)
    assert all(r["telemetry"]["request_id"] == r["budget_id"] for r in rows)
    if kind == "late":
        assert failed["telemetry"]["result_abandoned"]
        assert failed["telemetry"]["deadline_exhausted"]


@pytest.mark.parametrize("calls", [1, 3, 5])
def test_direct_transport_is_warning_free(experiment, calls):
    report = experiment("direct", calls)
    clean(report)
    assert not report["warnings"]
    assert len(report["http"]) == calls
    assert len({r["connection"] for r in report["http"]}) == 1


def test_runner_warning_scaling_and_taxonomy(experiment):
    reports = [experiment("runner", calls) for calls in (1, 3, 5)]
    for report in reports:
        clean(report)
        assert len(report["http"]) == report["count"]
        assert all(w["request"] != "0" for w in report["warnings"])
        assert all(w["filename"].endswith("base_events.py") for w in report["warnings"])
    counts = [len(r["warnings"]) for r in reports]
    assert counts[0] == 0 and counts[1] > 0 and counts[2] == 2 * counts[1]


def test_tracing_does_not_cause_warnings(experiment):
    off, on = experiment("runner", 3), experiment("runner", 3, "on")
    clean(on)
    assert off["trace_starts"] == 0 and on["trace_starts"] == 3
    assert Counter(w["generator"] for w in on["warnings"]) == Counter(w["generator"] for w in off["warnings"])


@pytest.mark.parametrize("tracing", ["on", "off"])
def test_architecture_c_cancellation_reuse_and_shutdown(experiment, tracing):
    report = experiment("async", 3, tracing)
    clean(report)
    assert not report["warnings"]
    assert len(report["http"]) == 7
    assert sum(r["cancelled"] for r in report["rows"]) == 1
    assert sum(r["completed"] for r in report["rows"]) == 6
    assert not any(r["error"] or r["mapping_error"] for r in report["rows"])
