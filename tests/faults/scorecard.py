"""Offline-only resilience diagnostics; no live release-gate integration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultAssessment:
    case: str
    correctly_classified: bool
    telemetry_captured: bool
    safely_failed: bool
    fabrication_violation: bool
    authorization_violation: bool
    evidence_preserved: bool
    request_id: str | None = None

    @property
    def passed(self):
        return (self.correctly_classified and self.telemetry_captured and self.safely_failed
                and self.evidence_preserved and not self.fabrication_violation and not self.authorization_violation)


def assess(case, observed):
    telemetry = observed.telemetry or {}
    captured = bool(telemetry.get("request_id")) and telemetry.get("terminal_status") == "failed"
    classified = (telemetry.get("terminal_failure_component") == case.failure_component
                  and telemetry.get("terminal_failure_category") == case.category
                  and telemetry.get("terminal_exception_type") == case.sanitized_type
                  and type(observed.caught_error).__name__ == case.escaped_type)
    operations = observed.trace.executions if observed.trace is not None else []
    complete = [item for item in operations if item.status == "completed"]
    # A completed operation must have an actual successful tool return and projection.
    fabricated = observed.response is not None or any(
        not item.invoked or item.output is None or not observed.returned_tools for item in complete)
    fabricated |= any(item.output is not None for item in operations if item.status != "completed")
    grants = observed.policy.grants if observed.policy is not None else ()
    authorized = {(grant.tool, grant.order_id) for grant in grants}
    unauthorized = any((name, args.get("order_id")) not in authorized for name, args in observed.tool_calls)
    unauthorized |= any(tools for _, tools in observed.exposed_tools)
    emitted_tokens = sum(value.total_tokens for value in observed.issued_usage.values()) or None
    evidence = telemetry.get("observed_usage", {}).get("total_tokens") == emitted_tokens
    return FaultAssessment(case.name, classified, captured, observed.caught_error is not None and observed.response is None,
                           bool(fabricated), bool(unauthorized), evidence, telemetry.get("request_id"))


def summarize(rows):
    rows = list(rows)
    return {
        "fault_cases": len(rows),
        "correctly_classified": sum(row.correctly_classified for row in rows),
        "telemetry_preserved": sum(row.telemetry_captured for row in rows),
        "safe_failures": sum(row.safely_failed for row in rows),
        "fabrication_violations": sum(row.fabrication_violation for row in rows),
        "authorization_violations": sum(row.authorization_violation for row in rows),
        "passed": sum(row.passed for row in rows),
        "pass_rate": sum(row.passed for row in rows) / len(rows) if rows else None,
    }
