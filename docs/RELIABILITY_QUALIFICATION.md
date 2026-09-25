# Milestone 13C.4A: reliability qualification and shadow policies

`--suite reliability` measures the actual production request path without
deterministic evaluators, DeepEval judges, or release gates. Each selected
functional and safety dataset scenario executes once per repetition, in dataset
order. Comparing more policy candidates adds analysis, not production requests.
The mode does not inject provider faults or intentionally generate rate limits.

Since 13C.5, application, smoke, full and performance requests use the
[v1 production policy](PRODUCTION_RELIABILITY_V1.md). Reliability qualification
intentionally supplies an explicit unbounded diagnostic policy unless candidate
enforcement is selected. Production retries remain disabled.
The baseline configuration remains unlimited, single-pass,
and no-retry. The separate experimental
[`reliability-deadline-candidates.json`](../config/reliability-deadline-candidates.json)
defines L/R hypotheses; presence or selection alone does not enforce them.
See [Controlled deadline qualification](CONTROLLED_DEADLINE_QUALIFICATION.md)
for hierarchical budget semantics and the required opt-in flag.

## Commands for later authorized runs

These commands make real production requests when executed. They were **not run**
for this milestone. From the repository root, a no-retry qualification is:

```powershell
python scripts/run_agentguard_eval.py --suite reliability --qualification-config config/reliability-qualification.json --report reports/reliability_baseline.json
```

For shadow comparisons, create `reports/reliability_candidates.json` containing
approved candidate values and select the baseline as `execution_candidate`.
Run the same command with that configuration to evaluate all candidates without
executing their retries. A `--repetitions` argument overrides the configured count.

After candidate values and retry execution are explicitly authorized, the command
for controlled retries is:

```powershell
python scripts/run_agentguard_eval.py --suite reliability --qualification-config reports/reliability_candidates.json --execution-candidate candidate_A --execute-retries --report reports/reliability_controlled.json
```

The second command requires the named, enabled candidate in that local file.
No enabled numeric candidate is shipped. `--execute-retries` defaults to false;
selecting a candidate alone does not enable retries or deadlines. Candidate
request budgets and recovery reserves are descriptive unless deadline enforcement
is explicitly selected as described below. The report separately records
configured and effective policies. The retry flag
with a disabled candidate fails setup before any execution.

Reports must be new `.json` files under this project's gitignored `reports/`.
Existing reports are never overwritten. Use a fresh destination for each run.
Exit code zero means measurement/reporting completed, not that a candidate or
release passed. Observed failures remain in the report. Setup failures return
nonzero; an interrupted run leaves `complete=false`, completed observations and
an `in_progress` scenario reference. It is never resumed or retried automatically.

## Configuration schema

`config/reliability-qualification.json` is a valid baseline example. A configuration
contains only these fields:

| Field | Purpose |
| --- | --- |
| `dataset_suite` | `smoke` or `full`, using the existing validated dataset loader. |
| `repetitions` | Positive explicit repetition count; no hidden warmup exclusions. |
| `execution_candidate` | One unique named candidate; actual enforcement requires its corresponding explicit CLI opt-in. |
| `recovery_scenario_ids` | Optional existing selected scenario IDs marking a recovery-probe cohort. |
| `candidates` | Named configurations compared by shadow analysis and deadline statistics. |

Each candidate has `name`, optional `request_budget_ms` (`null` means unlimited),
`retry_policy`, optional `recovery_budget_policy`, and optional
`qualification_budget_policy`. Retry fields are those of
[ModelRetryPolicy](MODEL_RETRY_POLICY.md): enablement, per-call maximum, shared
extra allowance, failure categories, selected 5xx statuses, maximum delay,
minimum useful attempt budget, downstream reserve and Retry-After policy.
`backoff` accepts `initial_delay_ms` and optional `multiplier`. Qualification JSON
intentionally excludes callable jitter to keep comparisons reproducible.

Recovery policy supports `recovery_allowance_ms`,
`required_execution_reserve_ms`, `synthesis_reserve_ms` and
`completion_reserve_ms`. These legacy candidate fields remain shadow inputs.
Direct runtime callers can still explicitly pass a `RecoveryBudgetPolicy` with
a finite `RequestBudget`; that existing API is unchanged. Unknown
configuration fields, duplicate candidates, unsupported policies and invalid
probe IDs fail before requests. Dataset expectations and runtime routing remain
unchanged; scenario IDs only select report cohorts.

## Pure shadow decisions

`reliability_policy.shadow_retry()` reuses `RequestRetryState.decide()` and
`RequestBudget.admission()`. It accepts failure category, delivery certainty,
status, Retry-After, available candidate budget, component, attempt number,
actual prior extra-attempt consumption and cancellation state. It never invokes
a model, executes a tool or sleeps.

Every candidate decision includes:

- `shadow_retry_eligible`, `shadow_retry_admitted`, `shadow_retry_denial_reason`;
- `shadow_retry_delay_ms`, `shadow_remaining_budget_ms`;
- available budget, useful-attempt requirement, reserve and remaining allowance;
- failure category, delivery certainty and server guidance;
- `shadow_dispatch_count=0` and no retry-success estimate.

`shadow_remaining_budget_ms` is budget after the proposed delay, before an
unknown-duration extra attempt. On denial it retains available budget. Candidate
remaining budget is derived from the observed attempt end offset against that
candidate's original deadline. A missing elapsed-time observation denies finite
candidate admission as unavailable evidence. Unlimited is explicitly different
from missing evidence. Missing delivery certainty remains unknown and cannot
justify replay.

Decisions are independent counterfactuals at observed failure boundaries. They
use actual prior retries, not imaginary successful retries or invented timing
shifts. Actual retry flags and counts are reported separately. If a candidate
would have changed earlier execution, later observations are not a simulation of
that alternative trajectory. Admission predicts neither success nor cost savings.

On default no-retry failures, qualification extracts typed delivery/status/guidance
from the terminal exception without recording messages or bodies. Enabled retry
attempt telemetry retains numeric Retry-After evidence, including malformed-guidance
status, so earlier failures remain available after eventual request success.

## Populations, timing and recovery

The report uses disjoint intersections of:

- functional / safety dataset;
- no recovery / recovery triggered / recovery evidence unavailable;
- zero / single / multiple required operations / unavailable operation count;
- runtime completion / observed failure / offline controlled failure.

No pooled cross-population P95 is offered as a qualification result. A completed
request is not necessarily a semantically correct or safe response: this harness
does not run those evaluators. Controlled-failure samples come from offline mocks,
not a live failure generator.

Each population retains primary routing, recovery, policy/planning, required
operations, synthesis and total-request distributions. Local policy/planning
time subtracts nested recovery from completeness review; required-operation time
includes projection and is not added to nested tool/projection spans. Component
spans include real retry waits when executed. Attempts retain their own durations.

The explicit recovery-probe cohort only marks existing requests. It neither
forces recovery nor executes those requests a second time. Every observed
recovery includes router/recovery/downstream/synthesis/total durations, recovery
tokens and available budget before recovery. Naturally occurring recovery outside
the probe cohort is also retained. Zero recovery executions produces an explicit
measurement gap and no fabricated zero-duration or zero-token samples. Small
recovery populations are flagged; production policy is never changed to fill them.

## Statistics and accounting

Statistics reuse `performance.distribution`: nearest rank `ceil(p*N/100)`;
median separately interpolates the middle pair. Reports include available N,
unavailable count, mean, maximum and supported percentiles. P50/P90/P95/P99 need
at least 2/10/20/100 observations respectively; unsupported percentiles are null
and flagged. These descriptive floors provide one expected upper-tail observation,
not stable tail estimates, confidence intervals or permission to change gates.
Existing performance qualification sample requirements remain unchanged.

Per-candidate deadline exhaustion counts use duration **greater than or equal to**
the candidate deadline, consistent with RequestBudget exhaustion at equality.
An unlimited candidate has no exceedance count/rate. Failures remain in separate
populations, and their durations can be censored by the executed deadline. These
comparisons cannot reconstruct a late result that was never accepted.

Every population includes input/output/total token distributions for routing,
recovery, synthesis and observed production totals; attempt counts, SDK-returned
requests, actual extra attempts, lower-layer retry configuration and usage
completeness counts. Unknown usage is never zero-imputed. Known totals in partial
requests are observed subtotals, not complete provider consumption. Evaluator
usage is separately zero because no judges execute. No dollar costs are calculated.

## Report safety and reproducibility

JSON includes timestamps, cheaply available Git commit, SDK versions, effective
configuration, dataset suite, repetitions, selected probes, configured and
resolved model identities, retry ownership, attempt evidence and all summaries.
Unresolved model identity stays unavailable; configured identity is not promoted
to effective identity. Lower-layer retries remain disabled on the production SDK
path; missing transport observations stay unknown rather than inferred from
configuration or SDK request counts.

Persistence uses an explicit nested measurement allowlist. Prompts, final answers,
entity values, operation IDs/arguments, projected outputs, exception messages,
provider bodies and credentials are not written. Configuration names and scenario
IDs are identifiers: never put secrets or customer data in metadata labels.
Only the already sanitized observation schema reaches the report writer.

Offline tests use pure decisions, fake measurements and the real SDK with a
mocked HTTP transport. Run `python -m pytest tests -q`; live qualification is a
separate authorized activity. Stable recovery samples, sufficient population
sizes and measured provider/cost/latency evidence are still needed before choosing
production parameters.
