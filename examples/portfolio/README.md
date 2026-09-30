# Synthetic portfolio evidence

These examples complement the existing
[M16 fixtures](../../tests/fixtures/model_prompt_runs/README.md). They are authored
offline illustrations in the existing artifact schemas, not captured live runs.
There are no private prompts, responses, tool payloads, credentials, customer
identifiers or absolute machine paths.

| Run ID | Demonstration |
|---|---|
| `17000000000000000000000000000001` | COMPLETED; passing functional/semantic/safety observations and selected illustrative quality checks |
| `17000000000000000000000000000002` | INCOMPLETE; retained synthesis deadline, request correlation, stage timings, abandonment, partial usage and zero retries |

Run/scenario references and digests validate through `load_run`; timestamps,
model identities, source MATCH records and measurements are synthetic. The deadline
uses an illustrative shorter request budget, not a production configuration change.
The success summary is not a full release qualification or promotion candidate.
Tool identity/status unavailable in retained summary evidence remains unavailable
in the inspection output; no new telemetry schema was added for the demo.

Follow [INTERVIEW_DEMO.md](../../docs/INTERVIEW_DEMO.md) to copy the examples into
gitignored `reports/evaluations`, inspect them through the existing CLI and generate
observability output locally. Existing runtime output is not committed here.
