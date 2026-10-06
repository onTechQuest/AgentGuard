# AgentGuard project overview

AgentGuard evaluates whether AI and tool-enabled agents behave correctly and
reliably. The included order-support agent supplies a reference workload. Results
describe tested boundaries, not customer adoption or universal production safety.

## Implemented v1 capabilities

| Area | Implementation and limits |
|---|---|
| Functional | `EvaluationRecord` and `scoring.evaluate_record` produce `ScenarioScore`: required/forbidden text, configured behavior predicates, tool names, structured arguments and optional authoritative facts. No arbitrary semantic equivalence or general JSON assertion language. |
| Multi-step | Actual calls, projected outputs, `ExecutionPlan`, `ExecutionTrace`, planning and operation states retain required, missing and prohibited work. Generic expected-call matching is unordered; runtime obligations precede synthesis. |
| Semantic | DeepEval relevance, GEval correctness and HallucinationMetric consistency with captured tool outputs. Without tool context hallucination evidence is unavailable. Live judging differs from offline doubles and fixture inspection. |
| Safety | `safety_evaluate_record` returns `SafetyScore`: applicable tool policy, grounding, data protection, unsupported-action and injection checks. Classifier factories enable offline tests; unconfigured policies remain null and applicable classifier errors fail. |
| Reliability | Request/stage budgets, bounded planning recovery, explicit retry ownership, failure retention and bounded worker/queue isolation. Default production retries are disabled; experimental retry paths require separate configuration. |
| Performance/cost | Repeated qualification, latency and request/component token accounting; production and judge usage remain separate. Attributed verified pricing enables calculated USD estimates. Absent pricing is TOKEN_ONLY, not invoiced spend. |
| Regression | Validated existing-run comparisons, paired eligible populations and scenario changes. Baseline promotion is explicit; model/prompt reports are advisory and declare no winner. |
| Release governance | `AgentGuardScorecard`, `QualityGateResult` and 16 structural/configuration gates use checked-in policies. Missing required evidence cannot establish a gate pass; correctness-smoke latency is report-only. |
| Observability | `NullObserver`, `JsonObserver`, `ConsoleObserver`, sanitized projection and `otel_span` mapping. No installed collector, vendor exporter or autonomous monitoring service. |
| Datasets/lineage | Functional/safety scenarios, fingerprints, immutable manifests, completion/result digests, invocation receipts and source-integrity checks. Digests detect mutation but do not authenticate untrusted sources. |

## Deterministic and probabilistic evidence

Tool/argument and policy checks answer different questions from model judgments.
Retain judge/evaluator identity, configuration, availability and paired scenario/
repetition populations for comparisons. Unknown judge identity is not a match.

Judge variance can move scores without a substantive behavior regression. Existing
gates apply configured thresholds without statistical noise filtering. Comparison
reports expose arithmetic changes and eligibility, not confidence intervals,
significance tests, effect sizes, repeated-judge uncertainty, noise bands or judge
calibration. Those are future evaluation guidance, not v1 functionality. Asking
a classifier for confidence does not establish calibrated statistical confidence.

## Relationship to AutoQE

AutoQE uses AI-assisted orchestration to perform QE work. AgentGuard evaluates AI
and agent behavior. AutoQE's integration is external and provider-neutral;
AgentGuard does not depend on AutoQE. The implemented joint evaluation is limited
to triage-classification agreement, not independent validation of all AutoQE
planning, execution or quality results.

## Scope and reading path

Continue with [architecture](ARCHITECTURE.md), the [offline demo](DEMO_GUIDE.md),
and [qualification/release review](PUBLIC_RELEASE_REVIEW.md). Deeper engineering
documents identify their own evidence populations and limitations.

Enterprise adapters, RAG/KnowledgeProvider, MCP/ToolProvider, broader multi-agent
evaluation, richer governance integrations and production analytics are
[future proposals](ENTERPRISE_ARCHITECTURE_V2.md), not installed capabilities.
