# Milestone 13: production reliability, concurrency and release qualification

Milestone 13 adds observation, bounded qualification and release controls without
turning small experimental samples into production availability or throughput
promises. Implementation/validation is offline first; live qualification is a
separate explicit action.

## Architecture delivered

| Phase | Result |
|---|---|
| 13A | Vendor-neutral request/component/attempt telemetry; known, partial and unavailable usage remain distinct. |
| 13B | Deterministic fault injection and the 28-case safe-failure matrix. |
| 13C | Ingress request budgets, deadline admission/result acceptance, retry ownership and effective policy evidence. Recovery is bounded planning work, not a retry. |
| 13D | Persistent worker owning its thread, event loop and client; bounded admission/queueing, isolation and backpressure qualification. |
| 13E | Production-only consumption baseline, report-only SLO/error budgets, shadow candidates and promotion of 16 deterministic release gates. |

Production v1 remains request **20,000 ms**, router **13,000 ms**, recovery reserve
**3,000 ms**, synthesis **6,000 ms**. AgentGuard retries are disabled (one maximum
attempt, zero shared extras); Agents SDK and OpenAI provider retries are disabled
on the qualified default path. Queueing uses the same ingress budget. No policy,
prompt, model, tool, business dataset or runtime ownership was changed in 13E.4.

The qualified envelope is **workers <=5, queue capacity <=5**. It is a qualification
boundary, not a universal deployment optimum or sustained-capacity SLO.

## Authoritative release controls

`config/release-gates.yaml` owns only the 16 NEW_ENFORCED structural/configuration
gates. `config/quality-gates.yaml` continues owning EXISTING_ENFORCED quality,
semantic, safety, token and sequential performance requirements, without duplicate
thresholds. The old 13E.3 candidate specification remains a historical shadow tool.

New structural violations must be zero: fabrication, authorization, hidden retries,
duplicate required operations, request-ID collisions, mixed results, contaminated
telemetry. Seven configuration comparisons check application/SDK/OpenAI retries
and the four production-v1 budget values. Two more compare the concurrency envelope.

PASS requires actual applicable evidence. A measured/configured violation is FAIL.
Missing, partial, stale or unassociated required evidence is NO_DATA, never zero.
Concurrency is N/A only with explicit `active: false`; absent configuration is
unknown. Active deployments require both configured counts. The sequential
smoke/full CLI explicitly declares its own concurrency inactivity; that declaration
does not describe any separately hosted deployment.

Decision precedence is enforced FAIL, required NO_DATA (INSUFFICIENT_EVIDENCE),
report-only MISS (REVIEW_REQUIRED), then PASS. Exit is nonzero for FAIL or
INSUFFICIENT_EVIDENCE. Statistical misses and insufficiency never fail CI.

## Evidence scope and configuration provenance

The structural qualification source is the existing **offline production transport
harness**, covering 1, 2 and 5 workers, with three requests per worker (24 total),
including recovery. It exercises the real Runner, runtime policy, business tools,
projection and HTTP dispatch path against local deterministic model responses.
Its fabrication detector compares the harness's exact status-response contract;
this is deliberately bounded fixture coverage, not a universal natural-language
fabrication detector and not a replacement for live semantic/safety evaluation.

Resolved SDK retry settings are observed when constructing the actual RunConfig.
The client returned by the installed SDK's `_get_client` adapter is transparently
observed for `max_retries`; the test-only observer forwards the original call
unchanged and is restored during teardown. This pinned private accessor is a
qualification adapter: dependency drift must be revalidated. No client setting is
overridden to make the gate pass. HTTP retry headers and logical attempts are
independently reconciled. These are actual offline execution observations, not
values copied from the declared lower-layer policy snapshot.

Effective per-request policy snapshots and the local default resolver are compared
against the authoritative gate values and checked-in policy. File/runtime drift
cannot be hidden by selecting one source. Required-operation grants, actual calls,
tool outputs, transport identities and telemetry are associated per request. Every
gate observation records its population and denominator. Missing fields/experiments
cannot manufacture zero violations.

Artifacts bind to git commit, a fingerprint of source/config/scripts/tests/CI and
requirements, and installed SDK versions. A changed implementation/dependency set
requires a fresh offline artifact. Historical live reports are not silently used
to qualify the current checkout. Reports include timestamps, model identity,
runtime policy, specification version/hash, source hashes and applicability.

The live correctness run reuses each existing EvaluationRecord exactly once. Its
effective policy and proven deterministic violations supplement the offline proof.
No detected live contradiction is ignored, but missing live grounding/dispatch
coverage is not relabeled complete. Seven structural gates qualify the bounded
offline architecture population; they do **not** claim exhaustive structural
measurement for every live request. Supplemental coverage is explicit. Existing
live semantic, safety and deterministic gates remain separately authoritative.

## Commands and CI

Fresh offline qualification artifact (no external API calls):

```powershell
$env:AGENTGUARD_PRODUCTION_TRANSPORT_REPORT = 'reports/release_13e4/transport.json'
python -m pytest tests -q
Remove-Item Env:AGENTGUARD_PRODUCTION_TRANSPORT_REPORT
```

The destination is exclusively created. Retain an earlier file and choose a new
path/environment value when repeating qualification; do not silently overwrite it.
Transport warnings/timeouts and assertions remain unchanged. Historical probe
intermittency is retained; a clean latest suite does not explain past failures.

Assemble offline evidence without executing any request:

```powershell
python scripts/qualify_release.py --structural-evidence reports/release_13e4/transport.json --deployment reports/release_13e4/sequential.json
```

The explicit sequential deployment descriptor is `{"active": false}`. To assess
a hosted deployment supply its actual activation, `max_workers` and `queue_capacity`.
Omitting it leaves envelope gates NO_DATA. Without a current saved quality result,
the final offline release decision is INSUFFICIENT_EVIDENCE even when structural
checks pass. `--quality-result` accepts an attributed current-revision bundle,
not anonymous numbers or historical console summaries. `--slo` and `--cost` are
optional saved observations; neither triggers evaluations or affects deterministic
enforcement. USD pricing is not required and judge usage never enters production
consumption. Output is exclusively created under gitignored `reports/release_13e4/`.

Optional **live** production-default smoke qualification, run separately
from the offline 13E.4 implementation validation:

```powershell
python scripts/run_agentguard_eval.py --suite smoke --release-qualification --structural-evidence reports/release_13e4/transport.json
```

CI retains its offline test step, which now writes the attributed transport
artifact, and its existing single smoke evaluation with `--release-qualification`.
The current workflow runs that live step only through explicit manual opt-in;
standard pull-request validation is offline. The bundle evaluates the 16 promoted gates and existing
quality result together and prints the exact failing/unavailable gate. GitHub
Actions retains the generated artifacts even on failure. Existing early runtime
errors still fail immediately. Sequential performance remains its existing
separate qualification path; correctness mode still explicitly defers latency.
No statistical SLO analysis command was made enforcing or added as a CI blocker.

## Evidence limits and remaining work

Normal consumption (25 comparable requests) was mean 852.16 tokens and P95/max 882;
successful recovery N=1 used 1,644 tokens. Pricing remains TOKEN_ONLY. Production
reliability 99%, concurrent/service P95 10s, queue P95 6s and normal-token marker
1,000 remain provisional/report-only. Recovery targets, capacity rejection rate,
sustained throughput and statistical cost/SLO metrics are not enforced.

Representative independent production windows, workload diversity, failure and
recovery coverage, complete component usage, sustained demand measurements and
business risk acceptance are needed before promoting those statistical objectives.
No amount of synthetic qualification alone supplies that evidence. Offline
implementation completion does not mean a production release is qualified: missing
current quality/safety observations or deployment evidence still blocks PASS.
