"""Caller-owned campaign aggregation of immutable worker completion snapshots."""
from collections import Counter
import json
import os
from pathlib import Path
from threading import get_ident
import time
from uuid import uuid4

from src.hosting.runtime import Admission, CompletedRequest


class CampaignCollector:
    def __init__(self, *, clock=time.monotonic, measure_rates=True):
        self._thread = get_ident()
        self._clock, self._started = clock, clock()
        self._measure_rates = measure_rates
        self._admissions, self._completed = {}, {}
        self._collisions = 0

    def _check(self):
        if get_ident() != self._thread:
            raise RuntimeError("CampaignCollector may only be mutated/read by its owning thread")

    def observe(self, admission: Admission):
        self._check()
        if admission.request_id in self._admissions:
            self._collisions += 1
            raise ValueError("Duplicate admission/request ID")
        self._admissions[admission.request_id] = admission

    def record(self, result: CompletedRequest):
        self._check()
        admission = self._admissions.get(result.request_id)
        if admission is None or not admission.accepted or result.request_id in self._completed:
            raise ValueError("Completion must match a unique admitted request")
        self._completed[result.request_id] = result

    def snapshot(self, runtime_status: dict):
        self._check()
        rows, outcomes, isolation = [], Counter(), Counter()
        isolation["request_id_collision"] = self._collisions
        assessed = Counter()
        for result in self._completed.values():
            data = json.loads(result.telemetry_json) or {}
            execution = json.loads(result.execution_json)
            dispatches = json.loads(result.dispatches_json)
            spans = data.get("component_spans", [])
            assessed["telemetry"] += bool(data)
            isolation["telemetry_contamination"] += bool(data) and data.get("request_id") != result.request_id
            isolation["telemetry_contamination"] += sum(d["request_id"] != result.request_id for d in dispatches)
            if execution is not None:
                assessed["tool_results"] += 1
                operations = execution.get("operations", [])
                keys = [(op["operation"]["tool"], json.dumps(op["operation"]["arguments"], sort_keys=True))
                        for op in operations if op.get("invoked")]
                isolation["duplicate_operation"] += len(keys) - len(set(keys))
                for op in operations:
                    output = op.get("output") or {}
                    target = dict(op["operation"]["arguments"]).get("order_id")
                    actual = output.get("order_id", output.get("order", {}).get("order_id"))
                    isolation["mixed_tool_result"] += actual is not None and target != actual
            outcome = result.outcome
            outcomes[outcome] += 1
            late = bool(data.get("result_abandoned"))
            outcomes["late_result"] += late  # Independent flag, not an additional completed request.
            tokens = data.get("observed_usage", {}).get("total_tokens")
            rows.append({"request_id": result.request_id, "worker_id": result.worker_id, "outcome": outcome,
                         "queue_wait_ms": result.queue_wait_ms, "service_ms": result.service_ms,
                         "end_to_end_ms": result.end_to_end_ms,
                         "router_ms": sum(s.get("duration_ms") or 0 for s in spans if s["component"] == "primary_router"),
                         "recovery_ms": sum(s.get("duration_ms") or 0 for s in spans if s["component"] == "recovery_planner"),
                         "tools_ms": sum(s.get("duration_ms") or 0 for s in spans if s["component"] == "tool"),
                         "synthesis_ms": sum(s.get("duration_ms") or 0 for s in spans if s["component"] == "synthesis"),
                         "tokens": tokens, "usage_completeness": data.get("usage_completeness", "UNAVAILABLE"),
                         "logical_model_calls": sum(s.get("logical_model_calls") or 0 for s in spans),
                         "provider_dispatches": len(dispatches),
                         "recovery_count": data.get("planning_summary", {}).get("recovery_count", 0),
                         "extra_retry_attempts": data.get("retry_attempts_total", 0), "late_result": late})
            if data.get("decision_evidence") is not None:
                rows[-1]["decision_evidence"] = data["decision_evidence"]
        admissions = list(self._admissions.values())
        rejected = sum(not a.accepted for a in admissions)
        outcomes["admission_rejection"] = rejected
        duration = max(0, self._clock() - self._started)
        known_tokens = [r["tokens"] for r in rows if r["tokens"] is not None]
        complete_usage = all(r["usage_completeness"] == "COMPLETE" for r in rows)
        latency = {}
        for field in ("end_to_end_ms", "queue_wait_ms", "service_ms", "router_ms", "recovery_ms", "tools_ms", "synthesis_ms"):
            samples = [r[field] for r in rows]
            latency[field] = {"count": len(samples), "average": sum(samples) / len(samples) if samples else None,
                              "maximum": max(samples) if samples else None}
        return {"schema_version": 1, "scope": "observations; no production SLO thresholds",
                "runtime": json.loads(json.dumps(runtime_status)),
                "admission": {"offered": len(admissions), "admitted": len(admissions) - rejected, "rejected": rejected,
                              "max_observed_queue_depth": max((a.queue_depth for a in admissions), default=0)},
                "throughput": {"starts": runtime_status.get("starts", sum(r.worker_id >= 0 for r in self._completed.values())),
                               "completions": len(rows), "successful_completions": outcomes["success"],
                               "observation_seconds": duration,
                               "rates_enabled": self._measure_rates,
                               "completions_per_second": len(rows) / duration if duration and self._measure_rates else None},
                "reliability": {key: outcomes[key] for key in ("success", "admission_rejection", "deadline_rejection",
                                 "provider_failure", "rate_limit", "cancellation", "late_result", "unknown_failure")},
                "consumption": {"known_tokens": sum(known_tokens), "usage_complete": complete_usage and bool(rows),
                                "average_known_tokens_per_request": sum(known_tokens) / len(known_tokens) if known_tokens else None,
                                "tokens_per_second": sum(known_tokens) / duration if duration and rows and complete_usage and self._measure_rates else None,
                                "logical_model_calls": sum(r["logical_model_calls"] for r in rows),
                                "provider_dispatches": sum(r["provider_dispatches"] for r in rows),
                                "dispatch_evidence": "HTTP-client request hook; remote receipt/billing unknown",
                                "recovery_count": sum(r["recovery_count"] for r in rows),
                                "extra_retry_attempts": sum(r["extra_retry_attempts"] for r in rows)},
                "isolation": {key: isolation[key] for key in ("request_id_collision", "mixed_tool_result",
                              "duplicate_operation", "telemetry_contamination")},
                "isolation_assessed": dict(assessed), "latency": latency, "requests": rows}

    def publish(self, directory, runtime_status, *, filename=None):
        """Atomic publication without overwriting a previous campaign artifact."""
        self._check()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        filename = filename or f"concurrency_{uuid4().hex}.json"
        if Path(filename).name != filename:
            raise ValueError("Report filename must be a basename")
        target, temporary = directory / filename, directory / f".concurrency_{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(self.snapshot(runtime_status), stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)  # Atomic same-filesystem link; fails if target exists.
        finally:
            temporary.unlink(missing_ok=True)
        return target
