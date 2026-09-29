# Milestone 14A: evaluation lineage foundation

Smoke/full, performance, reliability and structural release assembly create a
write-once evaluation identity before executing their workload. This is an
observation feature: model selection, prompts, datasets, policies, thresholds,
execution counts and existing quality decisions are unchanged.

## Local artifacts

`reports/evaluations/<uuid>/` contains `manifest.json`, `scenarios.json`,
`results.json`, and `completion.json`. Reports remain gitignored. Files publish
atomically with an exclusive hard link from a flushed temporary file; an existing
destination is never replaced. This requires a filesystem supporting hard links
(including the repository's Windows NTFS environment). Results and scenario indexes
reference the immutable manifest digest. Completion references the result digest.
Missing completion means INCOMPLETE, including process termination. Evaluation
completion and quality PASS are separate: completed failing evaluations remain
completed evaluations. In-memory manifests expose copies, not mutable identity.

One run ID follows real EvaluationRecords, incident evidence, and result envelopes.
Standalone incidents still generate their own UUID. Performance/reliability report
rows include manifest references. A structural release assembly is a separate
derived run; it links the current correctness run rather than pretending offline
transport evidence and live answers were one execution.

## Fingerprints

SHA-256 hashes UTF-8 canonical JSON of `{kind, canonicalization_version: 1,
content}`. Object keys are sorted, list order and string whitespace are preserved,
and duplicate JSON/YAML keys, non-string keys, unsupported types and nonfinite
numbers are rejected. YAML comments and formatting do not affect parsed-content
identity. Integers and floating-point values retain distinct canonical forms.
Adapters convert known dataclasses, enums and SDK schema objects into JSON types;
the generic utility does not guess how to serialize arbitrary objects.

UNKNOWN, ABSENT and DISABLED are distinct identity states. Effective runtime
defaults and SDK model settings are observed locally, without invoking a model.
Reliability qualifications fingerprint their explicit effective unbounded or
candidate policy rather than mislabeling it as the production-v1 default.

Full and selected functional/safety datasets have separate schema-v1 identities.
Scenario IDs remain unchanged. Inputs, metadata and expected behavior have separate
hashes; new unclassified fields fail closed until the partition is updated. Set
identity sorts scenarios by ID; execution-order identity preserves sequence and
repetitions. Business fixtures include orders.json and the evaluation date/window.

Local observation adapters cover router, omitted-work recovery, actionability
recovery and synthesis templates/output schemas; source hashes cover conditional
synthesis fragments and rendering logic. Tool descriptions, argument schemas,
capabilities, action registry, authorization and projection implementation are
included. Executable logic uses conservative source hashes, not claims of semantic
equivalence. Cosmetic source edits may therefore change identity. Evaluator source,
DeepEval version, gates and SLO definitions have separate fingerprints.

## Known identity limits

Production model names are SDK/configuration-resolved aliases, not independently
verified immutable provider revisions. Judges remain UNKNOWN when determining
their effective defaults would require initialization or unavailable provenance.
No judge is initialized just to collect lineage. Consequently current live
correctness manifests normally have PARTIAL_LINEAGE. Matching UNKNOWN judge values
never establishes scoring equivalence. Captured local source identifies the files
at run start, not remote provider internals; do not edit inputs during execution.
Fingerprinting cannot make a stochastic evaluation reproducible or detect a
coordinated rewrite of every artifact; hashes provide integrity links, not signed
authenticity.

## Comparison

Run `python scripts/compare_evaluation_runs.py <before-directory> <after-directory>`.
The reader checks manifest/result/scenario integrity before comparing. Results give
reason codes, changed components, eligible metrics, eligible scenario IDs and
UNCHANGED/ADDED/REMOVED/MODIFIED scenario classifications. Modified partitions are
listed; renames are not inferred. Incomplete runs are not comparable.

Application changes may be intended experiment variables under an unchanged
benchmark. Judge/evaluator changes remove affected semantic metrics; unchanged
deterministic metrics can remain eligible. Dataset changes compare only unchanged
scenarios. Gate-only changes preserve raw score comparisons, not gate decisions.
Execution-order, hosting, runtime-budget or measurement-protocol changes limit
operational comparisons. Live and offline-fixture evidence are not interchangeable.
No statistical significance or single-cause attribution is inferred.

## Privacy and legacy

Lineage stores fingerprints and IDs, not scenario text, answers, rendered customer
prompts, environment dumps, client objects, endpoints or credentials. SDK headers,
arbitrary request extras and metadata are excluded from settings fingerprints.
Only approved static templates/configuration and evaluation fixtures are hashed.
Hashes of customer data would still be sensitive and are not a substitute for
retention/access controls. Existing incident artifacts retain their own policy.

`read_legacy(path)` reads representative prior JSON reports as PARTIAL_LINEAGE or
LEGACY_UNVERSIONED, leaving missing fields UNKNOWN. No historical reports are
rewritten and backfill is deferred. No database or external service is introduced.

## Validation

Offline: `python -m pytest tests -q` and
`python -m pytest tests/faults/test_resilience.py::test_fault_matrix -q`.
The lineage tests cover canonicalization, partition changes, source/contract
dependencies, privacy, interrupted runs, schema validation, integrity, comparison,
and record/incident/result association without provider calls.

Later, explicitly authorized live validation requires just one smoke invocation:
`.venv/Scripts/python.exe scripts/run_agentguard_eval.py --suite smoke`.
This command has not been executed as part of 14A implementation.
