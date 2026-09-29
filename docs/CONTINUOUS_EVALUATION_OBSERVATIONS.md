# Continuous evaluation observation contract (14B.1)

Existing CLI runners publish an invocation receipt before setup at
`reports/continuous_evaluation/invocations/<invocation_id>/receipt.json`.
The receipt moves from CREATED to RUNNING and then COMPLETED, FAILED,
CANCELLED, or SETUP_FAILED. It records the exact evaluation and optional
release run IDs and absolute artifact paths, exit code, and sanitized error
types. Consumers use these links rather than discovering the newest directory.
Baseline and comparison IDs remain null.

Evaluation results add version 1 observations, a versioned metric registry,
descriptive aggregates, protocol identity, and source integrity. Each execution
has a scenario fingerprint, population, explicit repetition number, completion
state, deterministic scores, separate semantic and safety evidence, judge
identity links, and available operational telemetry. Observed production model
names are retained separately from configured identities; unavailable immutable
revisions remain UNKNOWN. Failure observations reference the existing sanitized
failure envelope by run, manifest digest, scenario, and repetition.

Metrics declare family, unit, aggregation, population, required lineage, and
direction. Aggregates report known-value denominators, missing counts, and
population completeness. They are descriptive and never control release gates.
Unknown measurements remain null, with usage availability explicit. Missing
fields in later partial measurements do not erase earlier evidence for the same
execution. No additional evaluator, model, retry, or scenario execution occurs.

Protocol identity records suite, repetition count, ordering, execution mode,
hosting, and scenario populations. Start and end source inventories produce
MATCH, CHANGED, or UNKNOWN without modifying the manifest. The approved source
inventory includes source, scripts, configuration, evaluation datasets,
requirements, and the orders fixture, but excludes secrets and generated reports.

The loader validates schema versions, digests, run references, scenario index
fingerprints, execution counts, protocol/repetition identity, observations,
aggregate denominators, and source integrity. Valid partial runs remain readable.
A completed collection can contain failed scenario executions, preserving the
existing runner semantics. Legacy version 1 artifacts lacking observations are
readable with PARTIAL_LINEAGE and LIMITED_COMPARABILITY annotations.

Observations use fixed allowlists and existing redaction; they omit prompts,
responses, evaluator reasons, credentials, headers, and customer content.
Receipts retain exception types rather than exception messages.

One later live smoke validation, when explicitly authorized:

```powershell
.venv/Scripts/python.exe scripts/run_agentguard_eval.py --suite smoke
```

This milestone does not create baselines, score drift, or change gate decisions.
