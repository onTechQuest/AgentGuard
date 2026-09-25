# Milestone 13C.5: qualified v1 production reliability policy

Normal production requests now use the Candidate R limits qualified in 13C.4F/G.
This deliberately replaces the unlimited default. It does not change model,
prompts, business routing, tool implementations, datasets, quality gates or CI.

## Policy ownership and reproducibility

[`config/runtime-reliability.json`](../config/runtime-reliability.json) is the
checked-in v1 definition, loaded once per process by
`src/agent/runtime_reliability.py`. Invalid configuration fails closed before
dispatch; it never falls back to unlimited execution. No environment variable
or evaluation configuration chooses the production policy.

| Setting | v1 value |
| --- | ---: |
| Request deadline | 20,000 ms |
| Router maximum allowance | 13,000 ms |
| Recovery admission minimum | 3,000 ms |
| Synthesis maximum allowance / recovery's downstream reserve | 6,000 ms |
| Model retries enabled | false |
| Maximum attempts per logical call | 1 |
| Shared extra attempts per request | 0 |
| Agents SDK / scoped OpenAI client retry maximum | 0 / 0 |

`RuntimeReliabilityPolicy` owns the values and model retry policy. Production
imports no evaluation or qualification modules. Qualification adapts its
candidate into this production type; its old `QualificationBudgetPolicy` name
remains a compatibility alias for the production-owned `StageBudgetPolicy`.

The outer `runtime_execution` wrapper resolves policy before telemetry starts.
`RequestBudget` and existing `stage()` boundaries enforce it using request-local
context variables. A supplied shorter request budget still applies. Passing
`None`, an unlimited `RequestBudget()`, or a larger budget does not bypass v1;
limiting a caller budget preserves its original start and cancellation signals.

Tests and diagnostics can deliberately opt out:

```python
from src.agent.runtime_reliability import RuntimeReliabilityPolicy

result = run_support_agent_detailed(
    message, runtime_reliability_policy=RuntimeReliabilityPolicy.unbounded()
)
```

Legacy reserve tests may combine that explicit policy with a finite request budget
and `RecoveryBudgetPolicy`. A conflicting loose retry or recovery override is
rejected under v1. The retry engine remains available for future controlled use
through an explicit runtime policy containing the chosen retry policy; v1 does
not activate it.

## Deadline semantics and failure safety

The 13-second router cap, 3-second recovery minimum and 6-second synthesis cap
are **not an additive 22-second reservation**. The original 20-second request
deadline bounds every child. Stage preparation consumes its allowance, and
repeated checks do not reset the stage clock.

Recovery is optional and distinct from retry. At admission, remaining request
time must cover at least 3 seconds of recovery work plus a full 6-second synthesis
reserve. After a 12-second router, the remaining 8 seconds cannot cover that
9-second requirement, so recovery is denied before its factory/call. Recovery's
child deadline protects the synthesis reserve; 3 seconds is not a new recovery
timeout. With sufficient remaining budget a recovery may take longer than 3 seconds.

Synthesis receives the smaller of its 6-second cap and positive remaining request
time, after required operations complete. Insufficient time can deny admission;
late synthesis is abandoned. A final request acceptance check covers local work
after synthesis. No stage can reset or extend the original deadline.

Late routing prevents policy resolution, authorization, tools and synthesis.
Unavailable recovery produces no invented fallback. Tools execute once; their
actual completion and projected output survive later rejection. Returned model
usage survives rejection, and logical recovery calls are not counted as retries.

These synchronous admission/result-acceptance boundaries do not preempt network
calls. A provider may return after 20 seconds, at which point its result is rejected.
The deadline is neither a guaranteed termination time nor a guarantee that all
future requests complete. Rejection alone does not prove cancellation or token
savings. Requested and observed cancellation remain separate fields.

## Why 20 seconds, not the 7.5-second performance gate

The unchanged 7,500 ms P95 performance gate assesses a workload distribution.
A per-request runtime deadline governs admission and acceptance of one execution.
They answer different questions. Using the performance gate as a hard request
deadline would reject some observed correct responses in rare 13–15 second tails.

The 13C.4E comparison and 13C.4F equivalent-input replay found Candidate L rejected
six of 136 known-successful trajectories at its router cap. Two also exceeded its
request and synthesis thresholds retrospectively. Candidate R accepted all
136/136, including the single recorded recovery. Replay reconstructs some local
timing from stage durations and attempt offsets; it is not a fresh production run.

Existing saved controlled-live evidence was inspected for this promotion:

| Source artifact (gitignored reports/) | Population | Completed | Deadline/stage rejections | Extra retries | Unknown usage attempts |
| --- | --- | ---: | ---: | ---: | ---: |
| `reliability_13c4f_candidate_R.json` | Broad smoke | 16/16 | 0 | 0 | 0 |
| `reliability_13c4g_candidate_R_tail.json` | Selected tail, five repetitions of two scenarios | 10/10 | 0 | 0 | 0 |

Both records confirm Candidate R enforcement. Combined completion is 26/26, with
zero late completions, abandoned results or cancellation claims. That count does
not pool the deliberately selected tail observations into a balanced workload
latency estimate, and runtime completion is not semantic/safety certification.

Neither controlled run exercised recovery. Recovery timing evidence remains the
single earlier observed trajectory plus deterministic admission tests and replay.
No new recovery timeout is inferred from N=1. Retries stay disabled because no
live retry-benefit evidence supports activating them; offline retry tests alone
are insufficient for that decision.

## Telemetry and qualification compatibility

`ProductionExecutionTelemetry.effective_runtime_policy` records the policy name,
all stage/request values, model retry configuration and lower-layer retry
configuration/scope, including requests rejected before a model dispatch.
`deadline_budget_ms` separately records the effective request bound, which may be
shorter than v1 because of the caller's budget. Stage admission, allocated limits,
recovery reserves/denial evidence, completed operation counts and rejected-attempt
usage remain available. Observation failures cannot disable enforcement.

Configured SDK retry suppression is not a measured transport-attempt count.
Per-attempt telemetry continues to mark arbitrary injected runners unverified.
No provider billing or cancellation outcome is inferred from settings alone.

Smoke/full/performance retain their CLI and scoring behavior and now exercise
the production default. Reliability qualification remains explicitly descriptive
without `--enforce-candidate-budget`: its adapter intentionally supplies an
unbounded diagnostic policy. The flag installs the selected candidate policy.
Existing replay, baseline and experimental artifacts are unchanged.

## Validation

Offline validation uses real orchestration with fake clocks and mocked HTTP
transport, plus the existing 28-case fault matrix and evaluation/qualification
runner tests. No new live calls were made for promotion.

One final post-promotion live smoke validation, **not executed**:

```powershell
python scripts/run_agentguard_eval.py --suite smoke
```
