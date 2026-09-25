# Required execution obligations

The production path is now:

1. Semantic routing builds capability bindings.
2. A bounded [planning completeness review](planning_completeness.md) checks suspicious empty plans before request policy resolves the minimum authoritative tool cover for each target.
3. `build_execution_plan()` turns those authorized grants into required operations.
4. `execute_required()` executes each operation once and projects its result through the existing capability disclosure policy.
5. The support model receives the original request plus actual, projected function-call history and synthesizes one answer.

The execution layer does not read user text, scenario IDs, datasets, or evaluation policies. It does not repair empty router plans; completeness review is a separate upstream planning concern. Clear grants still execute when another request component needs clarification. Unresolved or unauthorized components create no required operation. For combined status and eligibility on one target, the existing resolver selects the eligibility tool alone.

`OperationMode.REQUIRED` blocks synthesis until successful completion. `OPTIONAL` permits explicit execution without blocking completion; the current policy does not emit optional reads. `PROHIBITED` and operations outside the plan cannot execute. Unsupported write actions are listed from the shared action registry, not a second mapping.

The answer model has no executable tools. It interprets supplied results, explains missing orders and eligibility, handles unresolved components, and refuses prohibited portions. Its instructions reflect that role; enforcement comes from deterministic execution, not prompt compliance. Any attempted model function call is recorded and rejected by an SDK lifecycle hook. There is no continuation or application retry.

## Results and failures

Only capability-projected data enters model input or execution telemetry. SDK `RunResult.new_items` remains model-generated activity; direct runtime operations are recorded separately in `RunResult.context_wrapper.context` as an `ExecutionTrace`. They are not fabricated model calls.

A successful read returning `found=false` completes its obligation. A missing implementation, exception, or projection failure leaves the operation unresolved and raises `ExecutionFailure` before synthesis. The exception carries a trace and production usage, but telemetry contains only error codes/types, not exception payloads. Successful operations and failed attempts are not retried.

`EvaluationRecord.execution` contains the plan, per-operation status, call IDs, measured latency, required/completed/missing IDs, optional IDs, and prohibited attempts. `execution_error` is set for a rejected run. A failed run has an empty `final_output` because no support answer was accepted. Existing tool-call and output fields capture actual runtime invocations and projected results once, along with any legacy SDK-generated trajectory. Missing implementations are not recorded as invoked tools; failed invocations have no invented output.

The release runner reports captured execution errors and returns FAIL before invoking judges for that record. Safety adjudication and configured quality gates are unchanged.

Production token accounting includes primary router usage, any bounded planning recovery, and answer-model usage, merged once. Direct tools consume no inference tokens. A typical resolved request uses two model responses (routing and answer); a triggered recovery adds one. Token/latency diagnostics include direct-tool timings and exclude execution failures from successful performance observations.
