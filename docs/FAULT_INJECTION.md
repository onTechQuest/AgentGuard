# Milestone 13B: deterministic fault injection

Fault injection validates how the production request path responds to failures
without calling a provider or waiting for an outage. This is an offline test
capability under `tests/faults/`, not a runtime feature or release quality gate.
Production code does not import the harness, read fault flags, inspect scenario
IDs or expose user-accessible failure controls.

## Harness and coverage

`harness.py` provides typed `Stage`, `Fault` and `Injection` values, for example:

```python
Harness(Injection(Stage.ROUTER, Fault.TIMEOUT)).run()
Harness(Injection(Stage.TOOL, Fault.EXCEPTION, tool="get_order_status")).run()
Harness(Injection(Stage.SYNTHESIS, Fault.RATE_LIMIT)).run()
```

Tests install controlled boundary adapters with pytest's `monkeypatch`. Model
calls return structured doubles or raise real SDK exception types. Tool adapters
return deterministic records, raise immediately, or deliberately return malformed
data. Real routing validation, completeness checks, authorization, execution-plan
construction, required-operation enforcement, projection and synthesis hooks run
unless their specific boundary is the injected fault. Missing/pending/prohibited
operations exercise the real executor's rejection paths.

A harness instance owns each request's injected fault, observed calls and results.
A resettable test-only context variable selects that instance. Adapters install
once before concurrent requests, so no thread repatches another thread's runtime.

`matrix.py` defines 28 failure contracts with independent expected components,
categories, escaped exception types and sanitized exception types:

| Area | Cases |
| --- | --- |
| Router | Timeout, rate limit, network failure, provider failure, invalid structured output |
| Recovery | Timeout, provider failure, invalid structured output |
| Policy/planning | Policy exception, plan-construction exception |
| Tools/projection | Missing implementation, tool exception, tool timeout, malformed return |
| Execution contract | Pending required operation, prohibited operation attempt |
| Synthesis | Timeout, rate limit, network failure, provider failure, protocol error, malformed behavior, prohibited model tool call |
| Configuration | Safe SDK configuration-error simulation |
| Sequencing/accounting | Recovery then tool failure; recovery then synthesis timeout; synthesis protocol error with returned usage; router protocol error with returned usage |

Each matrix case checks terminal failure, unique request identity, the failing
span, successful prior spans, absence of later successful work, truthful required
operation state, available usage, missing-usage completeness and unknown transport
retry counts. Direct exceptions retain identity; existing router, recovery and
execution wrappers retain their types. Separate tests verify existing structured
`EvaluationRecord` failures still contain no accepted final answer.

## Safe failure and business outcomes

Failed authoritative operations must not produce tool output, complete an
obligation, reach synthesis or substitute fabricated user claims. Synthesis
failures must not produce an accepted fallback response. Unauthorized operations
must not invoke an implementation, and every model continues to receive no tools.

The harness supplies a malicious fabricated status in its input and a different
authoritative status in successful tool results. It checks actual calls against
real policy grants. Successful tool evidence remains recorded when synthesis
subsequently fails. Negative-control tests prove the assessor detects false
completion and unauthorized calls rather than returning constant pass values.

Order not found, clarification, policy refusal and unsupported actions are tested
as valid completed requests without reliability failure categories. A concurrent
failure/success pair checks distinct request IDs, independent spans/operation IDs,
failure attachment and unaffected successful output.

## Telemetry and privacy

Fake sensitive markers are placed in prompts, provider messages/bodies/headers
and internal tool records. Serialized production telemetry must exclude them and
business identifiers. Only sanitized exception types/categories, IDs, counters
and timings survive.

The matrix found a telemetry-only defect: recording a failed operation summary
overwrote a specific `TIMEOUT` classification with generic `TOOL_ERROR`.
`telemetry.operation()` now supplies a fallback category only when none exists.
Exception propagation and production execution behavior are unchanged.

## Running and interpreting results

```powershell
python -m pytest tests/faults -q
python -m pytest tests -q
```

The pytest terminal summary reports the executed matrix's total faults, correct
classifications, retained telemetry, safe failures, fabrication/authorization
violations and pass rate. `scorecard.py` also exposes pure `assess()` and
`summarize()` helpers. Empty input has no pass rate. Additional business-outcome,
concurrency and diagnostic-helper tests are not counted as injected matrix cases.
Filtered test runs report only executed cases. No files or external telemetry
are emitted; any future saved diagnostic artifact belongs under gitignored
`reports/`.

Timeouts are raised immediately at boundaries: these tests do not establish real
deadline enforcement. Likewise, simulated SDK-visible request counts do not prove
HTTP retry counts, provider consumption on failed attempts, backoff, queueing,
network behavior or cancellation semantics. Those remain unobservable here.
There are no new retries, timeout settings, live API calls, load tests, model or
prompt changes, dataset changes, quality-gate changes, or CI changes.
