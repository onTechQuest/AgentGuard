# Changelog

## AgentGuard v1.0 — prepared for release review

Core scope is complete and frozen. No release date or Git tag is assigned by this
document. Human review, commit selection and tag creation remain separate actions.

- **Evaluation:** deterministic functional, tool-selection/argument, operation
  trajectory and grounded-response checks; supplementary semantic judges.
- **Safety:** separate adversarial scenarios, prompt-injection adjudication,
  tool/data policy, unsupported-action and grounding evidence.
- **Reliability/performance:** request/stage budgets, explicit retry ownership,
  retained incomplete-run evidence, bounded concurrency and transport qualification,
  latency and production/judge usage separation.
- **Lineage/governance:** immutable manifests/fingerprints, schema/digest validation,
  exact invocation handoff, source integrity and governed baseline promotion.
- **Continuous evaluation:** explicit existing-runner invocation, per-scenario
  observations and advisory comparisons against a trusted baseline.
- **Observability:** default no-op observer, opt-in local JSON/console output and
  dependency-free OTel-compatible span mapping.
- **Model/prompt regression:** completed-run comparisons with technical change
  detection, dimension-specific eligibility, registry deltas and scenario failures.
- **CI/CD:** GitHub Actions offline tests and credentialed live smoke/structural
  release qualification; existing gates remain authoritative.
- **Portfolio:** concise architecture, future enterprise reference design,
  interview narrative and committed synthetic offline demonstrations.

No packaging/version field existed to align; M17 introduces no new packaging
mechanism. Schema versions and the production-policy version retain their existing
meanings and are not changed to match a marketing/release version.

### Outside v1.0

Vendor adapters, RAG/KnowledgeProvider, MCP/ToolProvider, RAG and multi-agent
evaluation, autonomous production monitoring, organization-wide governance and
advanced analytics remain future possibilities. See the
[v2.0 reference architecture](docs/ENTERPRISE_ARCHITECTURE_V2.md).
