# Model and prompt regression comparison

Compare two **existing, completed** evaluation runs on the same benchmark. This
is a view over M14's validated evidence, paired cohorts, metric registry, and
comparability checks. It does not run models, judges, tools, or scenarios; promote
a baseline; alter gates; select a winner; or compute an overall score.

```powershell
.venv/Scripts/python.exe scripts/compare_model_prompt_runs.py --baseline-run 16000000000000000000000000000001 --candidate-run 16000000000000000000000000000002 --baseline-label "fixture-v1" --candidate-label "fixture-v2"
```

These are explicitly synthetic `offline_fixture` runs, not live qualification.
Their retained artifacts are checked in under `tests/fixtures/model_prompt_runs`.
On a fresh checkout, copy those two run directories into `reports/evaluations`
before running the command. The demo shows a model/prompt change, new and unchanged
functional failures, semantic and safety regression, and lower observed latency
and tokens. No production prompt or model is changed by installing fixtures.

## Identity and validity

Display labels never determine identity or the comparison ID. Authoritative
identity uses resolved production models/providers, known immutable revisions,
observed model identities, and the existing `prompt_bundle` fingerprint. The
prompt fingerprint conservatively includes component source and effective
templates; it does not expose or reconstruct prompt text. Classification is
`MODEL_CHANGE`, `PROMPT_CHANGE`, `MODEL_AND_PROMPT_CHANGE`,
`NO_MODEL_PROMPT_CHANGE`, or `UNKNOWN`.

Missing resolved model or prompt identity yields `UNKNOWN`. Equal resolved names
with unavailable revisions mean equal **recorded names**, not proven equality of
underlying provider versions; `PRODUCTION_REVISION_UNKNOWN` is explicit. Unequal
known revisions count as a model change. One known and one unknown revision cannot
prove equality. Unknown judge identities never count as matching identities.

Both inputs pass the existing manifest, scenario index, result digest, completion,
observation, metric aggregate, and source-integrity validation. Incomplete runs
are rejected. Completed runs that failed existing quality gates remain valid
inputs, with their failures displayed. Legacy completed artifacts remain readable,
but unavailable observation evidence cannot produce regression claims.

Quality, semantic, safety, and operational dimensions each report `COMPARABLE`,
`PARTIALLY_COMPARABLE`, or `NOT_COMPARABLE`. M14 metric exclusions remain in force.
M16 additionally requires the same benchmark/datasets, scenario expectations and
population, compatible evaluation protocol, and `MATCH` source integrity. Changing
only production model or prompt does not invalidate quality comparisons. Changed
evaluators, policies, tools, or judges constrain the affected metrics. Differences
in execution profiles prohibit operational comparison while permitting otherwise
compatible quality comparison. Legacy profiles use M14's recorded runtime-policy,
hosting, protocol, and measurement fingerprints; missing required evidence blocks
comparison. Changed gate fingerprints are reported as limitations, not hidden.

## Results and gates

Metrics use M14's paired completed observations, exact repetition identities,
known-value denominators, and registry definitions. Each row retains values,
signed absolute delta (`candidate - baseline`), relative delta (null for a zero
baseline), registry direction, eligibility, and a reason when unavailable.

Directional statuses are `IMPROVED`, `REGRESSED`, `UNCHANGED`, `NOT_COMPARABLE`,
or `UNAVAILABLE`. Token metrics currently have registry direction `INFORMATIONAL`:
their numeric deltas are reported, but a nonzero change has directional status
`UNAVAILABLE`, reason `REGISTRY_DIRECTION_INFORMATIONAL`. This feature does not
silently redefine them as lower-is-better. Retry direction uses the registry's
`ZERO_IS_REQUIRED`. Metrics absent from the registry, including monetary cost and
an aggregate semantic-pass metric, are not invented.

Scenario output lists new, resolved, and unchanged failures per comparable
boolean check and repetition. It never turns missing scores into failures or
recomputes a scenario decision. Existing gate decisions and allowlisted failed
check names, actual values, operators, and thresholds provide gate context.
Free-text failure messages are omitted. Unknown gate evidence remains unknown.

All output is descriptive. Correctness smoke latency stays report-only; a
comparison never becomes performance/release qualification or a new winner gate.

## Artifacts and extension

`reports/model_prompt_comparisons/<comparison_id>/comparison.json` uses schema
version 1. The ID is the first 32 hex characters of the versioned lineage hash of
the ordered source run IDs, manifest/result digests, and metric registry version.
Creation time and labels do not affect it. Publishing is write-once; repeat
commands reuse the first artifact and its display labels. Changed evidence cannot
silently overwrite an existing technical comparison. Source runs are never mutated.

Only allowlisted identities, fingerprints, numeric/boolean evidence, and safe
labels are exported. Existing redaction rules and the artifact schema exclude
raw prompts/responses, user input, tool output/payloads, credentials, headers,
environment values, customer identifiers, and exception messages. Unsafe labels
are rejected without echoing their content.

AgentGuard answers whether an observed comparison is valid and what changed. It
does not replace experiment-management platforms. Future adapters could publish
these artifacts into MLflow, Weights & Biases, Braintrust, LangSmith, or enterprise
CI/CD systems. No integrations, database, dashboard, model registry, prompt
management, deployment orchestration, or automatic rollback are implemented here.

## Judge variance and statistical limits

`REGRESSED` describes a directional change in eligible recorded values, not a
statistically established regression. Judge/model variance can cause score noise.
Paired scenario/repetition cohorts and compatible judge/evaluator identities are
necessary but do not establish significance.

V1 does not compute judge confidence intervals, noise bands, significance tests,
effect sizes, repeated-judge uncertainty or judge calibration. Existing quality
gates enforce configured thresholds without a statistical noise filter; this
comparison does not alter them. Calibrated repeat judging and significance-aware
release policy are future evaluation guidance, not current release capabilities.
