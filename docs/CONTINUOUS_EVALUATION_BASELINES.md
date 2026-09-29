# Explicit baselines and advisory comparisons (14B.2)

A baseline is reviewed evidence, not the latest passing run. Promotion is an
explicit local operation. It neither executes evaluation nor commits to Git.
No baseline is created by the continuous evaluation command.

## Promotion

```powershell
python scripts/promote_evaluation_baseline.py --run-id <exact-run-id> --suite smoke --reason "Reviewed smoke benchmark"
```

Alternatively supply `--invocation-id <exact-invocation-id>`. Run-ID lookup
requires one matching invocation receipt; directory timestamps are never used.
Promotion validates the exact artifacts and receipt, including successful process
completion, explicit quality PASS, complete observations, the approved dataset
selection and ordering, one-pass sequential live smoke protocol, known source,
clean source at run start, and source integrity MATCH. Cleaning the checkout
after a dirty run does not make that run eligible. Source artifacts stay immutable.

`config/baseline-promotion.json` defines the promotion policy. Ordinary smoke
requires quality PASS without requiring release qualification. Use
`--promotion-policy <reviewed-policy.json>` with `require_release_pass: true` to
require actual linked release PASS. Requested release qualification must also
pass. Existing release FAIL or REVIEW_REQUIRED evidence cannot be promoted.
An exit code alone is never release evidence. `--promoted-by-mode CI_APPROVED`
records an external approval; it does not implement an automatic approval flow.

PARTIAL_LINEAGE is allowed when required source/benchmark identities are known.
Unknown judge identities and immutable production model revisions remain explicit
limitations. Those limitations constrain comparison; promotion does not resolve
them or assert equality.

Promotion writes `baselines/<suite>/snapshots/<baseline_id>.json` exclusively,
verifies its own canonical-content digest, then atomically replaces
`baselines/<suite>/baseline.json`. The descriptor binds promotion metadata to the
snapshot and retains the previous baseline ID, source run ID, and snapshot digest.
A per-suite exclusive lock prevents concurrent lost updates. A crash may leave a
lock or an unreferenced immutable snapshot; inspect those artifacts before manual
recovery. Snapshots are never overwritten. Human review and Git commit establish
the approved descriptor and snapshot. Transient evaluation directories remain
under gitignored `reports/`.

## Candidate evaluation and trusted resolution

```powershell
python scripts/run_continuous_evaluation.py --suite smoke
```

This command intentionally performs the existing live evaluation when invoked.
It creates an invocation, runs the existing runner once, uses the exact linked
artifact path, and publishes an advisory comparison. It returns the underlying
evaluation exit code even when comparison fails. Cancellation and SystemExit
remain terminal. Receipt links identify the baseline and comparison when available.

Local resolution reads `baselines/<suite>/baseline.json` and verifies schema,
suite, confined snapshot path, digest, internal identity, and observation linkage.
Digests detect corruption, not malicious replacement of both descriptor and
snapshot. Trust comes from reviewing the selected baseline source.

CI must supply `--trusted-baseline-dir <trusted-checkout>/baselines`. This can be
a separately checked-out trusted base ref or an explicitly trusted artifact
directory. `--ci` or a truthy `CI` environment variable disables local fallback.
The resolver never silently substitutes baseline files from the candidate PR.
An invalid or missing trusted source is reported without changing quality gates.
No ref checkout, fetch, or network operation is performed by this implementation.

Without a descriptor, output is `BASELINE: NO_BASELINE`, with no invented metrics
or zero baseline. The first baseline must be promoted explicitly after review.

## Comparison semantics

Artifacts are written to
`reports/continuous_evaluation/comparisons/<comparison_id>/{comparison,summary}.json`.
Execution, artifact integrity, quality, release, and comparison statuses are
separate. Quality/release decisions remain authoritative; comparison is ADVISORY.

Scenario content, metadata, and expectation fingerprints classify unchanged,
added, removed, and modified scenarios. Metrics are recomputed over paired,
completed observations of unchanged scenarios. Added and modified outcomes are
retained separately; removed scenarios and missing executions are explicit.
Metrics report raw values, candidate-minus-baseline delta, cohort IDs, denominators,
sample counts, eligibility, and exclusion reason. Unequal known-value coverage
does not produce a delta, even when denominators happen to match.

Deterministic metrics require compatible fixtures, tools, safety policy, evaluator,
and aggregation identities. Semantic and composite safety metrics additionally
require known matching judge and evaluator configuration. UNKNOWN never equals
UNKNOWN for judge eligibility. Production model, prompt, and application changes
are recorded experimental variables; they do not automatically exclude outcome
metrics, and changed components do not establish causality.

Operational metrics additionally require matching population, full protocol,
repetitions, hosting, libraries, budget/retry policy, and measurement definition.
New manifests fingerprint the telemetry/observation measurement implementation.
Older 14B.1 manifests lacking that fingerprint remain readable and promotable
subject to other policy requirements, but their operational deltas are excluded
as missing required lineage. No identity is inferred from today's checkout.

Completed quality failures retain valid cohort comparisons and their FAIL status.
Incomplete runs retain failure observations/references and explicitly partial
paired cohorts; they never produce a full-population delta. Invalid candidate
artifacts provide no trusted observations or deltas. Legacy artifacts lacking
observations are reported with LIMITED_COMPARABILITY and null deltas; this version
does not reconstruct rows or infer denominators from historical aggregates.

Snapshots and comparisons use allowlisted schemas and existing redaction. They
retain numeric/categorical observations, identities, and fingerprints rather than
prompts, responses, exception messages, incident reasons, or arbitrary aggregate
payloads. Promotion reasons are length-limited and redacted.

A raw delta is arithmetic, not statistical drift, significance, probability, or
an overall better/worse assessment. No thresholds, automatic promotion, rollback,
or CI enforcement are added. 14C can consume these governed identities, eligible
cohorts, and explicit missingness without changing the authoritative gate source.
