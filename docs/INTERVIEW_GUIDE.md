# Interview guide

Use the [offline demo](INTERVIEW_DEMO.md) for evidence and the
[architecture](ARCHITECTURE.md) for implementation boundaries. Speak about a
reference implementation; do not imply customer adoption or production results.

## 30-second explanation

“AgentGuard is an AI Quality Engineering reference framework for tool-enabled
agents. It combines deterministic correctness, tool and grounding checks with
semantic and safety evaluation, then applies explicit quality gates. It retains
failure evidence, versions evaluation lineage, and compares model/prompt variants
without rerunning them. It demonstrates how enterprise QE extends to AI while
keeping unknown evidence and production qualification limits visible.”

## Two-minute architecture explanation

“The reference agent routes requests, executes authorized local tools and
synthesizes a response under a deadline. Evaluation captures an EvaluationRecord
and telemetry. Deterministic checks verify behavior, operations, arguments and
supported facts. Semantic judges supplement those checks; adversarial safety has
its own population and policy composition.

“The existing scorecard and gate configuration own release requirements.
Versioned observations and the metric registry support descriptive comparisons.
Immutable manifests, scenario fingerprints, request IDs, invocation receipts and
source-integrity checks connect evidence to the code and benchmark that produced
it. Baselines require explicit governance. Optional observers project sanitized
evidence, and completed-run comparisons report model/prompt changes and eligible
deltas. Neither projection nor comparison silently reruns the agent.

“The offline demo uses synthetic fixtures. GitHub Actions also supports a separate
credentialed live smoke/release check. Enterprise adapters, RAG and MCP are future
architecture, not current integrations.”

## Five-minute technical explanation

| Time | Technical point | Evidence to show |
|---|---|---|
| 0–1 min | Turn expected behavior into deterministic assertions and required tool/argument contracts | Demo deterministic flags and architecture layers |
| 1–2 min | Supplement exact facts with relevance/correctness/faithfulness; separate adversarial safety | Retained semantic scores and safety result |
| 2–3 min | Preserve deadlines, retry ownership and terminal unknowns instead of hiding provider variability | INCOMPLETE fixture and failure envelope |
| 3–4 min | Explain gate authority, immutable lineage and governed baseline promotion | Gate checks, manifest fingerprint and source record |
| 4–5 min | Compare eligible evidence without ranking models; connect future enterprise systems through contracts | M16 report and green/future architecture diagram |

## Questions and defensible answers

**Why did you build AgentGuard?** To demonstrate how traditional QE concepts—test
contracts, traceability, evidence, release gates and regression analysis—apply to
AI agents whose behavior includes model judgment and external operations.

**Why aren't normal automated tests enough for AI?** Exact assertions remain
essential, but they do not fully assess answer relevance, faithfulness or semantic
attack-following. Agents also need tool, argument, grounding and operational
evidence beyond an apparently plausible final response.

**Why combine deterministic and semantic evaluation?** Use deterministic checks
where the system knows the correct facts or permitted operations. Add semantic
judgment for softer quality dimensions. Each layer has an explicit responsibility.

**How do you prevent judges becoming the source of truth?** Preserve deterministic
policy evidence, apply existing composite/adjudication rules, retain judge identity
and availability, and constrain comparison when judge identity is unknown or
changed. A semantic score does not authorize an operation or invent missing facts.

**How does it handle hallucinations?** It checks supported claims against captured
tool facts and uses semantic faithfulness evaluation. Coverage is explicit; there
is no claim of universal hallucination detection or a RAG implementation.

**How does it evaluate tool-using agents?** It checks required/allowed tools,
arguments, captured operation/trajectory evidence, authoritative outputs and
grounded results. The order-support domain supplies concrete predicates; record,
lineage, gate and comparison infrastructure is reusable.

**How does safety evaluation work?** Separate adversarial scenarios exercise
prompt injection, tool policy, data protection, unsupported actions and grounding.
Deterministic evidence and semantic classifiers feed documented composite rules.

**How do quality gates work?** Checked-in YAML requirements apply to the existing
scorecard. Structural release qualification adds 16 configuration/invariant gates.
Missing required evidence does not become PASS. Statistical report-only findings
remain separate from enforced failures and performance qualification.

**How do you handle flaky provider latency?** Keep correctness smoke latency
report-only and use separate repeated performance qualification. Preserve per-stage
timing, deadlines, late completion and abandonment. A local transport qualification
is not a guarantee about internet-provider latency.

**How do you prevent retries hiding defects?** Default production application and
qualified lower-layer retries are disabled. Explicit experimental retry paths
retain policy/attempt evidence and require deliberate invocation; they are not
silent recovery around correctness failures.

**Why is lineage important?** A metric only has meaning with its benchmark,
expectations, evaluator, model/judge identity, policy and execution protocol.
Fingerprints and source integrity make incompatible or missing evidence visible.

**How are baselines governed?** Promotion is explicit and checks eligibility,
completion, quality PASS, source state, observations and exact invocation linkage.
CI requires a trusted baseline source. The latest passing directory is not an
automatic baseline, and the demo fixtures are not eligible promotion evidence.

**How does model/prompt regression work?** It loads two completed runs, validates
artifacts, derives technical identity from lineage and reuses M14 paired cohorts
and metric eligibility. It reports directional deltas and scenario transitions
where comparable. It does not rank models, add thresholds or declare a winner.

**How would this integrate into an enterprise?** Keep evidence/decision logic in
AgentGuard and operational workflows in existing systems. GitHub Actions and local
observer contracts exist now; other CI, reporting, storage and incident adapters
are proposed extension work.

**Why use OpenTelemetry?** Its span/trace/attribute concepts provide an appropriate
vendor-neutral export boundary. v1 supplies a deterministic mapping and optional
local observers, not an installed collector, SDK exporter or vendor backend.

**Where would RAG fit?** In a future KnowledgeProvider with document provenance,
versioning, permissions, freshness and retrieval evidence. Requirement-derived
benchmarks would still need review and reproducibility controls.

**Where would MCP fit?** As a possible future ToolProvider transport for approved
enterprise capabilities. Server availability does not remove the need for least
privilege, allowlists, schema validation, approvals and prompt-injection resistance.

**What would you build in v2.0?** Start with enterprise adapter contracts driven
by a real team's workflow; then consider governed knowledge/tool providers, RAG
and multi-agent evaluation, broader production feedback, policy integration and
organization-wide governance. These remain future possibilities.

## Factual portfolio/resume bullets

- Built an AI Quality Engineering reference framework spanning deterministic,
  semantic, adversarial, reliability and model/prompt regression evaluation, backed
  by **2,490+ automated offline tests**.
- Implemented and validated a **28-case offline fault matrix** retaining failure
  telemetry and safe-failure classifications; bounded results do not imply a
  production availability guarantee.
- Added **16 structural/configuration release gates** alongside existing quality,
  semantic, safety and usage requirements, with GitHub Actions integration.
- Designed versioned evaluation lineage and explicit baseline governance, plus
  comparison across **four dimensions**: quality, semantic, safety and operational.
- Added **three observability event concepts**—span, metric and evaluation—with
  opt-in local export, OTel-compatible mapping and sanitized correlation metadata.

These describe implementation/test evidence only. They make no claim about revenue,
customers, production adoption, deployment scale or business savings.
