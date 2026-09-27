"""Monotonic admission primitives; explicit finite budgets are enforced in 13C.2.

None means unbounded, never zero. Child deadlines are absolute bounds in the
same clock domain. Cancellation evidence concerns local execution only; it does
not establish the outcome of a remote operation.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import math
import time
from uuid import uuid4


class ReliabilityState(str, Enum):
    TIMEOUT = "TIMEOUT"
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    CANCELLED = "CANCELLED"
    RESULT_ABANDONED = "RESULT_ABANDONED"
    REMOTE_OUTCOME_UNKNOWN = "REMOTE_OUTCOME_UNKNOWN"


class AdmissionDenial(str, Enum):
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    CANCELLED = "CANCELLED"
    INSUFFICIENT_ALLOWANCE = "INSUFFICIENT_ALLOWANCE"


class CancellationEvidence(str, Enum):
    LOCAL_TASK_CANCELLED = "LOCAL_TASK_CANCELLED"
    COOPERATIVE_ACKNOWLEDGEMENT = "COOPERATIVE_ACKNOWLEDGEMENT"


@dataclass(frozen=True)
class AdmissionEvidence:
    """admitted=None denotes an unevaluated decision, including current recovery."""

    remaining_budget_ms: float | None
    required_downstream_reserve_ms: float | None = None
    minimum_work_ms: float | None = None
    allowance_ms: float | None = None
    admitted: bool | None = None
    denial_reason: AdmissionDenial | None = None


class BudgetAdmissionError(RuntimeError):
    def __init__(self, evidence: AdmissionEvidence):
        self.evidence = evidence
        super().__init__(evidence.denial_reason.value)


class RequestBudgetRejected(BudgetAdmissionError):
    """Terminal request rejection, distinct from the outcome of completed work."""

    def __init__(self, component: str, evidence: AdmissionEvidence, *, completed=False):
        super().__init__(evidence)
        self.component = component
        self.completed = completed
        self.phase = "result_acceptance" if completed else "admission"


class RequestDeadlineExceeded(RequestBudgetRejected):
    """Deadline reached, or insufficient remaining budget for required reserves."""


@dataclass(frozen=True)
class RecoveryBudgetPolicy:
    """Optional admission requirements, not component timeouts or retry policy."""

    recovery_allowance_ms: float | None = None
    required_execution_reserve_ms: float | None = None
    synthesis_reserve_ms: float | None = None
    completion_reserve_ms: float | None = None

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            _milliseconds(getattr(self, name), name, optional=True)

    def requirements(self) -> dict:
        return {
            "minimum_ms": self.recovery_allowance_ms or 0,
            "reserve_ms": sum(value or 0 for value in (
                self.required_execution_reserve_ms, self.synthesis_reserve_ms, self.completion_reserve_ms)),
        }


@dataclass
class _Signals:
    cancellation_requested: bool = False
    cancellation_evidence: CancellationEvidence | None = None
    result_abandoned: bool = False
    remote_outcome_unknown: bool = False


def _milliseconds(value, name, *, optional=False):
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number of milliseconds")


@dataclass(frozen=True)
class RequestBudget:
    original_budget_ms: float | None = None
    request_id: str = field(default_factory=lambda: uuid4().hex)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    started_at_monotonic: float = field(init=False)
    deadline_monotonic: float | None = field(init=False)
    _signals: _Signals = field(default_factory=_Signals, init=False, repr=False, compare=False)

    def __post_init__(self):
        _milliseconds(self.original_budget_ms, "original_budget_ms", optional=True)
        started = self.clock()
        object.__setattr__(self, "started_at_monotonic", started)
        object.__setattr__(self, "deadline_monotonic", None if self.original_budget_ms is None
                           else started + self.original_budget_ms / 1000)

    def remaining_ms(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, (self.deadline_monotonic - self.clock()) * 1000)

    def exhausted(self) -> bool:
        remaining = self.remaining_ms()
        return remaining is not None and remaining <= 0

    def admission(self, *, minimum_ms=0, reserve_ms=0, cap_ms=None) -> AdmissionEvidence:
        """Compute a decision without dispatching, waiting, cancelling or retrying.

        A positive allowance is required even when minimum_ms is zero. Reserves
        are held for downstream work; cap_ms limits this child's work only.
        """
        _milliseconds(minimum_ms, "minimum_ms")
        _milliseconds(reserve_ms, "reserve_ms")
        _milliseconds(cap_ms, "cap_ms", optional=True)
        remaining = self.remaining_ms()
        allowance = None if remaining is None else max(0.0, remaining - reserve_ms)
        if cap_ms is not None:
            allowance = cap_ms if allowance is None else min(allowance, cap_ms)
        denial = None
        if self.cancellation_observed:
            denial = AdmissionDenial.CANCELLED
        elif self.cancellation_requested:
            denial = AdmissionDenial.CANCELLATION_REQUESTED
        elif remaining is not None and remaining <= 0:
            denial = AdmissionDenial.DEADLINE_EXHAUSTED
        elif allowance is not None and (allowance <= 0 or allowance < minimum_ms):
            denial = AdmissionDenial.INSUFFICIENT_ALLOWANCE
        return AdmissionEvidence(remaining, reserve_ms, minimum_ms, allowance, denial is None, denial)

    def can_start(self, **requirements) -> bool:
        return self.admission(**requirements).admitted

    def require_remaining(self, **requirements) -> AdmissionEvidence:
        evidence = self.admission(**requirements)
        if not evidence.admitted:
            raise BudgetAdmissionError(evidence)
        return evidence

    def child_budget(self, *, minimum_ms=0, reserve_ms=0, cap_ms=None) -> "RequestBudget":
        """Create a bounded child; time spent constructing it cannot extend its parent."""
        evidence = self.require_remaining(minimum_ms=minimum_ms, reserve_ms=reserve_ms, cap_ms=cap_ms)
        child = RequestBudget(evidence.allowance_ms, self.request_id, self.clock)
        if self.deadline_monotonic is not None:
            deadline = min(child.deadline_monotonic, self.deadline_monotonic - reserve_ms / 1000)
            object.__setattr__(child, "deadline_monotonic", deadline)
            object.__setattr__(child, "original_budget_ms", max(0.0, (deadline - child.started_at_monotonic) * 1000))
        object.__setattr__(child, "_signals", self._signals)
        return child

    def limit(self, cap_ms) -> "RequestBudget":
        """Narrow a request from its original start, including exhausted requests.

        This installs a bound, not an admission decision. Admission still occurs
        inside the observed execution boundary, so rejected work retains telemetry.
        """
        _milliseconds(cap_ms, "cap_ms")
        cap = min(self.original_budget_ms, cap_ms) if self.original_budget_ms is not None else cap_ms
        bounded = RequestBudget(cap, self.request_id, self.clock)
        object.__setattr__(bounded, "started_at_monotonic", self.started_at_monotonic)
        object.__setattr__(bounded, "deadline_monotonic", self.started_at_monotonic + cap / 1000)
        object.__setattr__(bounded, "_signals", self._signals)
        return bounded

    @property
    def cancellation_requested(self) -> bool:
        return self._signals.cancellation_requested

    @property
    def cancellation_observed(self) -> bool:
        return self._signals.cancellation_evidence is not None

    @property
    def cancellation_evidence(self) -> CancellationEvidence | None:
        return self._signals.cancellation_evidence

    @property
    def result_abandoned(self) -> bool:
        return self._signals.result_abandoned

    @property
    def remote_outcome_unknown(self) -> bool:
        return self._signals.remote_outcome_unknown

    def request_cancellation(self):
        self._signals.cancellation_requested = True

    def observe_cancellation(self, evidence: CancellationEvidence):
        # A timed-out wait or abandoned result is deliberately not accepted here.
        if not isinstance(evidence, CancellationEvidence):
            raise ValueError("Explicit local cancellation evidence is required")
        self._signals.cancellation_evidence = evidence

    def abandon_result(self):
        self._signals.result_abandoned = True

    def mark_remote_outcome_unknown(self):
        self._signals.remote_outcome_unknown = True
