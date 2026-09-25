# Safety evidence retention

The smoke/full evaluation runner and `evals/test_agent_safety.py` retain sanitized
evidence from the execution they already evaluated. They do not execute the agent
again, call another judge, or change a safety verdict, retry policy, or release gate.

Detailed evidence is retained for safety failures, execution/evaluation exceptions,
and prompt-injection classifier/composite disagreement (including passing cases).
Ordinary passes do not create incident files. All safety categories use the same
recorder in `src/agentguard/safety_incidents.py`.

## Location and correlation

The recorder prints `Safety diagnostic: <relative path>` after writing:

```text
reports/safety_incidents/<run-uuid>/<incident-uuid>.json
```

`reports/` remains gitignored. Exclusive file creation and unique names avoid
overwriting earlier evidence. Scenario IDs are stored inside the artifact rather
than used as filesystem paths. A recorder shares its run ID across scenarios;
request IDs come from captured production telemetry.

## Schema version 1

| Field | Evidence |
| --- | --- |
| `schema_version`, `run_id`, `request_id`, `scenario_id`, `timestamp` | Artifact version, correlation IDs, and UTC retention time |
| `git_commit`, `sdk_versions`, `models` | Commit, installed SDK versions, captured configured/resolved model identities |
| `retention_reasons` | Safety failure, execution failure, exception, classifier disagreement, or explicit diagnostic |
| `scenario` | Input, category, risk, test intent, expected injection label, and relevant policy expectations |
| `planning` | Primary/final plan, completeness evidence, recovery state and usage |
| `request_policy` | Captured execution-plan grants and telemetry business outcome; policy is not recomputed |
| `execution` | Planned operations, required/completed/missing operations, execution status, and projected outputs |
| `tool_calls`, `projected_authoritative_facts`, `final_response` | Sanitized actual trajectory and response |
| `runtime_reliability_policy`, `production_telemetry` | Captured effective policy and sanitized production measurements |
| `safety_score` | Final category checks/reasons, action claims, raw injection label/rationale, composite verdict, disagreement flag/diagnostic, trajectory evidence, and separate evaluation usage |
| `exception` | Exception class and failing stage, without exception message or provider payload |
| `availability`, `sanitization` | Evidence availability, sanitization version, redaction count, and omission rules |

Absent evidence remains unavailable; no model identity, policy decision, or earlier
execution is reconstructed. Exceptions carrying a captured execution trace can
retain completed operations even when no `EvaluationRecord` was returned. The full
original request-policy decision is not currently captured by the runtime; the
artifact explicitly identifies its available execution-plan/telemetry source.

Raw classifier and composite verdict fields remain separate. The recorder retains
disagreements if either the existing disagreement flag is true or the two captured
labels differ. It preserves the original flag and does not adjudicate the result.

## Sanitization

Structured fields are allowlisted. Unknown fields, raw provider responses/bodies,
headers, environment mappings, and arbitrary object representations are omitted.
The recorder never reads environment variables. Tool arguments retain only the
current public `order_id` argument. Tool facts reuse the existing runtime disclosure
projection; records for orders outside the scenario's extracted targets are omitted.
Unstructured tool payloads are marked unavailable rather than copied verbatim.

Retained text is redacted for recognized credential/PII patterns and private values
found in the captured input structures, including JSON-encoded tool arguments and
outputs. This also removes those values when echoed in responses or judge reasons.
Sanitized responses are therefore diagnostic copies, not necessarily byte-for-byte
originals. Pattern matching cannot identify every arbitrary unlabeled secret in
free text; retention should not be used as a general-purpose secret scanner.

Writing is best effort: a serialization/filesystem failure prints a retention
warning containing only the exception class and leaves the evaluation decision
unchanged. Such a warning means evidence was not successfully retained.

## Explicit targeted diagnostic

For a later authorized single execution, from the repository root in PowerShell:

```powershell
python scripts/inspect_safety_eval.py --scenario-id prompt_injection_002
```

This command **makes live production and applicable safety-judge calls**. It selects
one existing safety scenario, executes it once, evaluates that same record, and
retains evidence even on an ordinary pass. It adds no retry attempts or policy
overrides. Its exit status reflects execution/safety success, not aggregate release
qualification. Creating this command and running offline tests does not execute it.
