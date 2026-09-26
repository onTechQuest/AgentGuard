# Routing, completeness and policy evidence

Milestone 13D.4B introduced observations only. Milestone 13D.4D adds bounded
actionability-aware recovery, documented in [planning completeness](planning_completeness.md).
Authorization thresholds and reliability policies are unchanged. No evaluator
expectations enter production decisions.

`production_telemetry.decision_summary` is compact. Detailed `decision_evidence`
(schema version 1) is retained for runtime failures, empty plans, or explicit
`diagnostic_mode=True`. Ordinary successful nonempty requests omit detailed evidence.
Qualification callers must enable diagnostic mode before execution if they need
details for failures identified by subsequent business scoring. `WorkerRuntime`
accepts this observation flag and `CampaignCollector` preserves the evidence.

The decision chain contains:

| Section | Fields |
| --- | --- |
| router | Bindings, normalized targets, capability outcome, confidence, ambiguity, controls |
| completeness | Input/final plans and binding actionability, branch code, review trigger/type, recovery entry/count/result/admission, recovered bindings and confidence, scope-preservation result, semantic-state source |
| policy | Candidate plan, confidence threshold, per-binding grant/deny/clarification and reason, grants |
| execution_plan | Grants, required tools/arguments, explicit empty reason |
| categories | Observed distinctions; multiple may apply |

Only registered capability/tool/control identifiers, normalized order references,
typed values and internal reason codes are retained. Unknown capability names are
replaced with `<unregistered>`. No prompts, generated prose, exception messages,
denied-disclosure data, private tool fields or tool-result payloads enter this schema.
Order IDs are represented by request-local `entity_1`, `entity_2`, etc. in extraction
order. The same reference is used for candidate targets, grants and operation
arguments. Literal order IDs and the private reference mapping are not serialized.
Request-local telemetry provides isolation. Observation errors mark telemetry
incomplete without changing the request's outcome.

## Reason taxonomy

- `ROUTER_OMISSION`: empty primary bindings relative to an evaluation contract that
  expects business work; it is not an independent semantic judgment.
- `COMPLETENESS_SKIP`: review not triggered. The branch distinguishes existing
  bindings, no controls, no target, ambiguity, multiple targets, or legacy plans.
- `RECOVERY_NOT_ADMITTED`: observed admission denial; the budget reason is retained.
- `POLICY_DENIAL`: a candidate binding was denied, with confidence, unknown
  capability, unsupported capability or missing authoritative cover distinguished.
- `CLARIFICATION_REQUIRED`: policy needs clarification; ambiguous/missing-target
  bindings are recorded separately from denied bindings.
- `AUTHORIZED_EMPTY_PLAN_BUG`: authorized grants exist but no required operation
  was produced. This is distinct from zero grants.
- `LEGITIMATE_NO_WORK`: recovery confirmed empty work, or an external evaluation
  contract explicitly expects no work. An empty plan and an ID alone prove neither
  legitimate no-work nor a router defect.
- `EMPTY_BINDINGS_UNASSESSED`: empty plan without sufficient evidence to establish
  intent. This prevents silently labeling an omission as legitimate no-work.

Recovery admission can be unavailable for unbounded/injected paths; absence of
admission evidence is not a denial. No recovery call and admission failure are
distinct from a completed recovery returning no bindings.

## Explicit one-shot diagnostic (live, do not run without authorization)

```powershell
.venv/Scripts/python.exe scripts/diagnose_decisions.py --scenario order_status_004 --execute-live --output reports/concurrency_13d4b/order_status_004.json
```

This runs one selected functional scenario on one owned worker, with the unchanged
production reliability policy, and no judges or reruns. The scenario ID is used
only by the diagnostic selector/scorer. The worker receives only user input.
The artifact includes sanitized decisions, timing/usage and deterministic result
flags. An exclusive pre-execution claim prevents accidental reuse of the output
target after failure; publication is atomic and cannot overwrite prior evidence.
No live diagnostic was performed as part of the implementation.

Actionability review records primary confidence and binding clarification separately
from recovered confidence and clarification. `review_type` distinguishes omitted
work from binding actionability. `scope_preservation` is `NOT_CHECKED`, `PRESERVED`
or `REJECTED`; validated recovery output remains visible on scope rejection, but
does not become the final plan. `semantic_state_source=RECOVERY_OUTPUT` means the
reviewer explicitly supplied that state; it does not certify semantic correctness.
Policy results and required operations show whether that proposal was authorized.

For a separate 13D.4D validation, use a new artifact target; do not overwrite the
13D.4B incident:

```powershell
.venv/Scripts/python.exe scripts/diagnose_decisions.py --scenario order_status_004 --execute-live --output reports/concurrency_13d4d/order_status_004.json
```

For one later 13D.4G contract validation, use a new target:

```powershell
.venv/Scripts/python.exe scripts/diagnose_decisions.py --scenario order_status_004 --execute-live --output reports/concurrency_13d4g/order_status_004.json
```
