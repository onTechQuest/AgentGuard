# Planning completeness before authorization

The primary semantic router is unchanged. The production sequence now inserts `validate_planning()` between its result and `resolve_request_policy()`. The execution-obligation implementation is unchanged.

## Invariant and review boundary

An explicit empty plan with recognized targets, control signals and no known ambiguity cannot silently pass to authorization without one bounded semantic completeness review. Review evidence is not authorization. Any proposed binding still needs a registered supported capability, a parsed target, and normal runtime policy approval.

The validator skips review for existing bindings (including clarification-required or unsupported-action bindings), legacy injected plans without an explicit binding contract, missing entities, existing ambiguity, unbound multiple targets whose scope is not `all`, and messages without control signals. Primary confidence does not prove completeness and is never increased by recovery.

Controls plus entities identify a conservative candidate for review, not proof of a legitimate task. Exact registered tool/capability references and their registry authority relationships are provided as additional evidence. No natural-language phrase rules classify return eligibility or order status. An entity or a tool name alone never forces a lookup.

## One semantic recovery call

The tool-free reviewer asks whether legitimate supported business work remains after disregarding fabricated claims and untrusted control instructions. It uses the same model selection as production and returns only `capability_requests`, each with a capability, target and clarification flag. It cannot emit tool grants, disclosure permissions or a replacement confidence score.

The review distinguishes live verification requests from hypothetical examples, quotations, documentation, mere references, disclosure-only requests and prohibited transactions. It can return an empty list; that confirms an empty plan without a tool call. This distinction is semantic, so mocked offline tests validate the boundaries and orchestration, not real-model classification accuracy.

The validator checks every recovered capability against the shared registry and every target against the original extracted entities. Existing control signals, disclosure decisions, confidence and extracted entities survive unchanged. Recovery can add clarification requirements, but it cannot clear pre-existing ambiguity. Normal policy still resolves the minimum authoritative cover and applies confidence, target and privacy restrictions.

There is no recursive replan or application retry. A malformed recovery, invalid capability/target, or recovery exception produces `PlanningCompletenessError` before authorization and synthesis. Error telemetry omits exception payloads. Existing release handling rejects the captured error instead of judging an invented response.

## Telemetry and cost

`RunResult.context_wrapper.context.planning` exposes `PlanningResult`; `EvaluationRecord.planning` stores its serializable snapshot, including primary/final plans, trigger/reason, structured review evidence, recovery attempted/count, recovered plan, source (`primary` or `recovered`), sanitized error type, and reported recovery usage. Required-tool failures also retain this planning snapshot.

Ordinary requests keep their existing model-call count. A triggered review adds at most one tool-free model request (`max_turns=1`), even when it confirms no business work. A recovered lookup normally uses three model responses: primary routing, recovery, and answer synthesis. The required read still executes deterministically before synthesis, and the support model still has no tools.

Primary, recovery and support usage are counted once in production totals. The audit profiler labels recovery separately as `planning_recovery`; these tokens are not evaluation-judge usage. Recovery adds its instruction/schema/input tokens and structured output tokens, plus model latency. No live run or numeric token saving claim is part of this change. If a provider call fails without reporting usage, its unreported tokens cannot be recovered from SDK telemetry.

## Offline coverage

Tests cover recovery under fabricated eligibility/status results, tool suppression and conflicting authority claims; valid empty plans for examples, quotations, unsupported transactions, private disclosure and social engineering; irrelevant/fake-JSON tool references; ambiguity and clarification boundaries; one-call limits; malformed results and exceptions; ordinary authorization and privacy projection; minimal tool cover; production usage; and captured planning diagnostics.
