# Changelog

## AgentGuard v1.0.1

Upcoming public-readiness/documentation release; not yet published. Hosted CI
for the final release commit is not yet complete. No new runtime or evaluation
capabilities are introduced, and evaluation semantics remain unchanged.

- Professionalized public technical documentation.
- Clarified architecture responsibilities, evidence flow and qualification limits.
- Replaced the former presentation guides with `PROJECT_OVERVIEW.md` and
  `DEMO_GUIDE.md`, with updated references and an offline technical walkthrough.
- Added the [public release review](docs/PUBLIC_RELEASE_REVIEW.md).
- Added [Apache License 2.0](LICENSE) licensing.
- Corrected CI so standard validation is deterministic and credential-free; the
  existing live evaluation requires explicit manual opt-in. Gate semantics remain
  unchanged.

## AgentGuard v1.0.0

Previously published implementation, identified by the existing `v1.0.0` tag.
That tag remains unchanged; the release-readiness changes above belong to v1.0.1.

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
  release qualification; existing gates remain authoritative. Manual live opt-in
  is a v1.0.1 release-readiness correction.
- **Documentation:** current architecture, future enterprise reference design,
  technical guides and committed synthetic offline demonstrations.

No packaging/version field existed to align; M17 introduces no new packaging
mechanism. Schema versions and the production-policy version retain their existing
meanings and are not changed to match a marketing/release version.

### Outside v1.0

Vendor adapters, RAG/KnowledgeProvider, MCP/ToolProvider, RAG and multi-agent
evaluation, autonomous production monitoring, organization-wide governance and
advanced analytics remain future possibilities. See the
[v2.0 reference architecture](docs/ENTERPRISE_ARCHITECTURE_V2.md).
