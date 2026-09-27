"""Fault contracts validated through the production request entry point."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier
from uuid import UUID

import pytest

from src.agent import telemetry
from src.agentguard.evaluation_record import execute_scenario

from .harness import Fault, Harness, Injection, PRIVATE_MARKERS, PROMPT, Stage, _active
from .matrix import CASES
from .scorecard import FaultAssessment, assess, summarize


def check_contract(case, observed):
    data = observed.telemetry
    assert UUID(data["request_id"]).hex == data["request_id"]
    assert data["terminal_status"] == "failed"
    assert data["total_latency_ms"] >= 0
    spans = {span["component"]: span for span in data["component_spans"]}
    assert spans[case.failure_component]["status"] == "failed"
    assert spans[case.failure_component]["failure_category"] == case.category
    assert spans[case.failure_component]["sanitized_exception_type"] == case.sanitized_type
    assert all(span["status"] in {"completed", "failed"} for span in spans.values())
    assert all(span["duration_ms"] >= 0 and span["start_offset_ms"] >= 0 for span in spans.values())
    assert all(span["http_retry_count"] is None and span["retry_source"] is None for span in spans.values())
    order = [Stage.ROUTER, Stage.RECOVERY, Stage.POLICY, Stage.PLAN, Stage.CONTRACT, Stage.TOOL, Stage.SYNTHESIS]
    failing_index = order.index(case.injection.stage)
    for stage in order[failing_index + 1:]:
        assert observed.calls[stage] == 0, f"Unexpected work after {case.name}: {stage}"
    for stage in order[:failing_index]:
        if stage == Stage.RECOVERY and not observed.recovery:
            assert stage not in spans
        elif stage == Stage.CONTRACT and case.injection.stage == Stage.TOOL:
            assert spans[stage]["status"] == "failed"  # Parent of failing operation.
        else:
            assert spans[stage]["status"] == "completed", f"Lost prior evidence: {stage}"
    assert all(count <= 1 for count in observed.calls.values()), "Application retried a boundary"
    expected_usage = {stage: value for stage, value in observed.issued_usage.items()}
    for stage in (Stage.ROUTER, Stage.RECOVERY, Stage.SYNTHESIS):
        if stage not in spans:
            continue
        span = spans[stage]
        assert span["logical_model_calls"] == 1
        if stage in expected_usage:
            expected = expected_usage[stage]
            assert span["sdk_visible_requests"] == expected.requests
            assert span["input_tokens"] == expected.input_tokens
            assert span["output_tokens"] == expected.output_tokens
            assert span["total_tokens"] == expected.total_tokens
            assert span["usage_available"] is True
        else:
            assert span["total_tokens"] is span["input_tokens"] is span["output_tokens"] is None
            assert span["usage_available"] is False
    missing = any(stage in spans and stage not in expected_usage for stage in
                  (Stage.ROUTER, Stage.RECOVERY, Stage.SYNTHESIS))
    assert data["usage_completeness"] == ("UNAVAILABLE" if not expected_usage else "PARTIAL" if missing else "COMPLETE")
    if observed.recovery:
        assert data["planning_summary"]["recovery_count"] == 1
        assert spans[Stage.RECOVERY]["logical_model_calls"] == 1
        assert spans[Stage.RECOVERY]["http_retry_count"] is None
    if observed.trace is not None:
        summary = data["required_operation_summary"]
        operations = observed.trace.executions
        assert summary["required"] == [item.call_id for item in operations]
        assert summary["completed"] == [item.call_id for item in operations if item.status == "completed"]
        assert summary["incomplete"] == [item.call_id for item in operations if item.status != "completed"]
        if case.injection.stage == Stage.SYNTHESIS:
            assert len(summary["completed"]) == 1 and not summary["incomplete"]
            assert len(observed.returned_tools) == 1
            assert operations[0].output["status"] == "processing"
            assert PRIVATE_MARKERS[1] not in json.dumps(operations[0].output)
        else:
            assert not summary["completed"] and len(summary["incomplete"]) == 1
            assert operations[0].output is None
            assert not observed.synthesis_inputs
    else:
        assert not data["required_operation_summary"]
        assert not observed.tool_calls and not observed.synthesis_inputs
    if case.injection.fault == Fault.PROHIBITED:
        assert observed.trace.prohibited_attempts
        assert all(name != "issue_refund" for name, _ in observed.tool_calls)
    if observed.injected_error is not None:
        # Existing router/recovery/tool wrappers are allowed; direct errors preserve identity.
        if case.escaped_type not in {"CapabilityRoutingError", "PlanningCompletenessError", "ExecutionFailure"}:
            assert observed.caught_error is observed.injected_error
    serialized = json.dumps(data)
    for private in (*PRIVATE_MARKERS, PROMPT, "fake-raw-prompt-DO-NOT-LOG", "ORD-1001", '"status":"delivered"'):
        assert private not in serialized
    assert _active.get() is None
    assert telemetry._request.get() is None and telemetry._span.get() is None and telemetry._failure.get() is None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_fault_matrix(faults, fault_results, case):
    observed = Harness(case.injection).run()
    row = assess(case, observed)
    try:
        assert row.passed, row
        check_contract(case, observed)
    except AssertionError:
        fault_results.append(replace(row, evidence_preserved=False))
        raise
    fault_results.append(row)
    assert len({item.case for item in fault_results}) == len(fault_results)
    assert len({item.request_id for item in fault_results}) == len(fault_results)


@pytest.mark.parametrize("outcome,expected", [
    ("not_found", "Order not found."),
    ("clarification", "Which order?"),
    ("refusal", "I cannot disclose private data."),
    ("unsupported_action", "I cannot issue refunds."),
])
def test_business_outcomes_are_not_reliability_failures(faults, outcome, expected):
    observed = Harness(business_outcome=outcome).run()
    assert observed.caught_error is None
    assert observed.response.final_output == expected
    assert observed.telemetry["terminal_status"] == "completed"
    assert observed.telemetry["terminal_failure_category"] is None
    assert all(span["failure_category"] is None for span in observed.telemetry["component_spans"])
    if outcome == "not_found":
        assert observed.trace.executions[0].status == "completed"
        assert observed.trace.executions[0].output["found"] is False
    else:
        assert not observed.tool_calls and not observed.trace.executions
    assert all(not tools for _, tools in observed.exposed_tools)


def test_failure_and_success_concurrently_keep_separate_evidence(faults):
    barrier = Barrier(2)
    failed = Harness(Injection(Stage.SYNTHESIS, Fault.RATE_LIMIT), barrier=barrier)
    succeeded = Harness(barrier=barrier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda harness: harness.run(), [failed, succeeded]))
    first, second = outcomes
    assert first.telemetry["request_id"] != second.telemetry["request_id"]
    assert first.caught_error is first.injected_error and first.response is None
    assert second.caught_error is None and second.response.final_output == "Authoritative status: processing."
    assert first.telemetry["terminal_failure_category"] == "RATE_LIMIT"
    assert second.telemetry["terminal_failure_category"] is None
    assert first.telemetry["observed_usage"]["total_tokens"] == 14
    assert second.telemetry["observed_usage"]["total_tokens"] == 44
    first_ids = set(first.telemetry["required_operation_summary"]["required"])
    second_ids = set(second.telemetry["required_operation_summary"]["required"])
    assert first_ids and second_ids and first_ids.isdisjoint(second_ids)
    assert all(span["status"] == "completed" for span in second.telemetry["component_spans"])
    assert _active.get() is None and telemetry._request.get() is None


@pytest.mark.parametrize("injection", [Injection(Stage.RECOVERY, Fault.TIMEOUT), Injection(Stage.TOOL, Fault.TIMEOUT),
                                        Injection(Stage.CONTRACT, Fault.INCOMPLETE), Injection(Stage.SYNTHESIS, Fault.PROHIBITED)])
def test_existing_structured_failure_records_have_no_fallback_answer(faults, injection):
    observed = Harness(injection).run(lambda: execute_scenario({"id": "offline-record", "input": PROMPT}))
    assert observed.caught_error is None  # Existing structured failure semantics.
    assert observed.response.execution_error
    assert observed.response.final_output == ""
    assert observed.telemetry["terminal_status"] == "failed"
    if injection.stage != Stage.SYNTHESIS:
        assert observed.response.tool_outputs == []
        assert not observed.synthesis_inputs
    else:
        assert observed.response.tool_outputs[0]["output"]["status"] == "processing"


def test_sequential_requests_get_distinct_ids_even_after_failure(faults):
    first = Harness(Injection(Stage.ROUTER, Fault.TIMEOUT)).run()
    second = Harness().run()
    assert first.telemetry["request_id"] != second.telemetry["request_id"]
    assert second.telemetry["terminal_status"] == "completed"


def test_summary_counts_observed_violations_and_empty_is_unavailable():
    assert summarize([])["pass_rate"] is None
    good = FaultAssessment("good", True, True, True, False, False, True)
    bad = FaultAssessment("bad", False, False, False, True, True, False)
    summary = summarize([good, bad])
    assert summary == {"fault_cases": 2, "correctly_classified": 1, "telemetry_preserved": 1,
                       "safe_failures": 1, "fabrication_violations": 1, "authorization_violations": 1,
                       "passed": 1, "pass_rate": 0.5}


def test_assessor_detects_false_completion_and_unauthorized_execution(faults):
    case = next(case for case in CASES if case.name == "tool_exception")
    observed = Harness(case.injection).run()
    assert assess(case, observed).passed
    observed.trace.executions[0].status = "completed"
    observed.trace.executions[0].output = {"status": "delivered"}
    observed.tool_calls.append(("issue_refund", {"order_id": "ORD-1001"}))
    row = assess(case, observed)
    assert row.fabrication_violation and row.authorization_violation and not row.passed
