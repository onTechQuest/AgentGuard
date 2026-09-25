# Milestone 13C.1: request budget and attempt foundation

The runtime still routes, optionally recovers planning, resolves policy, executes
required tools once, projects their results, and synthesizes with `tools=[]`.
There is no new retry loop, admission gate, timeout setting, asynchronous
orchestrator, thread wrapper, or provider configuration change.

## RequestBudget API

`src/agent/request_budget.py` has no SDK dependency. `RequestBudget()` creates a
request ID and an unlimited budget. `RequestBudget(original_budget_ms=...,
clock=...)` accepts a finite non-negative allowance and an injectable monotonic
clock. No numeric production default is configured. Its immutable timing fields
are `started_at_monotonic`, `deadline_monotonic`, and `original_budget_ms`.
UTC is diagnostic metadata only; it never participates in admission calculations.

- `remaining_ms()` returns non-negative milliseconds, or `None` for unbounded.
- `exhausted()` checks the authoritative absolute deadline.
- `admission(minimum_ms=0, reserve_ms=0, cap_ms=None)` returns `AdmissionEvidence`.
  Remaining time minus the downstream reserve bounds the work allowance; a cap
  may reduce it further. A zero allowance is denied even with a zero minimum.
  An exact positive minimum is admitted. Requested or observed cancellation
  denies admission independently of elapsed time.
- `can_start(...)` returns the admission boolean. `require_remaining(...)`
  returns evidence or raises `BudgetAdmissionError` with a structured reason.
- `child_budget(...)` creates a child using the same clock and request ID. Its
  deadline cannot exceed the parent's absolute deadline minus the reserve,
  including time consumed during child construction. Descendants share request
  cancellation/abandonment signals. Independent requests own independent signals.

Admission is a point-in-time calculation, not a lock, reservation ledger, or
dispatch guarantee. A future dispatcher must check immediately before work starts.
The downstream reserve bounds time; it does not allocate concurrent resource quotas.

`run_support_agent_detailed(..., request_budget=budget)` accepts an optional
budget **for observation only in this phase**. Default execution creates an
unlimited budget. Even an explicitly exhausted or cancellation-requested budget
does not currently suppress work or reject its result. A caller-supplied budget
may start before this function; request latency still measures this invocation,
while budget remaining reflects the caller's original absolute deadline.
Use opaque request IDs and labels, never customer identifiers or secrets.

## Independent deadline and cancellation evidence

`ReliabilityState` and the telemetry failure taxonomy distinguish:

| State | Meaning |
| --- | --- |
| `TIMEOUT` | A component reported a timeout; this alone proves neither request deadline exhaustion nor cancellation. |
| `DEADLINE_EXHAUSTED` | The monotonic request deadline has been reached; future enforced admission must reject new work. |
| `CANCELLED` | Local task cancellation was actually observed. It says nothing about completion of a remote operation. |
| `RESULT_ABANDONED` | A result was explicitly marked abandoned; work may still be running. |
| `REMOTE_OUTCOME_UNKNOWN` | The outcome of remote work was explicitly marked unknown; a write may have succeeded. |

The independent flags can coexist. `request_cancellation()` records intent only.
`observe_cancellation()` requires typed `CancellationEvidence` for a local task
cancellation or cooperative acknowledgement. Telemetry observing an actual
`asyncio.CancelledError` records local evidence and re-raises the same exception.
Neither a timed-out wait nor `abandon_result()` supplies cancellation evidence.
`mark_remote_outcome_unknown()` is explicit; timeout classification does not infer
whether a remote request was dispatched or whether a remote write failed. A false
flag means no such event was recorded, not proof of a known remote outcome.
These methods record observations; none cancels, waits, or abandons work itself.

## Attempt and stage telemetry

Each current logical model call creates one `AttemptTelemetry`, nested in its
component span, with a new `logical_call_id` and `attempt_number=1`. Primary
routing, optional recovery, and synthesis have distinct identities. Recovery is
never routing attempt 2. Local output validation belongs to the logical call, so
an invalid structured result marks its attempt failed even if usage was returned.

An attempt retains component, monotonic start offset/duration, budget before/after,
status, failure category, sanitized exception type, SDK-visible requests, returned
token counters, and `usage_known`. Retry eligibility/reason/denial/delay remain
`None` because no application retry decision is made. `retry_performed=False`;
`http_retry_count=None` because lower-layer retries are not observed. SDK request
counts do not supply an HTTP retry count. Existing provider retries remain intact.

Usage is copied before production aggregation. Repeated hook/result observations
update the same attempt, not additional consumption. Missing usage is unknown,
never a zero estimate. `unknown_usage_attempt_count` counts recorded attempts
without returned token evidence. Existing `usage_completeness` describes returned
SDK evidence, not a complete provider bill; instrumentation failures can make the
observation incomplete. Aggregate production accounting still includes routing,
optional recovery, and synthesis; evaluation-only usage remains separate.

Stages retain remaining budget before/after. `allocated_allowance_ms` and
`timeout_deadline_source` remain `None`: no stage allowance is allocated and no
authoritative timeout source has been observed. Existing span and attempt durations
use monotonic `perf_counter`; budget calculations use the budget's injected clock.
Only relative budget values cross clock domains, never absolute timestamps.

When recovery starts, `recovery_admission` captures `AdmissionEvidence` with the
observed remaining budget. Reserve, minimum, allowance, admission, and denial
remain `None`, distinguishing an unevaluated decision from either admitted or
denied. This adds the evidence shape without changing recovery policy.

Request-local ContextVars reset on success, exceptions, and nested invocations.
Observation failures remain non-authoritative. JSON adds only structural counters,
timings, IDs, flags, and enum reasons. It does not serialize budget clocks, raw
prompts, provider bodies, tool payloads, customer facts, or exception messages.

## Operation reliability contract: designed, implementation deferred

Attaching an unused contract to `Operation` or the capability registry would
change execution-plan serialization without a consumer in this foundation phase.
Keep the executor's existing one-attempt behavior. Before any future tool retry
policy, add a registry-owned contract with these conservative defaults:

| Field | Proposed default |
| --- | --- |
| `read_only` | `False`, unless explicitly declared by the tool owner |
| `idempotent` | `False`, unless explicitly declared |
| `retry_safe` | `False` |
| `max_attempts` | `1` |
| `retryable_failure_categories` | Empty set |
| `idempotency_key_supported` | `False` |
| `outcome_reconciliation_supported` | `False` |

Read-only or idempotent declarations alone must not enable retries. In particular,
a timed-out write requires explicit remote-outcome handling rather than assuming
failure and repeating it. No tool contract or retry behavior is activated here.

## Validation and next phase

`tests/test_request_budget.py` advances injected clocks without sleeping and checks
admission, reserves, parent bounds, invalid inputs, independent concurrent budgets,
and truthful state distinctions. `tests/faults/test_request_budget_telemetry.py`
uses the 13B harness around real policy/execution/projection to check unchanged
calls, order, inputs, outputs, exceptions, recovery, usage, and sanitized telemetry.
The existing fault matrix remains the regression contract.

Run `python -m pytest tests -q`; no live evaluations are needed.

13C.2 still needs an explicitly approved deadline/enforcement policy, dispatcher
admission including recovery reserve decisions, cooperative cancellation/result
acceptance semantics, and a verified transport retry boundary. Single-owner retry
implementation, lower-layer retry disabling, backoff, and tool reliability contract
wiring are future work, not enabled by these primitives.
