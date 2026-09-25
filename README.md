# AgentGuard

Production-grade evaluation, testing, observability, and CI/CD quality gates for AI agents.

## Status

Initial project setup.

## Quality Gate

AgentGuard automatically evaluates agent behavior, tool usage, argument correctness, latency, and token consumption before release.

Reliability measurement and shadow retry-policy comparisons are available through
`--suite reliability`. See [Reliability qualification](docs/RELIABILITY_QUALIFICATION.md)
for explicit configuration and commands. Production retries remain disabled by default.

[Controlled deadline qualification](docs/CONTROLLED_DEADLINE_QUALIFICATION.md)
documents experimental L/R comparisons and the explicit `--enforce-candidate-budget`
flag for qualification. Candidate selection alone leaves qualification descriptive.

Normal production requests now use the [qualified v1 reliability policy](docs/PRODUCTION_RELIABILITY_V1.md):
a 20-second request deadline, hierarchical stage limits, and no retries.
