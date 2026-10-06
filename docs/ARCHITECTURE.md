# AgentGuard v1.0 architecture

AgentGuard evaluates a tool-enabled reference agent while keeping evaluation
evidence separate from runtime behavior and release decisions separate from
observations.

The [README diagram](../README.md#how-it-fits-together) is the overview. The
[offline demo](DEMO_GUIDE.md) makes each boundary inspectable.

## Layers and ownership

| Layer | Responsibility and implementation |
|---|---|
| Execution | `src/agent/`: routing, bounded recovery, authorized required tools, synthesis, request budgets and transport ownership |
| Evaluation | `scoring.py`, `behavior_predicates.py`, `semantic_evaluator.py`, `safety_evaluator.py`: deterministic checks followed by supplementary semantic evidence; separate safety population |
| Evidence | `EvaluationRecord`, production telemetry, retained terminal failure envelopes and versioned per-scenario observations |
| Metrics | Existing scorecard for gates; `metric_registry.py` for descriptive comparison definitions, populations and known-value denominators |
| Gates | `quality_gate.py` and structural release qualification apply checked-in requirements; unavailable required evidence cannot become PASS |
| Lineage | Immutable manifest/scenario fingerprints, results/completion digests, invocation receipts, repetition identities and source-integrity checks |
| Reliability | Request/stage deadlines, explicit retry ownership, usage completeness, concurrency isolation and bounded offline fault/transport qualification |
| Observability | Default `NullObserver`; optional sanitized JSON/console events and deterministic OTel-compatible span mapping |
| Comparison | Governed baseline/candidate comparisons and explicit completed-run model/prompt regression reuse M14 eligibility and paired cohorts |

Evidence flows from actual execution to evaluators, then into retained artifacts.
Observers and offline comparisons consume it; they do not execute requests or
judges to fill gaps. Artifact schemas validate shape and linkage, while digests
detect changes. Digests alone do not authenticate an untrusted artifact source.

## Decisions that matter

| Decision | Why it matters |
|---|---|
| Deterministic validation before semantic judgment | Tool names, arguments, authorization and known facts are explicit contracts; semantic judgments supplement them |
| Separate safety evaluation | Adversarial behavior has its own scenarios, policies, evidence and composite results |
| No silent retries | Default production retries are disabled; retry attempts and lower-layer configuration remain attributable |
| Retain terminal evidence | Incomplete evaluations preserve sanitized exception taxonomy, request correlation, deadlines, stage latency, usage and abandonment when available |
| Separate correctness and performance | Smoke/full correctness uses its documented evaluation profile; small-sample latency is report-only, not production latency qualification |
| Immutable lineage and explicit promotion | A baseline is reviewed evidence, not the newest passing directory; CI resolves an explicitly trusted baseline source |
| Unknown stays unknown | Missing timing, judge identity, model revision or telemetry is never fabricated or treated as zero |
| Integration-first observability | Existing enterprise stacks own storage, collection and dashboards; local projection is optional and exporter failures do not change gates |
| No model winner | Regression reports show eligible deltas and limitations without ranking models or creating an overall score |
| Domain predicates are separate | Order-support assertions live alongside reusable record, observation, gate and comparison contracts; the example domain is not the framework's entire architecture |

## Key architectural boundaries

**Deterministic versus semantic.** Deterministic checks verify captured operations,
arguments and supported facts. Judges evaluate softer output quality. Safety
combines deterministic policy evidence with semantic classifiers under explicit
adjudication rules. A judge cannot silently replace missing authoritative evidence.

**Quality versus operational qualification.** The production v1 policy and
correctness evaluation profile are distinct. The bounded concurrency envelope
and offline loopback/fault results do not establish internet-provider tail latency,
sustained capacity, or a universal deployment SLO. Cost analysis separates
production usage from judge usage and requires attributed pricing for money claims.

**Runtime versus evaluation.** The reference agent's tool grants, deadlines and
retry policy live in runtime modules. Evaluators inspect what occurred. M15/M16
projection and comparison do not change prompts, operations, models or gates.

**Core versus enterprise systems.** A GitHub Actions workflow already invokes
offline tests and manually opted-in live smoke/release checks with credentials.
Observer methods and OTel-compatible mapping exist; vendor exporters, a generic
plugin configuration system, RAG and MCP do not. See the explicitly
[future architecture](ENTERPRISE_ARCHITECTURE_V2.md).

## Evidence references

- [Release gate authority and bounded qualification](MILESTONE_13.md)
- [Production reliability policy](PRODUCTION_RELIABILITY_V1.md)
- [Evaluation lineage](EVALUATION_LINEAGE.md) and [baseline governance](CONTINUOUS_EVALUATION_BASELINES.md)
- [Observability](OBSERVABILITY.md) and [model/prompt regression](MODEL_PROMPT_REGRESSION.md)

## Multi-step execution contracts

`src/agent/execution_plan.py` defines `Operation`, `OperationMode`, `ExecutionPlan`,
`OperationExecution` and `ExecutionTrace`. `build_execution_plan` derives obligations
from authorized grants. `execute_operation` rejects unauthorized/prohibited work;
`execute_required` and `ExecutionTrace.model_input` prevent synthesis with unresolved
required operations. Completed work is reused; failed work is not silently retried.
The current resolver emits required reads. Optional/prohibited modes are contract
capabilities, not additional business features.

`evaluation_record.execute_scenario` captures actual runtime trace calls and SDK
items into `EvaluationRecord`, including partial work and explicit execution failure.
`scoring.evaluate_record` returns `ScenarioScore` using maximum one-to-one matching
of expected calls/arguments without requiring order. Missing calls fail tool and
argument checks. Generic matching permits extra calls/arguments; scenario tool
allowlists impose configured limits. Safety independently checks required/permitted
tools and captured facts.

Multi-target work, bounded planning recovery, obligations before synthesis and
recovery-then-failure behavior have offline coverage in `test_execution_plan`,
planning-completeness tests and the fault matrix. Stage/order evidence is retained;
no generic expected ordered sequence metric or reasoning-chain inspection exists.
See [required execution](required_execution.md) and [fault coverage](FAULT_INJECTION.md).

## Evaluation and decision flow

Execution → `EvaluationRecord` → deterministic `ScenarioScore` and optional
`SemanticScore`; separate safety scenarios produce `SafetyScore`. The scorecard
aggregates results for quality gates. Structural qualification combines configuration
and transport evidence with quality results. Reliability/failure telemetry,
observations and lineage link outcomes to their sources. Existing-run comparisons
and observers consume retained evidence; neither changes gate authority.

[Judge/comparison limits](PROJECT_OVERVIEW.md#deterministic-and-probabilistic-evidence)
and the [external AutoQE boundary](PROJECT_OVERVIEW.md#relationship-to-autoqe)
apply to this architecture.
