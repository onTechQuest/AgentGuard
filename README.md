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
documents experimental L/R policies and the explicit `--enforce-candidate-budget`
flag. Candidate selection alone leaves deadline enforcement disabled.
