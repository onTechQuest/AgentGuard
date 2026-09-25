# Milestone 13C.3B: AgentGuard-owned model retries

AgentGuard owns retry decisions at `model_execution.run_model()`. Normal requests
still use `ModelRetryPolicy.disabled()`: `enabled=False`, `max_attempts=1`, and
zero shared extra attempts. No production delay, deadline, reserve, or enabled
retry allowance has been selected. Tools and whole requests are never retried.

The lower-layer configuration remains `ModelRetrySettings(max_retries=0)` on
every SDK attempt. It disables both the scoped OpenAI client retries and Agents
SDK replay. [Transport verification](RETRY_OWNERSHIP.md) describes that boundary
and its limitations for custom providers/transports.

## Explicit policy API

`src/agent/retry_policy.py` defines immutable `ModelRetryPolicy` and `Backoff`.
Controlled callers can pass `retry_policy=` to `run_support_agent_detailed()`.
The existing CLI does not enable it. A policy declares:

| Field | Meaning |
| --- | --- |
| `enabled` | Explicit opt-in; defaults to false. |
| `max_attempts` | Total attempts for each logical call, including its first. |
| `shared_extra_attempts_per_request` | One pool across routing, recovery and synthesis. |
| `eligible_failure_categories` | Can restrict the conservative eligibility rules below, not bypass them. |
| `transient_http_statuses` | Selected 5xx statuses; initially 500, 502, 503, 504. |
| `backoff` | Explicit `Backoff(initial_delay_ms, multiplier, jitter)`; jitter optionally maps `(delay_ms, failed_attempt)` to milliseconds. |
| `maximum_retry_delay_ms` | Upper bound for any admitted retry delay. |
| `retry_after_policy` | `honor` guidance or `deny` retries that include it. |
| `minimum_attempt_budget_ms` | Positive useful-attempt allowance required when enabled. |
| `downstream_reserve_ms` | Budget held for required downstream work. |

Enabled policies require explicit backoff, maximum delay, and useful-attempt
allowance. Tests supply synthetic values; these are not production defaults.
`retry_sleeper=` accepts seconds and can advance a fake monotonic budget clock.
`retry_wall_clock=` supplies epoch seconds for HTTP-date Retry-After parsing.
The existing `RequestBudget(clock=...)` provides authoritative monotonic time.
Ordinary execution uses system clocks and a synchronous sleeper when explicitly
enabled. No background worker or cancellation/preemption mechanism is added.

## Delivery certainty and eligibility

Decisions use typed exceptions, HTTP status and headers, never exception message
phrases. The public OpenAI exception chain exposes the pinned transport causes.

| Evidence | Delivery certainty | Retry eligibility under an enabled policy |
| --- | --- | --- |
| Typed `httpx2.ConnectError` / `ConnectTimeout` underneath SDK connection/timeout error | `NOT_SENT` | Eligible network/connect timeout. |
| HTTP 429 | `KNOWN_FAILED` | Eligible rate limit. |
| Explicit selected transient 5xx | `KNOWN_FAILED` | Eligible provider failure. |
| Read/write error, read timeout, protocol failure, or connection error without typed before-send evidence | `SENT_OUTCOME_UNKNOWN` | Denied. |
| Authentication, authorization, configuration, invalid output, model protocol, policy failure, deadline or cancellation | As evidenced, often unknown | Denied. |

`KNOWN_FAILED` means a failure response was received; it does not assert that
provider processing or billing was zero. A read/write failure cannot inherit
`NOT_SENT` from an earlier connect exception's context. Unsupported providers
without sufficient evidence are not presumed safe to replay.

## Admission, shared allowance and waiting

Each request has its own context-local mutable allowance around an immutable
policy. Only a dispatched extra attempt consumes it. Exhausting either the
per-call maximum or shared allowance records `retry_exhausted=True`. Consuming
the last allowance on a successful retry alone is not an exhaustion failure.

An eligible retry must fit:

`delay + minimum_attempt_budget_ms + downstream_reserve_ms`

Admission also respects the existing recovery policy: recovery retries use the
larger minimum and the larger downstream reserve from the two policies. The
original request deadline is never reset or extended. Unlimited requests still
obey attempt limits and the maximum delay. Cancellation signals deny retries
even when the supplied RequestBudget is unlimited.

Backoff is bounded exponential delay with optional injected jitter. Retry-After
supports milliseconds, numeric seconds and HTTP dates. The chosen delay is at
least the valid server guidance. Guidance over the configured delay limit or
remaining useful budget is denied; it is never shortened to force a dispatch.
Malformed guidance is denied. Reasons include `RETRY_AFTER_EXCEEDS_BUDGET`,
`RETRY_AFTER_EXCEEDS_DELAY_LIMIT`, `INVALID_RETRY_AFTER`, and
`INSUFFICIENT_RETRY_BUDGET`.

Admission runs before sleeping and again afterwards. An oversleep or cancellation
can prevent dispatch without consuming allowance. Interrupted waiting records
`BACKOFF_INTERRUPTED`. Denial retains the last provider failure, plus the retry
denial reason and budget/cancellation evidence; it never manufactures a fallback
response. Existing late-result acceptance checks remain authoritative. These
checks do not preempt an in-flight SDK call or claim remote cancellation.

## Router, recovery and synthesis

The retry loop encloses only `Runner.run_sync()`, preserving its model, input,
settings, schema, tools and `max_turns=1`. Local routing validation is outside
the retry loop. It cannot obtain another answer by treating invalid output as
a transient failure.

- Router attempts share one logical call ID, numbered 1, 2, and so on. A terminal
  routing failure does not authorize tools or invoke planning recovery.
- Planning completeness alone decides whether the distinct recovery logical
  call is needed. Recovery retries use the same allowance and do not create
  another completeness-review cycle.
- Synthesis retries reuse the already projected authoritative input with
  `tools=[]`. Required operations remain completed and are never repeated.

Existing explicitly injected router/recovery runners retain their extension
contract and bypass this verified SDK boundary. Their retry suppression and
attempt control are not asserted. Qualification must use the production SDK
path to exercise the policy, as the mocked-transport tests do.

## Attempts and accounting

Every controlled SDK invocation has an `AttemptTelemetry` within its component
span: logical ID, attempt number, duration, budget before/after, failure category,
delivery certainty, provider status, retry decision/denial/delay, returned usage,
usage completeness and SDK-visible requests. Failed attempts do not prematurely
mark a subsequently successful logical call or request as failed.

Per-attempt token evidence is copied before aggregation. Hooks and later caller
observations cannot count the same attempt again. Successful SDK usage totals
include all returned failed-attempt usage plus the final successful attempt,
then the existing router/recovery/synthesis aggregation applies once.

On terminal errors, attached `production_telemetry.observed_usage` sums known
usage across every observed attempt, including exhausted retries. This is the
diagnostic accounting source for failed qualification requests; existing legacy
error-specific usage fields are not a complete failed-request ledger. Unknown
failed-attempt usage remains unavailable in its attempt record. A known subtotal
with unknown attempts is `PARTIAL`; entirely unknown consumption is `UNAVAILABLE`.
No provider cost is estimated or assigned zero for missing evidence.

Request summaries expose `retry_policy_enabled`, `retry_allowance_initial`,
`retry_allowance_consumed`, `retry_allowance_remaining`, `retry_attempts_total`
(extra attempts), and `retry_exhausted`. A performed retry sets the component's
`retry_source="agentguard"`. HTTP attempt counts remain unknown in production
without a transport observer; configuration is separate from measured evidence.
Observation failures cannot change retry eligibility, admission or dispatch.

## Offline proof and qualification remaining

`tests/test_retry_policy.py` exercises classification, policy validation, guidance
and injected backoff. `tests/test_model_retries.py` reuses the real SDK/OpenAI
client with a mocked HTTP transport and fake clocks. Dispatch counts reconcile
to AgentGuard attempts, and each HTTP retry header remains zero. Tests cover
router/recovery/synthesis, shared exhaustion, budget/reserve denial, unknown and
known failed usage, cancellation, concurrency, immutable inputs, and no repeated
tools. The default-path transport suite and 28-case fault matrix remain required.

Run `python -m pytest tests -q`. No live calls are needed for this validation.
Before enabling retries in qualification, choose explicit measured delay,
attempt, reserve and deadline policies; validate transient incidence, delivery
evidence, token/cost completeness, tail latency and success benefit on the chosen
provider/SDK path. Production activation is a later decision. No production
values, live qualification, quality gates, datasets or CI changes are included.

Milestone 13C.4A adds a separate [reliability qualification harness](RELIABILITY_QUALIFICATION.md)
for named candidates, shadow admission analysis and controlled opt-in execution.
It does not activate this policy for normal production requests.
