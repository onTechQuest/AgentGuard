"""No shared production pool, real network, live judge, or timing-based sleeps."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError, replace
import json
from threading import Barrier

import pytest

from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.safety_evaluator import SafetyScore
from src.agentguard.safety_incidents import SafetyIncidentRecorder

from .harness import ACTIVE, WAIT, Request, Schedule, assess, context_values


def clean(rows):
    counts = assess(rows)
    assert all(value == 0 for key, value in counts.items() if key != "requests_executed"), counts
    return [row.read() for row in rows]


def test_two_requests_exact_interleaving(campaign):
    a, b = Request("a", ("ORD-1001",), seed=1), Request("b", ("ORD-1002",), seed=2)
    schedule = Schedule(["a", "b"], dependencies={
        ("a", "router_end"): [("b", "tool")],
        ("a", "synthesis_end"): [("b", "synthesis_start")],
        ("b", "synthesis_end"): [("a", "synthesis_end")],
    })
    a.schedule = b.schedule = schedule
    rows = campaign.run([a, b])
    clean(rows)
    checkpoints = [("a", "router_start"), ("b", "router_end"), ("b", "tool"),
                   ("a", "router_end"), ("a", "tool"), ("a", "synthesis_end"), ("b", "synthesis_end")]
    assert [schedule.log.index(point) for point in checkpoints] == sorted(schedule.log.index(point) for point in checkpoints)
    assert a.owned["retry"] is not b.owned["retry"]
    assert a.owned["budget"]._signals is not b.owned["budget"]._signals


def test_ten_overlapping_varied_requests(campaign):
    requests = [Request(f"ten-{i}", tuple(f"ORD-{2000 + i * 10 + j}" for j in range(i % 3)),
                        capability="return_eligibility" if i % 2 else "order_status", seed=i + 1)
                for i in range(10)]
    schedule = Schedule([request.name for request in requests])
    for request in requests:
        request.schedule = schedule
    rows = campaign.run(requests)
    data = clean(rows)
    assert len({row.worker for row in rows}) == 10
    assert [row["name"] for row in data] == [request.name for request in requests]
    assert {len(row["tool_calls"]) for row in data} == {0, 1, 2}
    assert all(row["dispatches"] == {"primary_router": 1, "synthesis": 1} for row in data)


@pytest.mark.parametrize("fault", ["router", "synthesis", "rate_limit", "deadline", "cancel"])
def test_failure_does_not_affect_healthy_requests(campaign, fault):
    requests = [Request(f"failed-{fault}", fault=fault),
                Request(f"healthy-{fault}-1", ("ORD-1002",), seed=2),
                Request(f"healthy-{fault}-2", ("ORD-1003",), seed=3)]
    schedule = Schedule([request.name for request in requests])
    for request in requests:
        request.schedule = schedule
    rows = campaign.run(requests)
    data = clean(rows)
    assert data[0]["error_type"]
    assert all(row["error_type"] is None for row in data[1:])
    assert requests[1].budget.remaining_ms() == requests[2].budget.remaining_ms() == 20000
    assert all(request.owned["retry"].consumed == 0 for request in requests)


def test_recovery_is_a_separate_logical_call(campaign):
    requests = [Request("recovered", recovery=True), Request("direct", ("ORD-1002",), seed=2)]
    schedule = Schedule([request.name for request in requests])
    for request in requests:
        request.schedule = schedule
    first, second = clean(campaign.run(requests))
    assert first["dispatches"] == {"primary_router": 1, "recovery_planner": 1, "synthesis": 1}
    assert second["dispatches"] == {"primary_router": 1, "synthesis": 1}
    assert first["telemetry"]["planning_summary"]["recovery_count"] == 1
    assert second["telemetry"]["planning_summary"]["recovery_count"] == 0


def test_same_order_has_independent_projected_objects(campaign):
    requests = [Request("same-a"), Request("same-b", seed=2)]
    schedule = Schedule([request.name for request in requests])
    for request in requests:
        request.schedule = schedule
    clean(campaign.run(requests))
    first, second = requests
    assert first.trace.executions[0].output is not second.trace.executions[0].output
    before = dict(second.trace.executions[0].output)
    first.trace.executions[0].output["status"] = "changed after completed snapshot"
    assert second.trace.executions[0].output == before


@pytest.mark.parametrize("fault", [None, "router", "deadline"])
def test_reused_worker_has_no_stale_context(campaign, fault):
    first, second = Request(f"reuse-{fault}", fault=fault), Request(f"reuse-after-{fault}", seed=2)
    campaign.requests.extend([first, second])
    def invoke(request):
        assert all(value is None for value in context_values()) and ACTIVE.get() is None
        result = request.run()
        assert all(value is None for value in context_values()) and ACTIVE.get() is None
        return result
    with ThreadPoolExecutor(max_workers=1) as pool:
        rows = [pool.submit(invoke, request).result(WAIT) for request in (first, second)]
    clean(campaign.collect(rows))
    assert rows[0].worker == rows[1].worker


def test_nested_request_restores_outer_scope(campaign):
    nested = Request("nested", ("ORD-1002",), seed=2)
    nested_results = []  # Solely owned by the outer worker; parent reads after join.
    def invoke_nested(outer):
        before = context_values()
        nested_results.append(nested.run())
        assert all(a is b for a, b in zip(before, context_values()))
        assert ACTIVE.get() is outer
    outer = Request("outer", callback=invoke_nested)
    campaign.requests.append(nested)
    rows = campaign.run([outer])
    campaign.collect(nested_results)
    clean(rows + nested_results)


@pytest.mark.parametrize("propagate", [False, True])
def test_executor_context_contract(campaign, propagate):
    child = Request(f"context-child-{propagate}", ("ORD-1002",), seed=2)
    child_results = []
    def callback(outer):
        before = context_values()
        def work():
            inherited = context_values()
            if propagate:
                assert all(a is b for a, b in zip(before, inherited))
                assert ACTIVE.get() is outer
            else:
                assert all(value is None for value in inherited) and ACTIVE.get() is None
            result = child.run()  # New root replaces inherited mutable references.
            assert all(a is b for a, b in zip(inherited, context_values()))
            return result
        with ThreadPoolExecutor(max_workers=1) as executor:
            context = copy_context()
            future = executor.submit(context.run, work) if propagate else executor.submit(work)
            child_results.append(future.result(WAIT))
        assert all(a is b for a, b in zip(before, context_values()))
    outer = Request(f"context-parent-{propagate}", callback=callback)
    campaign.requests.append(child)
    rows = campaign.run([outer])
    campaign.collect(child_results)
    clean(rows + child_results)


def test_invalid_root_reuse_is_rejected_by_harness(campaign):
    first, second = Request("unique"), Request("invalid")
    second.budget = first.budget
    with pytest.raises(AssertionError, match="distinct budgets"):
        campaign.run([first, second])
    assert not first.claimed and not second.claimed
    clean(campaign.run([first]))
    with pytest.raises(AssertionError, match="must not be reused"):
        first.run()


def test_child_signals_are_scoped_to_one_root(campaign):
    first, second = Request("child-cancel", fault="cancel"), Request("child-healthy", seed=2)
    # The dispatch's cancellation injection exercises the real acceptance gate.
    schedule = Schedule([first.name, second.name])
    first.schedule = second.schedule = schedule
    clean(campaign.run([first, second]))
    assert first.budget.cancellation_requested and not second.budget.cancellation_requested


@pytest.mark.parametrize("shared", [True, False])
def test_incident_publication_isolated(campaign, tmp_path, shared):
    requests = [Request(f"artifact-{shared}-{i}", (f"ORD-{3100 + i}",), seed=i + 1) for i in range(3)]
    schedule = Schedule([request.name for request in requests])
    for request in requests:
        request.schedule = schedule
    data = clean(campaign.run(requests))
    recorders = [SafetyIncidentRecorder(tmp_path) for _ in range(1 if shared else len(data))]
    barrier = Barrier(len(data))
    def retain(index):
        row = data[index]
        observed = row["telemetry"]
        record = EvaluationRecord(row["name"], row["scenario_input"], row["final_output"], row["tool_calls"],
            0, 2, None, None, row["result_tokens"],
            tool_outputs=[{"name": item["operation"]["tool"], "call_id": item["call_id"], "output": item["output"]}
                          for item in row["execution"]["operations"]],
            execution=row["execution"], production_telemetry=observed)
        score = SafetyScore(row["name"], False, False, None, None, True, ["Scripted safety failure"])
        barrier.wait(WAIT)
        return recorders[0 if shared else index].retain({"id": row["name"], "input": row["scenario_input"]}, record, score)
    with ThreadPoolExecutor(max_workers=len(data)) as pool:
        futures = [pool.submit(retain, index) for index in range(len(data))]
        paths = [future.result(WAIT) for future in futures]
    assert None not in paths and len(set(paths)) == len(data)
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    campaign.collect_artifacts(paths)
    assert len({item["run_id"] for item in artifacts}) == (1 if shared else len(data))
    assert len({item["request_id"] for item in artifacts}) == len(data)
    for artifact, row in zip(artifacts, data):
        assert artifact["scenario_id"] == row["name"]
        assert artifact["final_response"] == row["final_output"]
        assert artifact["projected_authoritative_facts"][0]["output"] == row["expected_facts"][0]
        assert all(other["name"] not in json.dumps(artifact) for other in data if other is not row)


def test_only_parent_publishes_immutable_completed_snapshots(campaign, tmp_path):
    requests = [Request("publish-a"), Request("publish-b", seed=2)]
    rows = campaign.run(requests)
    clean(rows)
    with pytest.raises(FrozenInstanceError):
        rows[0].payload = "mutated"
    copy = rows[0].read()
    copy["telemetry"]["request_id"] = "mutated"
    assert rows[0].read()["telemetry"]["request_id"] != "mutated"
    target = tmp_path / "aggregate.json"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(AssertionError, match="parent coordinator"):
            pool.submit(campaign.publish, target, rows).result(WAIT)
        with pytest.raises(AssertionError, match="parent coordinator"):
            pool.submit(campaign.collect, rows).result(WAIT)
    assert not target.exists()
    campaign.publish(target, rows)
    assert json.loads(target.read_text()) == [row.read() for row in rows]
    with pytest.raises(FileExistsError):
        campaign.publish(target, rows)


def test_broken_schedule_fails_without_sleep_or_unbounded_wait(campaign):
    schedule = Schedule(["broken", "absent"])
    schedule.barrier.abort()
    request = Request("broken", schedule=schedule, fault="router")
    # Runtime translates barrier failure to a terminal routing error and cleans scope.
    row = campaign.run([request])[0]
    assert row.read()["error_type"] == "CapabilityRoutingError"
    assert all(value is None for value in context_values())


def test_failure_after_other_request_reaches_synthesis(campaign):
    failed, healthy = Request("ordered-failure", fault="synthesis"), Request("ordered-healthy", seed=2)
    schedule = Schedule([failed.name, healthy.name], dependencies={
        (failed.name, "router_end"): [(healthy.name, "synthesis_start")],
        (healthy.name, "synthesis_end"): [(failed.name, "synthesis_start")],
    })
    failed.schedule = healthy.schedule = schedule
    clean(campaign.run([failed, healthy]))
    assert schedule.log.index((healthy.name, "synthesis_start")) < schedule.log.index((failed.name, "router_end"))


@pytest.mark.parametrize("kind", ["request_id_collisions", "telemetry_contamination", "tool_result_contamination",
                                  "duplicate_operations", "cross_request_budget_contamination", "incorrect_final_answers",
                                  "failure_isolation_violations", "hidden_retry_amplification", "authorization_bypass"])
def test_scorecard_detects_corrupted_evidence(campaign, kind):
    rows = campaign.run([Request(f"oracle-{kind}-a"), Request(f"oracle-{kind}-b", seed=2)])
    clean(rows)
    data = rows[1].read()
    if kind == "request_id_collisions":
        data["telemetry"]["request_id"] = rows[0].read()["telemetry"]["request_id"]
    elif kind == "telemetry_contamination":
        data["telemetry"]["observed_usage"]["total_tokens"] += 1
    elif kind == "tool_result_contamination":
        data["execution"]["operations"][0]["output"]["tracking_number"] = "another request"
    elif kind == "duplicate_operations":
        data["tool_calls"].append(data["tool_calls"][0])
    elif kind == "cross_request_budget_contamination":
        data["cancelled"] = True
    elif kind == "incorrect_final_answers":
        data["final_output"] = "Fabricated answer"
    elif kind == "failure_isolation_violations":
        data["telemetry"]["terminal_status"] = "failed"
    elif kind == "hidden_retry_amplification":
        data["dispatches"]["synthesis"] = 2
    else:
        data["tool_calls"][0]["name"] = "issue_refund"
    corrupted = replace(rows[1], payload=json.dumps(data))
    assert assess([rows[0], corrupted])[kind] > 0
