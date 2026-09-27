# Production consumption baseline — Milestone 13E.1

This is an offline reporting tool. It never imports the support runtime, creates
a provider, evaluates a judge, changes a production limit or participates in a
release gate. No SLO or budget threshold is defined.

```powershell
.venv/Scripts/python.exe scripts/analyze_cost_baseline.py
```

The default artifact is `reports/cost_13e1/baseline.json`, under gitignored
`reports/`. Existing artifacts are not overwritten; use `--output` with a new
filename for another analysis. `--reports-root` selects another saved-report
directory. Missing reports produce explicit evidence gaps, not network calls.

## Evidence and accounting boundary

`src/agentguard/cost_evidence.py` adapts original smoke/token audits, performance
reports, reliability observations, concurrency campaigns and single-request
diagnostics. `scripts/analyze_cost_baseline.py` contains the curated original
source manifest. Other JSON files are inventoried with exclusion reasons;
derived comparisons, replay reports, nested collector copies, local transport
experiments and aggregate-only outputs are not fresh production requests.

Adapters select production counters explicitly. `evaluation_usage` and
`evaluator_usage` are retained separately and never enter request/component
tokens or pricing. Inputs, final responses, business payloads, exception text,
headers and credentials are not copied into the cost report. Records retain
source, canonical-document SHA256, request/campaign identity, population, model
when observed, and usage counters. No model identifier is inferred from today's
configuration for a historical request.

Duplicate documents and request identities are excluded. Conflicting copies of
one identity invalidate that identity rather than silently choosing a total.
Older records without request IDs use campaign timestamp, scenario and repetition
(plus observation timestamp for audits). These are weaker identifiers; independent
campaigns are not deduplicated merely because their token totals happen to match.

Incomplete reports, unreconciled audit usage, controlled/synthetic reliability
observations and stopped concurrency stages are excluded explicitly. A reliability
`recovery_probe` flag identifies a selected population; it does **not** establish
that recovery executed. Those production observations remain a separate population.
Business failures can still have valid consumption evidence; they remain historical
observations but cannot enter the current successful-workload baseline.

## Consumption model

`src/agentguard/cost_model.py` supplies typed request/component observations,
archetype classification, aggregation, pricing and projection functions.

| Archetype | Evidence |
|---|---|
| NORMAL_2_CALL | Completed request, one router and one synthesis call |
| RECOVERY_3_CALL | Completed request, one router, recovery and synthesis call |
| EARLY_FAILURE | Failure with observed stages and no synthesis call |
| ADMISSION_REJECTED | Explicit capacity rejection with zero-work evidence |
| DEADLINE_BEFORE_SERVICE | Explicit expiry and verified zero dispatch/tool work |
| DEADLINE_AFTER_DISPATCH | Deadline termination after dispatch evidence |
| OTHER_OBSERVED / UNCLASSIFIED | Insufficient or different call topology; never forced into a normal archetype |

Counts are not interchangeable: logical model calls, SDK responses and transport
dispatches are separate observations. For older single-response calls, response
counts support call counts but do not prove HTTP counts. Collector-only diagnostics
with exactly three calls and three observed model stages support one call per
stage; they do not supply component token splits.

Each population has per-archetype counts, call/dispatch statistics, input/output/
total token sum, mean, P50, P90, P95 and maximum. Percentiles use linear interpolation.
Each metric includes its available sample count; unavailable values are never
zero. Complete totals without input/output separation remain useful token evidence.
Partial usage is reported separately as known lower bounds and excluded from
complete distributions and projections. Component totals are a breakdown, never
added again to request totals. Component statistics describe each component's
per-request observation; older multi-turn component totals are not invented
per-call samples.

## Current baseline and projections

Historical architecture epochs, selected tails and reliability populations remain
separate. The current normal baseline selects complete, business-passing
`NORMAL_2_CALL` requests from 13D.5. Current recovery selects the successful
13D.4G diagnostic. Current normal evidence is N=25; current recovery is N=1.

The recovery comparison is a descriptive difference between different scenario
populations. It is not a causal estimate of adding recovery to the same request,
and no statistical precision is claimed for N=1. Historical recovery component
observations remain available in their own groups; they do not fill missing
component tokens in the current recovery sample.

For an assumed recovery fraction `r` and request count `N`:

```
expected tokens = N * ((1-r) * normal_mean + r * recovery_mean)
logical calls   = N * (2+r)
recovery calls  = N * r
```

Reports model 0%, 1%, 5%, 10%, 25% recovery and scale normal/5%/10% workloads to
1K, 10K, 100K and 1M requests. These are scenarios, not traffic forecasts. A missing
recovery input/output split leaves every positive-recovery split unavailable.
Zero-recovery projections preserve the observed normal split.

13D.5 velocity uses first ingress to last worker completion, excluding startup,
shutdown and scoring. Tokens/sec and calls/sec measure finite-burst consumption;
they do not establish sustained capacity. Concurrency changes consumption velocity,
not inherently tokens/request. Stage 3's rejected requests have zero observed
inference/tool work. Hypothetical additional work uses Stage 3's admitted mean and
is labeled an avoided-work estimate, never actual financial savings.

## Optional verified pricing

Without configured verified pricing, the mode is `TOKEN_ONLY` and USD values are
`null`, with an availability reason. There are no built-in prices or network
price lookups. Token projections work without pricing.

To supply independently verified pricing, pass `--pricing path/to/pricing.json`.
Required JSON keys are:

| Key | Meaning |
|---|---|
| model | Exact observed model identifier |
| effective_date | ISO date of the quoted rate |
| source | Nonempty reference/source text |
| input_usd_per_million | Verified input rate, or null if unavailable |
| output_usd_per_million | Verified output rate, or null if unavailable |
| verified | Explicit boolean confirming external verification |
| cached_input_usd_per_million | Optional verified cache rate |

No placeholder rate file is provided. Missing rates or `verified=false` retain
TOKEN_ONLY mode. Invalid metadata/rates raise a configuration error. Verification
is caller attestation; the offline tool cannot authenticate a price source.
The effective date is retained provenance, not automatic historical repricing.

When available, cost is `(input * input_rate + output * output_rate) / 1M`.
A configured cache rate requires a measured cached-input count and discounts
only that count. No cache saving is guessed. Model mismatch, unknown model,
partial usage or a missing split produces unavailable USD. The configured model
never overwrites evidence. Request, component, recovery-difference and scale USD
calculations use the same rules. Without a cache rate, the configured input rate
applies uniformly; it is not a claim about provider discounts or invoiced spend.

## Validation and next evidence

```powershell
.venv/Scripts/python.exe -m pytest tests -q
```

Offline tests cover adapters, source separation, deduplication, incomplete usage,
zero-work evidence, arithmetic, percentiles, mixtures, scale, velocity, backpressure
and synthetic pricing arithmetic. Synthetic unit-test prices are not provider quotes.

13E.2 needs repeated representative recovery observations with resolved model and
component input/output/cache tokens, comparable normal requests, complete partial-
failure consumption evidence, and verified pricing if USD reporting is desired.
Any future live collection needs separate authorization. No new release gates,
CI failures or production enforcement are introduced by this module.
