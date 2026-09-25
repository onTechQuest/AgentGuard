# Phase 1: production context optimization

The eight functional smoke executions averaged **1,205.88 production tokens**, down
from **2,233.38** (46.0%). This meets the unchanged 1,500-token threshold with
294.12 tokens of average headroom. Both components still resolve to the installed
SDK default, `gpt-5.6-luna`; no model or output-token settings changed.

The semantic router, deterministic policy resolver, constrained support agent,
authoritative tools, privacy projection and evaluation accounting remain separate.
There is no bypass, fusion, keyword routing, cache or new model call.

## Router field audit

The model emits `CapabilityPlanOutput`. Runtime code expands it into the existing
`RequestPlan` interface. The older `SemanticRoutingAnalysis` input remains supported
only for explicitly injected routers; the live output contract rejects that format.

| Previous field | Downstream use | Phase 1 representation |
| --- | --- | --- |
| `business_capabilities` | Capability/policy summary | Removed from model output; unique identifiers derived from capability requests. An empty request list remains empty for authorization. |
| `business_intent_ambiguous` | Previously marked every component uncertain | Removed from model output; uncertainty belongs to each request's `needs_clarification`. Every uncertain component is still withheld; independent clear components remain executable. |
| `entity_scope` | Legacy whole-request multi-entity fallback | Removed from model output; explicit capability/order bindings determine targets. Legacy injected routers retain their original ambiguity checks. |
| `capability_requests` | Authoritative input to per-target policy resolution | Required list; preserved capability ID, nullable order ID and strict clarification boolean. |
| `confidence` | Runtime 0.80 authorization threshold | Retained with the same numeric validation. |
| `control_signals` | Policy/diagnostic view of control attempts | Retained typed enum list, independent of business intent. The support prompt always states injection handling, so no repeated conditional prose is needed. |
| `denied_disclosures` | Presence enables the support refusal instruction | Simplified to disclosure-category enum list. Internal denial objects are reconstructed with no target. |
| Disclosure `target` | No authorization or execution consumer | Removed from model output. Disclosure targets never grant lookups; business role binding is still explicit. |

Capability identifiers resolve against the existing registry. The model does not
generate tool authority, permitted fields or business rules. The catalog still
comes directly from registry descriptions and supports registry extensions.
Parsed entity IDs remain deterministic, and generated bindings must reference
those IDs. Required fields and extra-field rejection remain enforced by the SDK's
strict [structured-output schema](https://developers.openai.com/api/docs/guides/structured-outputs).

## Context removed or consolidated

| Context | Before characters | After characters |
| --- | ---: | ---: |
| Router instructions including catalog | 3,179 | 1,521 |
| Router output schema | 2,256 | 1,412 |
| Support base instructions | 1,942 | 761 |
| Support instructions for smoke requests | 2,370–2,400 | 833–841 |

Character counts describe serialized context size, not tokenizer attribution.

The router contract consolidates repeated explanations of entity roles, disclosure
denial, control attempts and unsupported actions. Capability descriptions remain
in one registry-derived catalog. Global summaries no longer need to be generated
alongside per-request information.

The support agent receives a compact list of authorized `{tool, order_id}` bindings.
The runtime still enforces both dimensions. Capability lists and disclosure-field
contracts are not repeated in that instruction payload because the resolver and
tool projection enforce them. Parsed candidate IDs remain in the original user
input rather than a second instruction-level list; authorized normalized IDs are
present in the bindings. Conditional refusal and clarification instructions remain.

The support execution contract preserves required lookups before answering,
grounding, rejection of fabricated/overridden tool results, privacy refusal while
completing permitted inquiries, missing-order handling, eligibility explanations,
read-only action limits and clarification instead of guessing. Tool descriptions
are shorter; the eligibility tool still states the fixed evaluation date.

Privacy projection and business tools are unchanged. Projected output contains
registry-approved scalar facts and the public order envelope, including nulls,
delivery dates, eligibility and reasons. Order context within the eligibility
result supports the existing single-tool combined inquiry. Missing-order `error`
and eligibility `reason` can duplicate text, but both have established business
and evaluation consumers; neither was removed for a marginal saving.

## Measured comparison

Average tokens per scenario:

| Component | Before input | After input | Before output | After output | Before total | After total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Router | 953.25 | 539.00 | 79.50 | 56.38 | 1,032.75 | 595.38 |
| Support | 1,144.88 | 554.63 | 55.75 | 55.88 | 1,200.63 | 610.50 |
| Combined | 2,098.13 | 1,093.63 | 135.25 | 112.25 | 2,233.38 | 1,205.88 |

Each run still uses one router response and two support responses. Across eight
scenarios: 24 SDK-counted requests before and after, with one authoritative tool
call per scenario. Tool names, order-ID arguments and call ordering match exactly.
Component input, output, total and request counts reconcile with EvaluationRecord.

| Scenario | Before tokens | After tokens | Before latency ms | After latency ms |
| --- | ---: | ---: | ---: | ---: |
| tool_routing_001 | 2,347 | 1,309 | 4,446 | 4,232 |
| return_001 | 2,267 | 1,241 | 9,887 | 3,988 |
| return_002 | 2,259 | 1,233 | 7,037 | 5,221 |
| order_status_001 | 2,217 | 1,190 | 6,229 | 6,981 |
| grounded_response_001 | 2,214 | 1,186 | 3,847 | 4,072 |
| order_status_003 | 2,206 | 1,180 | 3,922 | 3,909 |
| order_status_002 | 2,207 | 1,175 | 4,088 | 5,068 |
| missing_order_001 | 2,150 | 1,133 | 4,408 | 4,909 |

Average latency: **5,483 → 4,798 ms**. P95 using the existing nearest-rank formula:
**9,887 → 6,981 ms**. These are two small sequential samples, not proof of a stable
latency improvement. Transport retries and any missing retry-token usage remain
unavailable in the diagnostic; production and evaluator accounting are unchanged.

## Validation and artifacts

Files changed for this phase:

- `src/agent/capability_router.py`
- `src/agent/support_agent.py`
- `scripts/audit_token_usage.py`
- `tests/test_capability_router.py`
- `tests/test_runtime_tool_routing.py`
- `tests/test_privacy_runtime.py`
- `tests/test_audit_token_usage.py`
- `tests/test_compact_routing.py` (new)
- `docs/TOKEN_OPTIMIZATION_PHASE1.md` (new)

- `python -m pytest tests -q`: **1,128 passed**.
- The eight saved after-optimization records passed deterministic functional,
  tool and argument checks, with no deterministic grounding failures.
- No semantic/safety judges or safety-dataset executions ran in this diagnostic.
  Their previous live 100% rates have not been re-established for the new prompts.
  Offline policy, injection, privacy, null-grounding and action regressions passed.
- Added compact-contract coverage for multiple capabilities, per-target roles,
  ambiguous and partially clear requests, low confidence, disclosure-only requests,
  controls, fabricated entities, schema strictness and single classifier invocation.

Generated artifacts are gitignored:

- `reports/token_audit_smoke.json`: preserved before profile.
- `reports/token_audit_smoke_optimized.json`: after profile, including captured
  EvaluationRecords for further evaluation without rerunning the support agent.
- `reports/token_optimization_comparison.json`: component/scenario comparison and
  deterministic results from those same records.

The audit script was extended only to persist EvaluationRecord. It still executes
each requested scenario once and does not invoke judges. Read report JSON with
explicit UTF-8 encoding on Windows to preserve response punctuation.

## Further work, not implemented

The token target is met, so no architectural change is needed for this gate.
Next validation should assess live semantic and safety behavior before a release.
Further prompt/schema reductions would need the same regression coverage; output
truncation is unnecessary. Model changes, caching, router bypass or fusion remain
outside this phase. Caching alone would not lower the current raw-token metric.
