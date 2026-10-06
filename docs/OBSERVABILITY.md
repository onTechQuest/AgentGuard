# Observability hooks

AgentGuard owns execution evidence and evaluation results, not a monitoring
platform. This optional projection has no clocks, model/tool/judge calls, retries,
thresholds, or gate authority. Existing telemetry, observations, and failure
envelopes remain authoritative. No SDK, collector, database, or vendor dependency
is required.

## Contract and correlation

Version 1 events have `event_type` (`SPAN`, `METRIC`, `EVALUATION`),
`schema_version`, nullable source `timestamp`, `run_id`, nullable `invocation_id`
and `request_id`, `scenario_id`, `repetition`, `trace_id`, `span_id`, and nullable
`parent_span_id`. A timestamp is the existing request start anchor, not an event
emission time or a child-span start. Legacy timestamps remain null.

Trace IDs are the first 32 hex digits of SHA-256 of Python's JSON encoding of
`[run_id, scenario_id, repetition, request_id]`. Span IDs are the first 16 hex
digits of SHA-256 of `trace_id + ':' + logical_label`; component labels include
their ordered ordinal. Existing request IDs are retained unchanged when safe;
unsafe identities are omitted, never rewritten. Repetitions have separate traces.
Receipts link invocation IDs by exact evaluation run ID, never by newest run.

Spans have `name`, `component`, `status` (`OK`, `ERROR`, `UNAVAILABLE`), nullable
`duration_ms`, and allowlisted attributes. A completed evaluation span means the
evaluation produced evidence, not that every assertion passed. Evaluation events
retain the existing deterministic pass flags, safety flag, semantic scores and
availability. Optional quality decisions are explicitly **run scoped**.

Missing values stay null. Child durations are not summed into request latency.
Legacy tool aggregate latency is labeled `retained_component_aggregate`; it does
not establish a tool name or success. Evaluation duration is unavailable.

## Observers

`src.agentguard.observability.observe(observer)` opts in around an existing runner:

```python
from src.agentguard.observability import observe, JsonObserver

with observe(JsonObserver(project_root)):
    existing_evaluation_runner()  # The caller's one existing execution.
```

Default `NullObserver` skips capture/projection/export. An observer implements
`emit_span(event)`, `emit_metric(event)`, and `emit_evaluation(event)` for projected
contract events. Capture caches only sanitized span/metric projections; final
evaluation evidence is emitted after lineage finalization. Export errors are
best-effort and cannot change evaluation results or mask execution exceptions.
`ConsoleObserver` prints compact evidence. `JsonObserver` writes deterministic
`reports/observability/<run_id>/<request_id-or-scenario>.json`; repeated attempts
have a repetition suffix. Unsafe filesystem labels use the trace ID. Reports are
already gitignored. Exporters consume projected events, never arbitrary payloads.

## Inspection and metrics

```powershell
.venv/Scripts/python.exe scripts/inspect_observability.py --run-id 17000000000000000000000000000001 --scenario-id order_status_001
.venv/Scripts/python.exe scripts/inspect_observability.py --run-id 17000000000000000000000000000002 --scenario-id order_status_001
```

First copy the committed synthetic examples as shown in the
[offline demo](DEMO_GUIDE.md). These commands do not depend on
uncommitted historical live reports. The CLI validates retained lineage artifacts, reads observations/failures, and
executes nothing. Add `--json` to export that projection. It does not reconstruct
missing legacy component evidence. A new opt-in successful capture can show:

```text
agentguard.request                OK
  agentguard.router               OK
  agentguard.tool.get_order_status OK
  agentguard.synthesis            OK
  agentguard.evaluation            OK
```

The committed synthetic deadline example shows:

```text
agentguard.request          19000ms ERROR
  agentguard.router         1200ms OK
  agentguard.tool.UNKNOWN       1ms UNAVAILABLE
  agentguard.synthesis      17799ms ERROR
  agentguard.evaluation             UNAVAILABLE
```

Synthesis retains `failure_category=DEADLINE_EXHAUSTED` and
`result_abandoned=true`. Recovery appears only with observed evidence.
Metrics include request/router/recovery/synthesis/tool latency, production token
total, and retry count when known. Evaluation metric names, units, versions, and
population applicability come from `metric_registry.py`; events are individual
observation samples, not newly computed run aggregates or gate decisions.
There is no invented semantic pass threshold or competing semantic-pass metric.

## OpenTelemetry and privacy

`otel_span(event)` deterministically maps IDs, names, status and safe attributes
to OTel concepts (`UNAVAILABLE` maps to `UNSET`, observed models to
`gen_ai.response.model`). It preserves known duration and does not invent absolute
child start/end times. This is an adapter input, not an OTLP wire document.

Enterprise extension: **AgentGuard → observer → OTel adapter → existing company
collector/backend**. A company can implement the observer using its existing SDK
and route through its collector to Datadog, Splunk, New Relic, Grafana/Tempo,
Honeycomb, or another supported backend, without changing evaluation/runtime
logic. Actual exporters, authentication, transport, and vendor integrations are
outside this milestone.

Projection uses strict field/type allowlists, registered tool names, safe model
identities, and the existing Redactor. No prompts, responses, payloads, headers,
environment values, exception messages, credentials, customer identifiers, or
operation labels are copied. Correlation IDs are metadata, not customer IDs.
