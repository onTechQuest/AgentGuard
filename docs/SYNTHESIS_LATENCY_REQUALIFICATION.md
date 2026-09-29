# Milestone 14A-R2: synthesis latency tail requalification

This is an offline analysis extension to `audit_latency.py` and
`performance.distribution`, not a new request executor. No production model,
prompt, tool, dataset, retry setting, policy, or gate changes are introduced.
The request deadline remains 20s, router allowance 13s, recovery reserve 3s,
and synthesis allowance 6s. No live campaign was run during implementation.

## Prepared campaign and commands

Eight functional smoke scenarios, five sequential dataset-order passes, N=40.
Keep failures, outliers, and early runs; no warmup exclusion, replacements,
application reruns, or judge calls. This matches the Milestone 12 performance
population/protocol, but historical configuration equivalence is not assumed.

Prepare the plan and inspect retained historical evidence without executing:

```powershell
.venv/Scripts/python.exe scripts/analyze_synthesis_latency.py --prepare
```

The single later live command (do not run as part of implementation):

```powershell
.venv/Scripts/python.exe scripts/audit_latency.py --repetitions 5 --qualify --output reports/latency_14ar2/current_raw.json
```

The existing audit refuses to overwrite the raw report. It retains all 40
executions even if stage acceptance fails. Its existing performance gate may
return failure; that does not prevent subsequent offline analysis.

Analyze afterward, with no SDK/runtime imports or model calls:

```powershell
.venv/Scripts/python.exe scripts/analyze_synthesis_latency.py --input reports/latency_14ar2/current_raw.json
```

Generated `campaign.json`, `comparison.json`, and `policy_replay.json` live under
the already gitignored `reports/latency_14ar2/`. The prepared campaign has zero
observations and explicitly says `PLANNED_NOT_EXECUTED`. Analysis projects the
raw audit into a measurement-only report. It does not alter its input, lineage
manifest, historical reports, production policy, or release gates.

## Measurement and interpretation

Capture request/scenario/run identity, router/recovery/required-operation/synthesis
durations, total request duration, remaining budget before synthesis, configured
and allocated synthesis allowance, late/abandoned result flags, overall deadline
state, failure component/category, retries, production tokens, usage completeness,
and observed model identities. Unknown values stay null. No failed duration is
silently converted into a successful provider completion.

Use existing nearest-rank percentiles. Router, synthesis and total distributions
retain mean, P50, P90, P95 and max independently. P99 is suppressed below 100
observations (and omitted as an estimate for total requests). At N=40 neither
P95 nor tail counts establish statistical certainty. Per-stage sample counts can
be smaller than N because a request may fail before synthesis. Missing counts
are explicit. Observed failed durations may be censored; their distribution is
not an estimate of the uncensored provider completion distribution.

Exceedance counts use strict `>` at synthesis 6/8/10/12s and total 7.5/10/15/20s.
Replay uses `>=` for deadline exhaustion, matching runtime acceptance semantics.

Failure classification gives overall exhaustion precedence, then a measured
stage-cap rejection with overall budget remaining, then provider failure, then
observed retry amplification, otherwise UNKNOWN. Retry amplification is evidence
of extra attempts, not proof retries caused the latency. A completed observation
has no failure class. The supplied missing_order_001 observation is separately
classified STAGE_ALLOWANCE_EXCEEDED; it is not inserted into the N=40 population.
Its missing remaining-budget measurement is not reconstructed from total minus
synthesis.

## Counterfactual replay

S6, S8, S10 and S12 apply 6/8/10/12s synthesis ceilings within the unchanged 20s
request deadline; REMAINING uses only the measured remaining request budget.
These are timing-only, fixed-trajectory replays of observed returned results,
including late completions abandoned by the current cap. They do not re-execute
anything, forecast provider behavior, or certify response correctness/safety.

Unknown remaining budget, unknown/nonzero retries, lower-layer retry uncertainty,
provider errors, censored timeouts, incomplete telemetry, and failures before
synthesis are not replayable. An unobserved future completion is never invented.
Every candidate reports accepted, stage-rejected, overall-rejected and late-result
counts, unknown count, acceptance rate, and maximum accepted total latency.
Overall rejection takes precedence to avoid double-counting. Whole-population
acceptance rate is null if any request is not replayable; a separately labeled
evaluable-only rate is available with its counts.

Measured span durations include instrumentation/admission overhead around the
child-budget boundary. Abandoned results may omit later local processing.
Decisions near a boundary need that uncertainty considered; raising a cap is not
guaranteed to reproduce the measured trajectory or complete before the deadline.

## Historical comparison

Default input covers retained Milestone 12 N=40 performance and legacy audit
reports, plus Milestone 13 baseline, repeated, candidate, and tail populations.
Each artifact, functional/safety population, source, and candidate remains
separate. No historical measurement is reconstructed. Legacy `agent` timings
map to synthesis only when retained metadata explicitly shows a tool-free,
single-response invocation; tool-loop agent durations are excluded.

The retained N=40 performance report has synthesis P95 about 1.910s and max
2.688s. The inspected Milestone 13 populations contain a largest observed
synthesis duration about 5.210s (a separate safety population). No >6s synthesis
completion was found in these inspected sources. That does not prove such tails
were absent. Current N=40 data does not yet exist, so there is insufficient
evidence to call the 11.439s observation a new distribution or a regression.
The generated comparison includes independent distributions, missing counts,
known largest observations, and explicit descriptive-only comparability limits.

## Architectural alternatives

| Policy | Predictability and user experience | Deadline, provider tails, and cost |
| --- | --- | --- |
| Keep fixed 6s | Stable stage acceptance bound; can abandon legitimate late answers | Overall 20s still applies. Local rejection does not undo provider work or billing. |
| Raise fixed ceiling using campaign evidence | May reduce unnecessary abandonment; accepts slower answers and a wider latency range | Must remain bounded by the overall deadline. A larger ceiling alone does not resolve a provider tail or establish a safe percentile. |
| Dynamic `min(ceiling, remaining)` | Explicit stage ceiling with graceful reduction when upstream work is slow | Keeps overall budget authoritative. Requires clear distinction between stage-cap and overall exhaustion. Current child-budget construction already applies a ceiling within remaining budget. |
| Remaining-budget-only final stage | Uses spare budget for a legitimate answer; larger response-time variability | No downstream model reserve is required after synthesis, but local final acceptance still consumes time. The 20s acceptance boundary must remain authoritative. |

Synthesis is the final inference stage: router/recovery reservations protect
required downstream work, whereas a final-stage ceiling primarily expresses a
latency/abandonment preference. These are different architectural purposes.
None of these alternatives introduces retries or promises provider cancellation.
Late-result rejection is an acceptance decision, not a guarantee the process
returns to the user within 20s. Cost already incurred by a late provider response
cannot be recovered by abandoning it. Keep failure semantics distinct from
provider failure and legitimate business refusal. Do not select a new number
from the single 11.44s observation; inspect the campaign and censored/missing
evidence first, then make a separate explicit policy decision.

## Observability validation

The audit already retains full request snapshots. The 14A-R allowlist lacked
remaining budget and configured/allocated stage caps even though runtime
telemetry had them. An optional `stage_budgets` projection now retains those
existing fields plus stage admission, result acceptance, late and abandonment
flags for router/recovery/synthesis. Old v1 result envelopes still validate.
No runtime telemetry instrumentation changes were needed. Missing snapshots
remain an explicit gap; they cannot support cap replay. Provider queue/network
subtimings and unreturned results remain unknowable from local observations.

Offline validation includes classification, percentile conventions, candidate
math, equality boundaries, remaining budget, late results, zero retries, missing
telemetry, population separation, preservation of inputs/policy, and an analysis
subprocess that fails if runtime/SDK modules are imported.
