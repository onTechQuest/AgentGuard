# Candidate release gates and operational scorecard — 13E.3

This milestone evaluates **shadow candidates only**. It never changes the release
decision used by CI, never activates an alert and never executes an agent or judge.
The existing runner, quality YAML, runtime policy, retries and worker ownership are
unchanged. Candidate `FAIL` is an advisory release-readiness decision, not a process
exit code or a new gate in GitHub Actions.

## Readiness matrix

| Readiness class | Meaning | Scope |
|---|---|---|
| EXISTING_ENFORCED | Requirement already applied by the existing qualification path | Existing functional/tool/argument, semantic, safety, failed-scenario, token and sequential performance gates |
| CANDIDATE_ENFORCE_READY | Deterministic candidate suitable for promotion review when its evidence is complete | Seven structural invariants, retry/budget configuration drift, qualified worker/queue boundary |
| REPORT_ONLY | Observation or provisional target, not a candidate enforced gate | 99% success, 10s load/service P95, 6s queue P95, 1,000 normal-token marker, normal call count |
| INSUFFICIENT_EVIDENCE | Missing required coverage or insufficient basis for an objective | Unknown deployment settings, missing existing results, recovery objectives, sustained capacity/rejection objectives |
| NOT_APPLICABLE | Explicitly inapplicable to the declared deployment or population | Concurrency explicitly inactive; policy/population skipped |

Readiness and observed status are separate. A candidate violation remains a
candidate-design item with `WOULD_FAIL`; it is not ready to release. Missing
required evidence is `NO_DATA`, never `WOULD_PASS`. Statistical PASS cannot promote
a metric. Evidence strength and the original provisional SLO status remain visible.

## Existing gates remain authoritative

`config/release-gates-candidate.yaml` references `config/quality-gates.yaml` instead
of duplicating its thresholds. The scorecard accepts saved `QualityGateResult`
JSON (`checks` with metric, actual, threshold, comparison, passed and optional
enforced). It retains existing PASS/FAIL decisions, including failure when a
configured metric is unavailable. A threshold mismatch is insufficient evidence,
not a recomputed PASS against stale configuration.

The correctness runner explicitly defers latency. The separate saved sequential
performance qualification can supply its existing verdict. Sequential P95 <=7.5s,
the ingress-created 20s request deadline and provisional concurrency/load targets
remain distinct. The existing 1,500 average-token gate is likewise distinct from
the report-only 1,000 normal-token investigation marker.

No retained quality result means unknown quality, not reconstructed success from
25 functional load observations. In particular those observations contain no
semantic safety judges. This tool does not run judges to fill the gap.

## Candidate specification

Every candidate has an expected value, comparison, section, evidence source and
specific promotion condition. Shared promotion conditions are listed in YAML and
included in the output. All candidates are SHADOW; the loader rejects activation
and rejects statistical metrics added as candidate gates.

| Candidate | Shadow comparison | Promotion-specific evidence |
|---|---|---|
| Fabrication violations | =0 | Deterministic contradiction detection with documented coverage; unknown grounding is not zero fabrication |
| Authorization violations | =0 | Allowed operation scope associated with actual tool execution |
| Hidden inference retries | =0 | Logical attempt and HTTP dispatch reconciliation, covering lower layers |
| Duplicate required operations | =0 | Per-request tool/argument operation identity |
| Request-ID collisions | =0 | Complete ingress/terminal census |
| Mixed tool results | =0 | Target and authoritative output association |
| Telemetry contamination | =0 | Component and dispatch request identity |
| AgentGuard retry configuration | disabled, max attempts 1, shared extras 0 | Checked-in and effective deployment configuration, with override applicability |
| Agents SDK retries | max retries 0 | Effective pinned SDK configuration and transport regression evidence |
| OpenAI retries | max retries 0 | Effective client configuration and transport regression evidence |
| Reliability budgets | request 20,000; router 13,000; recovery reserve 3,000; synthesis 6,000 ms | Effective policy snapshot and existing admission/reserve semantics |
| Worker envelope | max workers <=5 | Explicit deployment activation and configured capacity |
| Queue envelope | capacity <=5 | Configured capacity and ingress budget preserved while waiting |

These are 16 candidate metrics (four separate budget checks). They represent
existing architectural requirements and qualification boundaries as new *shadow
comparisons*, without duplicating existing statistical quality gates.

Configuration evidence is not runtime suppression evidence. A saved policy's
`lower_layer_retry_policy` declaration is assessed as a declaration only; the
hidden-retry measurement supplies a separate runtime check. Checked-in policy
JSON does not declare SDK/OpenAI settings, so those comparisons remain NO_DATA
for that source. The tool does not manufacture declarations by importing runtime
defaults or inferring them from AgentGuard's retry flag.

The five-worker/five-queue envelope is a demonstrated qualification boundary,
not a sustained throughput target or universal optimum. An explicit
`{"active": false}` deployment yields N/A for both envelope checks. An omitted
deployment file is unknown. Historical active stages do not establish activation
in a current deployment; source populations remain separate.

## Advisory decision and required scope

Decision precedence is:

1. Existing enforced failure or observed candidate invariant violation: **FAIL**.
2. Report-only SLO MISS: **REVIEW_REQUIRED**, never FAIL by itself.
3. Missing required existing/candidate evidence: **INSUFFICIENT_EVIDENCE**.
4. Otherwise: **PASS**, with optional insufficient SLO evidence annotated.

Rows always retain their missing-evidence reasons even when a higher-precedence
decision applies. Explicit N/A is excluded from required evidence. Known candidate
violations remain visible despite partial coverage; zero with partial/weak
coverage cannot qualify the candidate. Shadow labels are WOULD_PASS, WOULD_FAIL,
NO_DATA; N/A rows use NO_DATA with an explicit inapplicability reason.

The eight sections are QUALITY, SAFETY, RELIABILITY, CONCURRENCY / ISOLATION,
CONFIGURATION, PERFORMANCE, CONSUMPTION and SLO OBSERVATION. Each row includes
metric, expected, actual, status, readiness class, evidence strength, source
population and reason. Only qualified concurrency populations supply the seven
observed invariant candidates; unrelated cost-only panels cannot prove them.
Other SLO rows retain their 13E.2 evidence and population. No panel denominators
are pooled and no extra evaluation tokens or USD data are consumed.

## Offline invocation and evidence

```powershell
.venv/Scripts/python.exe scripts/analyze_release_readiness.py
```

Default output: `reports/release_13e3/scorecard.json` (gitignored). Existing output
files are protected by exclusive creation; choose a new `--output` for another
analysis. It reads the final 13E.2 scorecard, 13D.5 qualification, current declared
policy and saved sequential performance qualification. `--quality-result` and
`--deployment` supply optional retained evidence. Deployment input is:

```json
{"active": true, "max_workers": 5, "queue_capacity": 5}
```

File paths, SHA-256 hashes and the saved qualification revision are retained for
attribution. Saved evidence is not automatically current-release qualification;
promotion review must establish revision relevance. Output contains selected
metrics/configuration fields, not prompts, credentials or request payloads.

The CLI returns **0** after writing any valid scorecard, including advisory FAIL.
Invalid configuration/input or an existing output path is a real tool error and
can exit nonzero. The CLI is not wired into CI and cannot change CI's release
decision. Tests assert expected shadow failures; they do not fail merely because
a historical candidate observation misses its target.

## Promotion and evidence caveats

Before 13E.4 activation, every candidate needs deterministic measurement, clear
denominator/applicability, a qualified versioned evidence source, regression
coverage, stable collection, known missing/failure semantics and explicit approval.
Avoid stochastic judges for structural invariants. The per-candidate conditions
above are additional requirements, not waived by a current WOULD_PASS.

Recommend these 16 deterministic candidates for **13E.4 promotion review**, not
unconditional activation. Current deployment and lower-layer effective settings
must be supplied before a deployment can claim complete candidate coverage.

Do not promote 99% success, 10s load/service P95, 6s queue P95, the 1,000-token
marker, recovery rate/latency/tokens, rejection rate or sustained throughput.
They require representative independent production windows and business risk
acceptance. Recovery N=1 is descriptive only. Normal two-call observations remain
report-only pending failure/recovery applicability review.

Earlier full-suite runs occasionally showed transport-probe intermittency. The
latest authoritative 13E.2 run was clean at **1,958 PASS**, with the fault matrix
**28/28**. Neither observation erases the other. Root cause was not established;
no warning suppression, threshold change or new transport gate is justified by
this milestone. The 13E.3 validation log is retained separately.

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m pytest tests/faults/test_resilience.py::test_fault_matrix -q
```
