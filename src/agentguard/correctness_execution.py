"""Explicit evaluation-only ceilings and descriptive production-SLO evidence."""
from dataclasses import dataclass
import json
from pathlib import Path

from src.agent.runtime_reliability import RuntimeReliabilityPolicy, default_runtime_policy
from src.agent.request_budget import _milliseconds
from src.agentguard.synthesis_qualification import normalize

POLICY_PATH = Path(__file__).resolve().parents[2] / "config/correctness-execution.json"


@dataclass(frozen=True)
class CorrectnessExecutionPolicy(RuntimeReliabilityPolicy):
    recovery_synthesis_reserve_ms: float = 6000

    def __post_init__(self):
        super().__post_init__()
        _milliseconds(self.recovery_synthesis_reserve_ms, "recovery_synthesis_reserve_ms")
        retry = self.model_retry_policy
        if self.request_deadline_ms is None or retry.enabled or retry.max_attempts != 1 or retry.shared_extra_attempts_per_request:
            raise ValueError("Correctness evaluation requires finite budgets without retries")

    def requirements(self, component):
        if component == "recovery_planner":
            # A larger maximum synthesis duration is not a larger reservation.
            return dict(minimum_ms=self.recovery_reserve_ms, reserve_ms=self.recovery_synthesis_reserve_ms)
        return super().requirements(component)

    def profile_identity(self):
        return {**{key: getattr(self, key) for key in ("name", "request_deadline_ms", "router_allowance_ms",
            "recovery_reserve_ms", "synthesis_allowance_ms", "recovery_synthesis_reserve_ms")}, "retries_enabled": False}


def load_correctness_policy():
    return CorrectnessExecutionPolicy(**json.loads(POLICY_PATH.read_text(encoding="utf-8")))


def production_slo_observation(scenario_id, raw):
    """Compare observed durations to production bounds, never change decisions."""
    projected = normalize(dict(scenario_id=scenario_id, production_telemetry=raw))
    policy = default_runtime_policy()
    synthesis = projected["component_latency_ms"]["synthesis"]
    total = projected["total_request_ms"]
    return dict(scenario_id=scenario_id, request_id=projected["request_id"], authority="REPORT_ONLY",
        production_policy=policy.name, production_synthesis_allowance_ms=policy.synthesis_allowance_ms,
        observed_synthesis_latency_ms=synthesis,
        production_synthesis_allowance_exceeded=None if synthesis is None else synthesis >= policy.synthesis_allowance_ms,
        production_request_deadline_ms=policy.request_deadline_ms, observed_request_latency_ms=total,
        production_request_deadline_exceeded=None if total is None else total >= policy.request_deadline_ms)
