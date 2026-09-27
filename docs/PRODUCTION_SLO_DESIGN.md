# Production SLIs, candidate SLOs and error budgets — 13E.2

This milestone is **report-only**. It adds no gates, deadlines, retries, worker
settings, CI conditions, alerts or automatic actions. Existing release checks in
`config/quality-gates.yaml` and the production policy remain unchanged.

The executable specification is [slo-report-only.yaml](../config/slo-report-only.yaml).
Every entry has a definition, denominator, classification, population, aggregation,
target (possibly unavailable), window, comparison, sample screen, evidence strength,
status, enforcement and rationale. YAML defaults expand into full entries in the
generated report. All entries use `enforcement: REPORT_ONLY`. The schema vocabulary
recognizes `ENFORCED`, but the 13E.2 loader/evaluator rejects activation.

```powershell
.venv/Scripts/python.exe scripts/analyze_slo_baseline.py
```

Output: `reports/slo_13e2/scorecard.json`, gitignored. The command reads existing
13D.5, 13E.1, 13D.4G and performance reports; it cannot execute the production path.
`--telemetry` can supply one saved production telemetry JSON. `--output` chooses a
new artifact; overwriting historical reports is refused. Missing evidence is
reported. No judge tokens, USD pricing or pricing API is required.

## SLI taxonomy and classifications

| Domain | SLIs | Classification and reason |
|---|---|---|
| Correctness | Functional, tool, argument, authoritative grounding accuracy | Statistical SLOs: contract quality may fail without constituting unauthorized execution. New production targets require representative contracts. |
| Safety | Injection resistance, unsupported-action protection, data protection, tool-policy pass rate | Statistical SLIs on applicable checks; do not convert every evaluation failure into a proven safety violation. Existing release gates still apply unchanged. |
| Safety | Fabrication, authorization violations | Hard invariants: existing prohibited outcomes have no allowed error budget. |
| Reliability | Correct request success, provider failure, rate limit, deadline rejection, cancellation, late/abandoned, unknown failure | Statistical SLOs/attribution rates. Runtime completion alone is report-only and does not imply business correctness. |
| Reliability | Hidden inference retries | Hard invariant under the existing retry-disabled contract. |
| Performance | Service, ingress-to-terminal and queue P95 | Provisional statistical SLOs. Router/synthesis/recovery latency remains report-only attribution. |
| Capacity | Admission ratio, capacity rejection, active workers, queue depth, saturation duration fractions | Operational guardrails. Only the already qualified five-worker envelope has a numeric comparison. Offered/admitted/started/completed counters and throughput are report-only. |
| Consumption | Normal mean/P95 tokens, normal logical calls, usage completeness | Report-only operational guardrails, not new release gates. Recovery rate, adjusted consumption and tokens/sec are descriptive. |
| Recovery | Recovery success, stage latency, incremental tokens | Report-only; recovery is valid work, not intrinsically an error or retry. |
| Isolation | Request-ID collisions, mixed tool results, duplicate required operations, contaminated telemetry | Hard invariants: request isolation and exactly-once required execution cannot be averaged away. |

`QUALIFIED` on an invariant records an existing requirement and bounded supporting
tests; it does not assert universal detection of every semantic violation. We do
not label new statistical targets QUALIFIED. Known invariant violations produce
MISS even if other requests lack evidence. Zero observed violations with missing
coverage produces INSUFFICIENT_DATA. Concrete natural-language fabrication detection
is limited by the retained deterministic checks; unassessed classifiers remain unknown.

## Request and denominator semantics

An **offer** is one unique ingress request, including admission rejections.
**Admitted** means accepted into a reserved worker slot or the bounded queue.
**Started** means a worker took the request; it does not imply inference dispatch.
**Completed** means a terminal record exists, including errors, cancellations and
deadline rejections. Business success is a separate condition.

Three primary outcomes are:

1. **SUCCESS:** admitted, terminal runtime success and applicable business contract
   passed. Runtime-only telemetry cannot prove this without contract evidence.
2. **EXPECTED_CAPACITY_REJECTION:** explicitly rejected with `capacity_exhausted`.
   It is outside the admitted execution denominator and independently visible in
   offered-traffic admission/rejection metrics.
3. **UNEXPECTED_FAILURE:** admitted terminal failure or a completed but incorrect
   business outcome. Provider failures, 429s, queued expiry, deadline rejection,
   cancellations and unknown failures consume the admitted success budget.

Pending or unassessed requests are neither silently successful nor discarded.
They retain a denominator position and block a complete statistical assessment.
Missing start/completion evidence is unavailable, not zero. Other admission errors
are reported separately from expected capacity exhaustion and need an ingress
availability policy before a user-wide availability claim can be made.

An admitted-only cost or telemetry subset is not a complete ingress census. Such
panels leave offered counts and admission/rejection ratios unavailable rather than
claiming a 100% admission ratio from selection of accepted requests.

```
execution success = successful admitted requests / all admitted mature requests
execution failure = unexpected failures / all admitted mature requests
admission ratio   = admitted / offered
capacity rejection rate = capacity_exhausted / offered
```

Per-policy rates use applicable admitted checks. An explicit skipped/None policy
result is not applicable. An absent assessment is unknown. Independent reliability
flags can overlap: an abandoned request may also be a deadline failure. Compute
the error budget once per unique unsuccessful admitted request, never by summing
overlapping component/failure counters. Until caller-initiated cancellation can
be identified reliably, cancellations remain in the failure denominator; no
intent is guessed from an exception.

Future production design uses rolling 28-day mature-request cohorts, stratified
by stable model/prompt/policy and workload. Short 5-minute/1-hour views would aid
operations, but existing finite reports do not contain those production windows.
Current evaluation uses `qualification_batch`; mismatched windows are insufficient
data rather than silently treated as a rolling month. Cost/concurrency panels
overlap in identities and must not be summed into unique traffic.

## Three different latency concepts

| Concept | Value | Interpretation |
|---|---:|---|
| Existing sequential performance qualification | P95 <= 7,500 ms | Existing repeated sequential gate; unchanged. |
| Existing request deadline | 20,000 ms | Absolute ingress-created budget including queue wait; unchanged. No promise about remote cancellation or client network delivery. |
| Candidate concurrent ingress-to-terminal SLO | P95 <= 10,000 ms | Report-only PROVISIONAL exploration, including queued terminal failures. |

Service starts at worker pickup and ends at terminal completion. Queue wait runs
from ingress to service start. End-to-end includes both. Successful requests alone
must not define a flattering latency population. Production-only `total_latency_ms`
lacking hosting timestamps is not relabeled ingress latency. The terminal boundary
is hosting completion, not browser delivery. Capacity rejections require their own
ingress-response timing if an offered-traffic latency promise is desired.

| Candidate | Target | Informational warning / critical bands | Rationale |
|---|---:|---:|---|
| End-to-end P95 | 10s | >10s / >15s | Round above the observed stage P95 maximum 9.316s; 15s leaves 5s before the hard deadline. |
| Service P95 | 10s | >10s / >13s | Round above observed 9.313s; a 13s service investigation band plus 6s queue allowance approaches 20s. |
| Queue P95 | 6s | >6s / >10s | Round above 5.532s P95 and 5.609s maximum; 10s waiting consumes half the request budget. |

These are coarse design hypotheses, not statistically estimated safety margins.
Quantiles do not add into a joint guarantee. No alert is emitted. Router 13s,
synthesis 6s and recovery reserve 3s remain runtime-policy references, not new
percentile targets. Candidate percentiles require at least 100 comparable samples
for screening (about five upper-tail observations), plus representative independent
windows before any qualification claim. Current per-stage N=5/10/10 is insufficient.

## Reliability candidates and mathematical error budgets

The initial report-only success hypothesis is 99.0%, because it permits explicit
discussion of one unsuccessful admitted request per 100. It is **not** a final
availability commitment. Neither 25/25 success nor an industry convention selects
the acceptable business failure rate. 99.5% and 99.9% remain alternative budget
models for stakeholder review. Fault types, customer consequences and capacity
rejections must inform that choice.

| Admitted requests | 99.0% allowed failures | 99.5% | 99.9% |
|---:|---:|---:|---:|
| 1,000 | 10 | 5 | 1 |
| 10,000 | 100 | 50 | 10 |
| 100,000 | 1,000 | 500 | 100 |
| 1,000,000 | 10,000 | 5,000 | 1,000 |

The module uses decimal arithmetic: `allowance = admitted * (1-objective)`.
Allowed whole failures use floor, not rounding up. Remaining budget may be
negative. Burn ratio is observed failures divided by the exact allowance; it is
a same-window arithmetic ratio, not a time-based multi-window alert. Empty cohorts
have no measured failure rate. Hard invariants never gain a statistical allowance.

With zero failures in independent representative trials, a one-sided 95% binomial
bound would need approximately 299 / 598 / 2,995 trials to substantiate 99.0% /
99.5% / 99.9%, respectively. The 300-sample screen for the initial hypothesis is
motivated by that first bound; it is not sufficient when samples are correlated,
narrow or selected. Current bursts do not qualify any of these objectives.

## Capacity, recovery and consumption

Stage 3 admitted 10 of 15 offers and explicitly rejected five. Its 33.3% rejection
rate demonstrates bounded admission under intentional overload, not an acceptable
production target. Five workers is the maximum live-qualified envelope. Queue peaks
of five prove occupancy was reached, not how long it remained full. Queue-full time
fraction needs time-weighted queue samples (not applicable for zero-capacity queues).
Worker saturation needs busy-time samples relative to configured workers. No rejection,
throughput or saturation-duration objective is selected from these finite bursts.

Recovery invocation rate is requests invoking recovery / admitted requests.
Recovery success is correctly completed recovery-invoking requests / all invoked
recoveries; returned bindings alone do not establish success. Current successful
recovery N=1 used 1,644 tokens and three calls. Its difference from the current
normal mean, 791.84 tokens, is descriptive and cross-scenario. Measured recovery
stage latency is attributable stage work, not a causal estimate of extra total
latency. No recovery-rate, success, latency or token gate is proposed.

Before recovery gating, collect at least 100 representative recoveries to screen
tail behavior across intents, ambiguity/control patterns and worker/queue profiles,
with successes and failures retained. Reliability claims need the larger goal-
dependent sample screens above plus independent windows. Matched normal/recovery
comparisons and complete component usage are needed for incremental-cost claims.
These are future evidence proposals, not authorization for live collection.

The normal token baseline is mean 852.16 and P95/max 882, N=25. A **provisional
1,000-token** normal mean/P95 investigation marker is a coarse round-unit boundary
above this narrow observed range. It is not a chosen percentage margin or release
budget. It requires at least 100 comparable normal samples for screening. Recovery
requests are excluded. Normal call count two is an architectural guardrail; a
non-recovery request with three calls is not silently filtered out of its denominator.
Legitimate recovery normally has three calls and remains separate.

Recovery-adjusted consumption reports the residual from
`852.16 + observed_recovery_indicator * 791.84`, with no threshold. That model inherits
the N=1 limitation. Partial/unknown usage prevents token assessments; it is not zero.
Usage completeness and tokens/sec remain operational observations without arbitrary
100% or spend-rate gates. Judge usage and USD pricing never enter these SLIs.

## Evidence strength and report outcomes

| Evidence area | Strength for the proposed objective | Limitation |
|---|---|---|
| Existing isolation/authorization/retry invariants | MODERATE | Strong deterministic contracts and offline tests plus bounded live checks; limited production diversity and semantic detection coverage. |
| Five-worker envelope and explicit backpressure | MODERATE | Direct bounded live measurements; no sustained capacity claim. |
| Normal two-call path | MODERATE | Direct architecture and 25 current observations; do not generalize to recovery/failures. |
| Success, concurrency latency, normal token targets | WEAK | Only 25 comparable admitted burst requests; variance and cohort diversity unresolved. |
| Recovery objectives, saturation duration, production rejection target | INSUFFICIENT | N=1 recovery and missing time series/demand envelope. |
| New production-qualified statistical SLO | No STRONG evidence yet | More samples alone cannot cure selection bias or missing telemetry. |

Scorecard outcomes are PASS, MISS, INSUFFICIENT_DATA and NOT_APPLICABLE. Small samples
can retain `observed_comparison=true` while the assessment remains INSUFFICIENT_DATA.
A descriptive metric with a value but no numeric objective is NOT_APPLICABLE for
target comparison, with an explicit reason. Missing data remains INSUFFICIENT_DATA.
PASS is only a batch comparison; provisional status and evidence strength remain
visible. A MISS never fails CI: the analysis command returns zero after successfully
writing a report. Malformed input/configuration and output-path conflicts are real
tool errors, not SLO misses, and can exit nonzero.

## 13E.3 readiness and validation

Existing structural invariants and the five-worker qualification envelope are
ready for **candidate gate design review**, not automatic activation. Two-call
normal-path checks can be considered with explicit recovery/failure applicability.
New statistical success, latency, token, recovery, saturation-duration and admission
targets need representative temporal cohorts, business risk acceptance, cancellation
attribution, policy applicability and complete production telemetry first.

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m pytest tests/faults/test_resilience.py::test_fault_matrix -q
```

Tests use synthetic local evidence only. Production code, quality-gate configuration,
models, prompts, datasets, deadlines, retries and worker ownership are untouched.
