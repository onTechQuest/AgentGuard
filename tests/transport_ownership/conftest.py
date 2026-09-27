"""Terminal evidence and an optional new, never-overwritten qualification artifact."""

import json
import os
from pathlib import Path

from .test_qualification import OBSERVATIONS


def summarize(reports):
    groups = {"A": {"shared", "closed", "init"}, "B": {"owned"},
              "C": {"same", "cancel", "cancel_direct", "shutdown", "delayed"}}
    results = {}
    for name, modes in groups.items():
        rows = [row for row in reports if row["mode"] in modes]
        if not rows:
            continue
        fields = ("requests_attempted", "requests_completed", "http_request_count", "request_mapping_errors",
                  "transport_exceptions", "loop_affinity_errors", "initialization_races", "cleanup_failures",
                  "worker_join_timeouts", "pending_tasks_at_close", "clients_left_open", "hidden_retries",
                  "client_count", "pool_count", "pooled_connections_after_cleanup", "truncated_http_requests")
        result = {key: sum(row.get(key, 0) for row in rows) for key in fields}
        result["resource_warning_count"] = sum(len(row.get("resource_warnings", [])) for row in rows)
        result["unclosed_resource_warnings"] = sum("unclosed" in warning.lower() for row in rows
                                                  for warning in row.get("resource_warnings", []))
        result["partial_experiments"] = sum(bool(row.get("subprocess_timeout") or row.get("worker_join_timeouts")) for row in rows)
        result["cancelled_locally"] = sum(item.get("cancelled", False) for row in rows for item in row["rows"])
        defects = any(result[key] for key in ("request_mapping_errors", "transport_exceptions", "loop_affinity_errors",
                      "initialization_races", "cleanup_failures", "worker_join_timeouts", "pending_tasks_at_close",
                      "clients_left_open", "hidden_retries", "unclosed_resource_warnings", "truncated_http_requests"))
        covered = {(row["mode"], row["count"]) for row in rows}
        complete = ({("owned", 2), ("owned", 5)} <= covered if name == "B" else
                    {("same", 2), ("same", 10), ("same", 25), ("cancel", 1), ("cancel_direct", 1),
                     ("shutdown", 1), ("delayed", 1)} <= covered if name == "C" else False)
        result["classification"] = ("NOT_QUALIFIED" if defects else "INSUFFICIENT_EVIDENCE" if not complete else
                                    "CONDITIONALLY_QUALIFIED" if result["resource_warning_count"] else "QUALIFIED")
        result["scope"] = "Bounded loopback transport only; not production activation, capacity, TLS, or load qualification"
        results[name] = result
    return results


def pytest_terminal_summary(terminalreporter):
    reports = terminalreporter.config.stash.get(OBSERVATIONS, [])
    if not reports:
        return
    terminalreporter.write_sep("-", "Loopback transport ownership (not live/load qualification)")
    fields = ("requests_attempted", "requests_completed", "http_request_count", "transport_exceptions",
              "loop_affinity_errors", "cleanup_failures", "initialization_races", "hidden_retries",
              "client_count", "pool_count", "pending_tasks_at_close", "clients_left_open")
    for report in reports:
        terminalreporter.write_line(f"{report['mode']}[{report['count']}]: " +
                                   ", ".join(f"{key}={report[key]}" for key in fields))
    architectures = summarize(reports)
    for name, result in architectures.items():
        terminalreporter.write_line(f"Architecture {name}: {json.dumps(result)}")
    destination = os.environ.get("AGENTGUARD_TEST_TRANSPORT_REPORT")
    if destination:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as output:
            json.dump({"scope": "offline localhost only; production unchanged", "experiments": reports,
                       "architectures": architectures}, output, indent=2)
        terminalreporter.write_line(f"Transport evidence: {target}")
