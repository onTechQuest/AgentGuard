# Milestone 13A: production reliability telemetry

`src/agent/telemetry.py` provides request-local in-memory observations without
changing production decisions, exceptions, models, prompts, retry settings or
timeouts. It uses UTC start metadata and `perf_counter()` durations. There is no
exporter, background worker, telemetry service or automatic file persistence.

## Lifecycle and consumers

`run_support_agent_detailed()` creates one `ProductionExecutionTelemetry`. On
success it is available as `result.context_wrapper.production_telemetry`; on
failure the original exception carries `production_telemetry` when attachment is
possible. The original exception is re-raised. Existing structured failures still
produce the same empty-answer/error `EvaluationRecord`, now with telemetry too.
Other exceptions still propagate; callers can inspect their attached observations.

`request_label=` is optional opaque caller metadata. Never pass a prompt, customer
identifier, credential or other sensitive value as the label. Evaluation datasets
and scenario IDs do not participate in runtime decisions.

Context variables isolate the request, current span and failure observation.
Every token resets in `finally`, including nested requests and failures. There is
no shared mutable collector. Observation and serialization errors degrade to
partial/missing telemetry; they never authorize, suppress or repeat work.

Milestone 13C.1 adds an observation-only `request_budget=` argument and per-call
attempt records. See [Request budget and attempts](REQUEST_BUDGET.md) for admission
primitives, cancellation evidence, recovery admission metadata, and the deferred
operation reliability contract. Default production has an unlimited budget and
still has no application retry loop or enforced deadline.

## Spans and authoritative evidence

Spans cover primary routing, completeness validation, optional recovery, policy
resolution, plan construction, required execution, each tool operation, projection
and synthesis. Tool duration is copied from `ExecutionTrace`, which remains the
source of truth for operation status, invocation and completion. Projection is a
nested span: do not add its duration to tool duration. Required-execution and
completeness spans also contain child spans and are not additive with them.

Planning summaries copy only recovery trigger/count/source from `PlanningResult`.
Operation summaries contain required/completed/incomplete operation IDs, not
arguments or returned facts. No second obligation engine or tool executor exists.
No-grant and clarification outcomes are successful business outcomes, not
reliability failures. A no-grant outcome is named `NO_AUTHORIZED_OPERATIONS`, since
absence of grants alone does not prove a policy denial.

## Failures and accounting

Failure category, component and sanitized exception type are separate. Categories
cover timeout, rate limit, authentication/authorization, network/provider,
invalid model output, missing/erroring tools, projection, incomplete obligations,
prohibited operations, model protocol, configuration and unknown failures. Nested
failures retain the first observed cause rather than replacing it with a generic
parent obligation failure. Provider bodies and exception messages are excluded.

Component token counters are copied before production aggregation. The existing
aggregate remains primary routing + optional recovery + synthesis. `observed_usage`
sums only available counters; on partial observations it is not a complete bill.
`usage_completeness` means:

- `COMPLETE`: all observed logical model calls have returned token evidence.
- `PARTIAL`: some usage is known, but another call or observation is incomplete.
- `UNAVAILABLE`: no model token usage is known.

This describes SDK-visible accounting, not complete HTTP consumption. Logical
calls, SDK-visible requests and transport retries are distinct. Recovery is a new
logical call, not a retry. HTTP retry count/source remain `null` without transport
evidence. Failed-attempt tokens are never estimated. SDK default zeros without
usage evidence are represented as unavailable in telemetry. Existing aggregate
scorecard formulas remain unchanged.

Configured and resolved model identities are separate. The normal SDK path records
its existing default resolution; injected implementations without resolution
evidence leave `resolved_model` unset. Provider alias/version resolution beyond
the configured SDK identity is not inferred.

Semantic and safety scores optionally carry separate `EvaluationUsage` records.
These capture available DeepEval and action-classifier counters, never production
tokens. Evaluation failures and provider retries may still lack complete usage.

## Privacy and diagnostics

`telemetry.snapshot(record)` returns a JSON-safe dictionary or `None` if observation
fails. It contains counters, timings, component/model identities, opaque operation
IDs and sanitized failure types. It excludes prompts, output text, order IDs,
tool arguments, internal/projected tool payloads and credentials.

`audit_token_usage.py` and `audit_latency.py` consume these shared observations;
profiling does not monkeypatch `Runner` or tool callables. Mandatory operations run
before synthesis; synthesis receives `tools=[]`. Existing diagnostic report fields
and qualification populations remain intact. Older/injected records without
telemetry report unknown component reconciliation rather than manufacturing it.

Generated reports belong under gitignored `reports/`. Existing diagnostic reports
also retain `EvaluationRecord` for offline evaluation, including its captured
request/answer/projected facts; that existing artifact is not the sanitized telemetry
view. Consumers needing counters only should serialize `production_telemetry` alone.

Offline validation: `python -m pytest tests -q`. No live evaluations are required.
