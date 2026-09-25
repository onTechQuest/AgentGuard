# Latency measurement audit

This document records the earlier diagnostic and its then-current release
semantics. The finalized CLI and gate separation are documented in
[Performance qualification](PERFORMANCE_QUALIFICATION.md); the measured evidence
below is preserved unchanged.

The diagnostic implementation described below is historical. Milestone 13A now
consumes [request-local production telemetry](PRODUCTION_TELEMETRY.md) without
patching Runner/tool callables. Mandatory tools execute before synthesis, and
their timing includes projection. The older support-phase measurements below
describe the architecture at the time of that diagnostic, not the current path.

## Current release metric

`scripts/run_agentguard_eval.py` supplies eight functional smoke records to
`build_scorecard`. Eight safety executions contribute safety scores but their
records do not enter the performance aggregate. Failed production execution
currently stops the release runner rather than producing a completed scorecard.

`execute_scenario` uses `time.perf_counter()` around
`run_support_agent_detailed`: semantic routing, deterministic policy resolution,
support-agent model turns, authoritative tools, projection and SDK orchestration
are included. Record extraction, deterministic scoring, DeepEval and safety judges
are outside that timer. The latency boundary represents end-to-end production
service work, although failed transport attempts may lack corresponding token usage.

The scorecard uses nearest-rank P95: `(95 * n + 99) // 100`, one-based. For eight
records it selects the eighth/largest value. The configured maximum remains
7,500 ms. No percentile, gate, runtime prompt, model, timeout or retry setting was
changed in this audit.

## Diagnostic architecture

`scripts/audit_latency.py` executes five sequential passes through the eight
functional smoke scenarios using the existing `execute_scenario` path. There
are no judge calls, warmup exclusions, trimmed values, parallel load, or retries
of failed observations. An attempted-observation marker is saved before execution,
then replaced with its result. Failure durations and sanitized error types are
retained separately alongside the completed-execution distribution.

The shared diagnostic observer in `scripts/audit_token_usage.py` captures each
router/support `Runner.run_sync` phase and its usage before router usage is merged.
Opt-in tool timing wraps the SDK's public `on_invoke_tool` callable. It forwards
arguments, returns the original result, propagates exceptions, and restores the
callable after the run. Tool schemas and model requests are unchanged.

Tool time covers argument handling, business lookup and projection. It is nested
inside support-phase time. Tool durations must not be added to router plus support
durations; concurrent tool sums would not equal elapsed critical-path time either.
Support-phase duration includes SDK orchestration and model/network waits. It is
not a measurement of model-server computation alone. Diagnostics add small timing
overhead to the otherwise unchanged production path.

SDK-visible failed-attempt counts are recorded where available. Zero in that
field does **not** establish zero HTTP retries. Transport retries, their token cost,
provider queueing and network delays are unknown without lower-level evidence.

The JSON retains chronological observations, repetition IDs, UTC start times,
monotonic durations, component/model usage, tool trajectories, captured records,
per-scenario distributions and every observation above the YAML limit. Generated
data is under gitignored `reports/latency_audit_smoke.json`.

## Metric design recommendations (not implemented)

1. Keep the current eight-observation gate honest: it is an observed maximum
   expressed as nearest-rank P95, useful as a small smoke check but a noisy
   estimator of population tail latency. Report sample count and percentile rank.
2. Twenty samples are the first size where nearest-rank P95 is not the maximum
   (rank 19). Forty select rank 38, the third-largest value. Neither guarantees
   a stable estimate. As an initial engineering benchmark, collect 100–200
   representative observations across several time windows, giving roughly
   5–10 expected observations in the upper 5% under independent sampling.
   Determine the final sample requirement from variability and desired precision;
   correlated repeated requests provide less information than independent samples.
3. Separate correctness smoke coverage from intentional performance sampling.
   Repeating production requests for a benchmark need not repeat paid judges.
   Keep workload weights, execution order, model/SDK versions and concurrency
   explicit and comparable. Repeated passes help distinguish scenario effects
   from time/order effects, but this diagnostic alone cannot prove either cause.
4. Report safety production latency separately by workload/category. It is real
   user-serving cost currently omitted from the performance aggregate. A future
   combined metric should use an explicitly justified production mix; simply
   pooling safety and functional samples can conceal workload changes.
5. PR correctness CI and scheduled/manual performance validation should share
   timing definitions and instrumentation, but need not share sample sizes or
   gate policy. Keep the existing PR threshold and behavior until a reviewed
   design establishes benchmark baselines, sample requirements and decision rules.
6. Preserve external-latency spikes, failure counts, maxima, exceedance rates
   and component timings. Add uncertainty to repeated-run comparisons rather
   than trimming observations or rerunning until a pass. More detailed transport
   telemetry could investigate retries/connection/queue effects before changing
   production timeouts, retries or models.

For even sample sizes the report provides both nearest-rank P50 and conventional
median (the mean of the middle pair). Per-scenario P95 with five samples is the
maximum and is labeled as such. P99 with forty samples is also the maximum.

The prior Run B 22,493 ms maximum cannot be attributed from its aggregate alone.
Fresh diagnostic evidence describes this new sample, not that historical event.

## Results: forty completed observations

Five executions per functional smoke scenario completed, with zero failed
executions and no additional diagnostic retries. No production runtime, dataset,
quality gate or CI configuration was changed. All components used `gpt-5.6-luna`.

| Statistic | Latency ms |
| --- | ---: |
| Mean | 4,220 |
| Median | 4,104 |
| Nearest-rank P50 | 4,088 |
| P90 | 4,966 |
| P95 | 5,184 |
| P99 | 9,898 |
| Minimum | 3,311 |
| Maximum | 9,898 |

One observation exceeded 7,500 ms: **1/40 = 2.5%**. The largest observation is
still present in the mean, P99, maximum and exceedance reporting. P95 uses rank
38 of 40 and does not select it; the formula is unchanged.

| Scenario | Count | Mean ms | Min ms | P95 = Max ms |
| --- | ---: | ---: | ---: | ---: |
| order_status_001 | 5 | 5,202 | 3,622 | 9,898 |
| order_status_002 | 5 | 4,169 | 3,728 | 4,966 |
| return_001 | 5 | 3,861 | 3,311 | 4,369 |
| missing_order_001 | 5 | 4,405 | 4,119 | 5,184 |
| order_status_003 | 5 | 3,741 | 3,560 | 3,952 |
| return_002 | 5 | 4,652 | 4,065 | 5,461 |
| tool_routing_001 | 5 | 4,095 | 3,738 | 4,284 |
| grounded_response_001 | 5 | 3,638 | 3,311 | 4,264 |

### Outlier attribution

`order_status_001`, repetition 1, was the first observation of the process:

| Evidence | Outlier | Same-scenario normal-peer comparison |
| --- | ---: | ---: |
| End-to-end ms | 9,897.53 | Remaining observations: 3,622–4,698 ms |
| Router phase ms | 4,811.70 | Median 1,551.15 ms |
| Support phase ms, including tool | 5,081.22 | Median 2,241.15 ms |
| Tool invocation ms | 0.96 | All forty tool invocations <= 1.13 ms |
| SDK request count | 3 | 3 in every observation |
| Total tokens | 1,188 | Peer median 1,190.5 |
| SDK-visible failed attempts | 0 | 0 across all observations |

The tool name and arguments match the normal repetitions. Both model-facing
phases were slower, with no extra SDK-visible model request, token spike or slow
tool. End-to-end time outside the measured router/support phases ranged from
2.27 to 16.26 ms across the forty observations; it did not account for the spike.

This establishes where elapsed time accumulated, not its remote cause. First-run
initialization/connection effects, provider/model tail latency and network delay
are possible; none is proven. HTTP retries remain **unknown**. The same scenario's
other four observations do not show a consistently slow scenario. This small
sample provides no evidence justifying production prompt, model, tool, timeout or
retry changes. Investigate transport timing before making such changes.

### Token regression and verification

- Mean tokens: **1,206.675** (reported as 1,206.68).
- Nearest-rank P95 tokens: **1,313**.
- Minimum/maximum tokens: **1,122 / 1,320**.
- No material regression against the approximately 1,206-token baseline.
- 120 total SDK-counted requests: 40 router responses and 80 support responses.
- All forty component usage totals reconcile with EvaluationRecord.
- No DeepEval or safety-judge calls were made.
- Offline suite: **1,137 passed**.

Changed files: `scripts/audit_latency.py` (new), `scripts/audit_token_usage.py`
(optional tool timings and partial failure diagnostics),
`tests/test_audit_latency.py` (new), and this document. Production metric and
runtime files remain unchanged. The raw JSON artifact is gitignored and was not
committed; it contains all observations and distributions at full precision.
