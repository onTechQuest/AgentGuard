"""Each risky ownership experiment is killed on timeout, never left in pytest."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = Path(__file__).with_name("probe.py")
OBSERVATIONS = pytest.StashKey[list]()


@pytest.fixture
def experiment(request):
    def execute(mode, count=1):
        try:
            completed = subprocess.run([sys.executable, str(PROBE), mode, str(count)],
                                       capture_output=True, text=True, timeout=35)
        except subprocess.TimeoutExpired as error:
            # Only the deliberately unsafe shared-loop experiment may produce an
            # expected architecture timeout. Missing checkpoints fail the test.
            assert mode == "shared", "Supported ownership or fixture hung"
            output = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout
            report = json.loads(output.strip().splitlines()[-1])
            assert report["partial"]
            report.update(mode=mode, count=count, subprocess_timeout=True,
                          cleanup_status="unknown_after_forced_process_termination")
            request.config.stash.setdefault(OBSERVATIONS, []).append(report)
            return report
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        report["stderr"] = completed.stderr
        request.config.stash.setdefault(OBSERVATIONS, []).append(report)
        assert report["request_mapping_errors"] == report["hidden_retries"] == 0, report
        assert report["server_threads_alive"] == 0
        assert not report["server_errors"], report["server_errors"]
        if mode not in {"shared", "closed"}:
            assert report["truncated_http_requests"] == 0, report["truncated_http_evidence"]
        assert not any(row.get("gate_timeout") for row in report["http_records"]), "Local gate was never released"
        assert all(limits == {"max_connections": 1000, "max_keepalive_connections": 100}
                   for limits in report["pool_limits"]), "Pool limits changed"
        if mode not in {"shared", "closed"}:
            assert "Task was destroyed" not in completed.stderr
        return report
    return execute


def clean(report):
    for key in ("transport_exceptions", "loop_affinity_errors", "cleanup_failures", "pending_tasks_at_close",
                "clients_left_open", "pooled_connections_after_cleanup", "worker_join_timeouts"):
        assert report[key] == 0, (key, report)
    # The installed Runner calls shutdown_asyncgens after each run_sync and emits
    # ResourceWarning on later runs in the same loop. Retain those advisories;
    # do not equate them with an unclosed transport or suppress SDK warnings.
    assert all("was scheduled after loop.shutdown_asyncgens()" in message
               for message in report["resource_warnings"]), report["resource_warnings"]
    assert not report["worker_errors"], report["worker_errors"]
    assert not any(text in report["stderr"] for text in ("ResourceWarning", "Exception ignored in:", "Task was destroyed"))
    assert report["http_request_count"] == report["requests_attempted"]


@pytest.mark.parametrize("count", [2, 10, 25])
def test_same_loop_shared_client(count, experiment):
    report = experiment("same", count)
    clean(report)
    assert report["requests_completed"] == count * 2
    assert report["client_count"] == report["pool_count"] == 1
    assert report["server_connection_count"] == count
    assert all(row["client_open_after_request"] for row in report["lifecycle"])
    assert len({row["loop"] for row in report["rows"]}) == 1


@pytest.mark.parametrize("count", [2, 5])
def test_shared_client_across_thread_loops_is_observational(count, experiment):
    report = experiment("shared", count)
    assert report["client_count"] == report["pool_count"] == 1
    assert 0 < report["requests_attempted"] <= count * 2
    assert len({row["loop"] for row in report["rows"]}) == count
    assert len({row["thread"] for row in report["rows"]}) == count
    assert report["http_request_count"] <= report["requests_attempted"]
    # No assertion of success: a clean sample cannot qualify cross-loop ownership.
    # Exceptions and cleanup failures are retained in the scorecard, not patched.


@pytest.mark.parametrize("count", [2, 5])
def test_worker_owned_clients_reuse_and_close(count, experiment):
    report = experiment("owned", count)
    clean(report)
    assert report["requests_completed"] == count * 2
    assert report["client_count"] == report["pool_count"] == count
    assert report["server_connection_count"] == count
    records = {row["tag"]: row for row in report["http_records"]}
    for index in range(count):
        assert records[f"worker-{index}-0"]["connection"] == records[f"worker-{index}-1"]["connection"]
    assert all(not row["client_open"] for row in report["lifecycle"])


def test_closed_owner_loop_cannot_qualify_shared_client(experiment):
    report = experiment("closed")
    assert report["lifecycle"][0]["client_open"]
    assert report["lifecycle"][0]["pooled_connections"] == 1
    assert report["rows"][0]["completed"]
    assert report["transport_exceptions"] + report["cleanup_failures"] > 0


def test_sdk_lazy_initialization_race(experiment):
    report = experiment("init", 5)
    assert report["initialization_races"] == 4
    state = report["lifecycle"][0]
    assert state["created"] == state["returned_clients"] == 5
    assert state["authoritative_clients"] == 1
    assert state["non_authoritative_open_clients"] == 4
    assert report["http_request_count"] == 0  # Lifecycle race, not model failure evidence.
    assert report["clients_left_open"] == 0  # Test explicitly cleans every raced allocation.


@pytest.mark.parametrize("mode", ["cancel", "cancel_direct", "shutdown"])
def test_cancellation_cleanup(mode, experiment):
    report = experiment(mode)
    clean(report)
    assert sum(row.get("cancelled", False) for row in report["rows"]) == 1
    assert any(row.get("local_cancelled") for row in report["lifecycle"])
    assert report["requests_completed"] == (0 if mode == "shutdown" else 1)


def test_runner_creates_reuses_and_leaves_loop_open(experiment):
    report = experiment("runner")
    assert report["requests_completed"] == 2 and report["transport_exceptions"] == 1
    assert report["http_request_count"] == 3 and report["hidden_retries"] == 0
    assert "RateLimitError" in {item["type"] for row in report["rows"] for item in row["error"]}
    assert all(row["open_after_run"] and row["pending_tasks"] == 0 for row in report["lifecycle"])
    assert report["cleanup_failures"] == report["clients_left_open"] == 0


def test_delayed_response_allows_unrelated_task_to_finish(experiment):
    report = experiment("delayed")
    clean(report)
    assert report["requests_completed"] == 2
    assert report["lifecycle"][0]["peer_completed_before_release"]


@pytest.mark.parametrize("url,key", [("https://api.openai.com/v1", "offline-ownership-test-key"),
                                    ("http://127.0.0.1:12345/v1", "not-a-test-key"),
                                    ("http://localhost:12345/v1", "offline-ownership-test-key")])
def test_target_validation_fails_closed(url, key):
    from .probe import validate_target
    with pytest.raises(ValueError):
        validate_target(url, key)


def test_outbound_guard_denies_external_dns_and_socket():
    from .probe import network_guard
    with pytest.raises(RuntimeError, match="External socket"):
        network_guard("socket.connect", (None, ("8.8.8.8", 443)))
    with pytest.raises(RuntimeError, match="External DNS"):
        network_guard("socket.getaddrinfo", ("api.openai.com", 443))


def test_http_hook_rejects_misconfigured_endpoint_before_connect(experiment):
    report = experiment("guard")
    assert report["http_request_count"] == 0
    assert report["transport_exceptions"] == 1
    assert "ValueError" in {item["type"] for item in report["rows"][0]["error"]}
