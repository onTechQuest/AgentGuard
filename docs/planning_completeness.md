# Planning completeness before authorization

The primary semantic router is unchanged. The production sequence now inserts `validate_planning()` between its result and `resolve_request_policy()`. The execution-obligation implementation is unchanged.

## Invariant and review boundary

An explicit empty plan with recognized targets, control signals and no known ambiguity cannot silently pass to authorization without one bounded semantic completeness review. Review evidence is not authorization. Any proposed binding still needs a registered supported capability, a parsed target, and normal runtime policy approval.

Milestone 13D.4D also reviews a nonempty plan when every binding is a supported, recognized-target read below the existing 0.80 confidence threshold, all bindings describe one capability/target pair, and exactly one target was extracted. This is evidence of potentially incomplete semantic planning, not permission to execute. No phrase, scenario ID, or dataset expectation triggers review.

Bindings are assessed as `ACTIONABLE`, `REVIEWABLE_NON_ACTIONABLE`, `LEGITIMATE_CLARIFICATION`, `UNSUPPORTED`, or `UNKNOWN`, with per-binding reasons. Confident explicit clarification, missing targets, multiple targets, competing capabilities, unsupported/unknown work, legacy plans, and already actionable work do not enter the new review path. Low-confidence genuine ambiguity may be reviewed, but the reviewer must return its own confidence and clarification state; nothing is automatically cleared or promoted. Empty-plan control-signal recovery keeps its previous eligibility rules.

Controls plus entities identify a conservative candidate for review, not proof of a legitimate task. Exact registered tool/capability references and their registry authority relationships are provided as additional evidence. No natural-language phrase rules classify return eligibility or order status. An entity or a tool name alone never forces a lookup.

## One semantic recovery call

Both review paths remain tool-free and use the same model. Existing empty-plan review retains its omitted-work instructions and `RecoveryPlan` schema with only `capability_requests`. Milestone 13D.4G gives binding-actionability review separate instructions: independently reconsider whether the primary confidence, clarification requirement, capability selection and target binding are justified. The primary plan is a hypothesis; agreement remains valid when supported by the request.

`ActionabilityRecoveryPlan` requires `capability_requests`, a strictly validated `confidence` between zero and one, and an explicit boolean `needs_clarification` on every binding. Missing semantic fields fail both local and SDK validation; no default clarification flag is supplied. Additional fields are forbidden. Neither schema can emit grants or disclosure permissions.

Actionability confidence means the reviewer's independent confidence that the proposed capability/target interpretation reflects the user's business intent. It does not measure tool success, order existence, final-answer correctness or a desire to authorize. The value is never clamped or promoted. Instructions prohibit increasing confidence merely to cross a policy threshold and do not disclose the numeric authorization threshold.

Clarification is required for material semantic ambiguity, including multiple plausible targets, competing capabilities, a missing target or genuinely ambiguous intent. Primary uncertainty alone does not require preserving uncertainty: the reviewer may remove it only when the request and recognized target make the business intent sufficiently clear. It may instead preserve uncertainty or return no bindings. The existing conservative review eligibility and deterministic scope checks remain unchanged.

The review distinguishes live verification requests from hypothetical examples, quotations, documentation, mere references, disclosure-only requests and prohibited transactions. It can return an empty list; that confirms an empty plan without a tool call. This distinction is semantic, so mocked offline tests validate the boundaries and orchestration, not real-model classification accuracy.

The validator checks every recovered capability against the shared registry and every target against the original extracted entities. Binding review additionally forbids expanding beyond the original capability/target pair. Control signals, disclosure decisions and extracted entities survive unchanged. The empty-plan path preserves primary confidence; the new path uses the reviewer's explicit confidence and clarification result. Unresolved ambiguity or a recovered confidence below 0.80 still yields no grant. An empty recovery yields no work. Normal policy remains the sole authority for minimum tool coverage, confidence, target and privacy restrictions.

There is no recursive replan or application retry. A malformed recovery, invalid capability/target, or recovery exception produces `PlanningCompletenessError` before authorization and synthesis. Error telemetry omits exception payloads. Existing release handling rejects the captured error instead of judging an invented response.

## Telemetry and cost

`RunResult.context_wrapper.context.planning` exposes `PlanningResult`; `EvaluationRecord.planning` stores its serializable snapshot, including primary/final plans, trigger/reason, structured review evidence, recovery attempted/count, recovered plan, source (`primary` or `recovered`), sanitized error type, and reported recovery usage. Required-tool failures also retain this planning snapshot.

Decision evidence identifies `review_type` (`omitted_work` or `binding_actionability`), primary/recovered confidence and clarification, and `scope_preservation` (`NOT_CHECKED`, `PRESERVED`, or `REJECTED`). Schema-valid output is retained even when scope validation rejects it; it never replaces the primary plan in that case. `semantic_state_source=RECOVERY_OUTPUT` identifies explicitly returned semantic state, not a verified improvement in judgment. Policy results and required operations remain separate downstream evidence.

Ordinary actionable requests keep their existing model-call count. A triggered review adds at most one tool-free model request (`max_turns=1`), even when it confirms no business work. The same recovery admission, request deadline and downstream synthesis reserve apply; this is not a retry. A recovered lookup normally uses three model responses: primary routing, recovery, and answer synthesis. The required read still executes deterministically before synthesis, and the support model still has no tools.

Primary, recovery and support usage are counted once in production totals. The audit profiler labels recovery separately as `planning_recovery`; these tokens are not evaluation-judge usage. Recovery adds its instruction/schema/input tokens and structured output tokens, plus model latency. No live run or numeric token saving claim is part of this change. If a provider call fails without reporting usage, its unreported tokens cannot be recovered from SDK telemetry.

## Offline coverage

Tests cover recovery under fabricated eligibility/status results, tool suppression and conflicting authority claims; valid empty plans for examples, quotations, unsupported transactions, private disclosure and social engineering; irrelevant/fake-JSON tool references; ambiguity and clarification boundaries; one-call limits; malformed results and exceptions; ordinary authorization and privacy projection; minimal tool cover; production usage; and captured planning diagnostics.
