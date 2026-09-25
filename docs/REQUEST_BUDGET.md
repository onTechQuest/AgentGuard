# Milestone 13C.2: request admission and result acceptance

The runtime still routes, optionally recovers planning, resolves policy, executes
required tools once, projects their results, and synthesizes with `tools=[]`.
Explicit finite budgets now gate admission and result acceptance. Unlimited/default
requests retain their existing behavior. No numeric production timeout,
asynchronous orchestrator or thread wrapper is added. Since 13C.3B, an explicit
[model retry policy](MODEL_RETRY_POLICY.md) can use this same budget; retries
remain disabled by default.

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
dispatch guarantee. Runtime boundaries check before work starts and recheck before
model/tool dispatch after preparation.
The downstream reserve bounds time; it does not allocate concurrent resource quotas.

`run_support_agent_detailed(..., request_budget=budget)` accepts an optional
budget. **Only explicitly finite budgets enable enforcement.** Default execution
creates an unlimited budget; unlimited cancellation signals retain their earlier
observation-only behavior. A caller-supplied budget
may start before this function; request latency still measures this invocation,
while budget remaining reflects the caller's original absolute deadline.
Use opaque request IDs and labels, never customer identifiers or secrets.

## Authoritative execution boundaries

`src/agent/request_execution.py` scopes enforcement separately from best-effort
telemetry. Its `stage()` context checks `RequestBudget.admission()` before primary
routing, recovery, policy resolution, execution-plan construction, every required
operation, and synthesis. `admit()` also checks before model/tool dispatch after
preparation. No duplicated monotonic comparisons live in these callers.
An observation failure cannot disable admission. A broken authoritative clock
fails closed rather than authorizing work with an unknown deadline.

`RequestDeadlineExceeded` carries component, admission evidence, and whether the
stage body completed before rejection. It propagates as a typed terminal exception,
without being replaced by router, planning, or generic tool errors. Existing
unlimited exception paths remain unchanged. `EvaluationRecord`'s default execution
path remains unlimited; explicitly budgeted callers can inspect the exception's
production telemetry and, when available, execution trace/planning evidence.

After successful work, the boundary checks acceptance. At the deadline itself
(`remaining_ms()==0`) the result is too late. Completed model attempts retain their
usage and completed status; completed tools retain their invocation, projected
output, and completed status. Their results are marked unaccepted/abandoned for
continued request processing. An unstarted operation stays pending. Required tool
execution includes privacy projection, so preserved evidence never exposes raw
tool payloads. No next operation or synthesis runs after rejection.

Synthesis still requires satisfied execution obligations and receives `tools=[]`.
A late synthesis answer is never returned as success. A final finite-budget
acceptance boundary also covers local processing after synthesis. No fallback
answer is generated. These are synchronous admission/acceptance guarantees, not
preemption: in-flight Python work and provider requests can finish and incur cost.

## Recovery reserves

Callers may pass `recovery_budget_policy=RecoveryBudgetPolicy(...)` with explicit
`recovery_allowance_ms`, `required_execution_reserve_ms`, `synthesis_reserve_ms`,
and `completion_reserve_ms`. All defaults are unspecified (`None`); they introduce
no numeric production policy. For finite budgets, admission requires remaining
time to cover the recovery allowance plus all supplied downstream reserves.
Exact positive requirements are admitted; zero available work allowance is denied.

Insufficient reserve raises `RequestDeadlineExceeded` with denial reason
`INSUFFICIENT_ALLOWANCE`, even when the absolute deadline has not elapsed.
`deadline_exhausted` truthfully remains false in that case. Recovery is never
silently skipped in favor of the suspicious original plan. Admission denial
before planner creation records zero recovery attempts. Recovery remains its own
logical call, not a retry. Reserves govern admission, not a new component timeout:
synchronous recovery is not preempted if it consumes its allowance, and its result
must still satisfy the authoritative request deadline.

## Independent deadline and cancellation evidence

`ReliabilityState` and the telemetry failure taxonomy distinguish:

| State | Meaning |
| --- | --- |
| `TIMEOUT` | A component reported a timeout; this alone proves neither request deadline exhaustion nor cancellation. |
| `DEADLINE_EXHAUSTED` | Request rejection because the deadline was reached or admission cannot satisfy required reserves; the separate `deadline_exhausted` flag describes actual elapsed time. |
| `CANCELLED` | Local cancellation occurred or finite-budget admission honored an explicit cancellation signal. Only direct evidence sets `cancellation_observed`. |
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
Deadline rejection never requests cancellation or claims it was observed. No
cancellation API is invoked by deadline enforcement. An explicitly recorded
cancellation signal on a finite budget denies new work with `RequestBudgetRejected`;
it still does not prove remote cancellation or the outcome of a remote write.

## Attempt and stage telemetry

Each logical model call starts with one `AttemptTelemetry`, nested in its
component span, with a new `logical_call_id` and `attempt_number=1`. Primary
routing, optional recovery, and synthesis have distinct identities. Recovery is
never routing attempt 2. Local output validation belongs to the logical call, so
an invalid structured result marks its attempt failed even if usage was returned.

An attempt retains component, monotonic start offset/duration, budget before/after,
status, failure category, sanitized exception type, SDK-visible requests, returned
token counters, and `usage_known`. Default requests make no application retry
decision, leaving retry eligibility/reason/denial/delay `None` and
`retry_performed=False`. Explicit 13C.3B policies record decisions and additional
numbered attempts under the same logical ID. For all policies,
`http_retry_count=None` because lower-layer retries are not observed. SDK request
counts do not supply an HTTP retry count. Since 13C.3A, lower-layer model retries
are disabled through scoped SDK settings; [Retry ownership](RETRY_OWNERSHIP.md)
documents the transport proof. Configuration evidence remains separate from counts.

Usage is copied before production aggregation. Repeated hook/result observations
update the same attempt, not additional consumption. Missing usage is unknown,
never a zero estimate. `unknown_usage_attempt_count` counts recorded attempts
without returned token evidence. Existing `usage_completeness` describes returned
SDK evidence, not a complete provider bill; instrumentation failures can make the
observation incomplete. Aggregate production accounting still includes routing,
optional recovery, and synthesis; evaluation-only usage remains separate.

Finite stages retain remaining budget before/after, latest admission decision and
denial reason, available `allocated_allowance_ms`, and `timeout_deadline_source`
of `request_budget`. The allowance is an admission window, not an SDK timeout.
They also record `late_completion`, `result_accepted`, and `result_abandoned`.
Denied stages are `not_started`; late successful work remains `completed` while
the request fails. Attempts retain completed status and acceptance evidence
separately. Unlimited stages leave the admission fields unset. Request metadata
includes original budget and monotonic deadline (meaningful only in its clock
domain), exhausted/abandoned flags, and truthful cancellation evidence.
Existing span and attempt durations
use monotonic `perf_counter`; budget calculations use the budget's injected clock.
Only relative budget values cross clock domains, never absolute timestamps.

For finite recovery, `recovery_admission` captures remaining budget, required
downstream reserve, minimum work allowance, admitted/denied, and denial reason.
Unlimited recovery retains the unevaluated evidence shape with `admitted=None`.

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
`tests/faults/test_deadline_execution.py` covers finite admission and late-result
boundaries, explicit reserves, truthful completed-tool evidence, synthesis rejection,
concurrency, sanitized telemetry, and enforcement independent of observers.

Run `python -m pytest tests -q`; no live evaluations are needed.

13C.3A verifies and disables lower-layer model retries. 13C.3B implements
disabled-by-default [application retry control](MODEL_RETRY_POLICY.md), including
eligibility, retry admission, backoff, and failed-attempt usage. Numeric production
deadline and reserve policy, cooperative cancellation, and tool reliability
contract wiring remain unconfigured.
