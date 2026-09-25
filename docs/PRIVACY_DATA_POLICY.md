# Privacy and data minimization (Milestone 12C)

Business capability authorization, entity binding, tool authorization and data
disclosure authorization are separate runtime decisions. A denied disclosure
request does not revoke an independently authorized business lookup.

## Planning and partial fulfillment

The tool-free semantic router returns `capability_requests` and
`denied_disclosures` alongside business ambiguity, confidence and control signals.
The resulting `RequestPlan` includes the original deterministically extracted
order IDs. Each `CapabilityRequest` contains a capability, an order ID (or null)
and a component clarification flag. The router and deterministic policy validate
every bound ID against the extracted list.

For example, an order-status component bound to ORD-1001 can coexist with a denied
customer-data component referring to ORD-1003. The second identifier is retained
as parsed data but gets no tool grant merely because it was mentioned. A request
with only private-data components receives no business tools. Denied disclosure
metadata has fixed categories; its free-text target is never promoted into agent
instructions or used to authorize tools.

`resolve_request_policy()` groups clear components by target and runs the existing
minimum authoritative cover for each target. Status plus return eligibility for
the same order therefore still needs only `check_return_eligibility`. Distinct
roles on distinct orders produce separate grants, not a capability/ID cross-product.
A missing target or uncertain component requests clarification while independent
clear components proceed. A globally uncertain business interpretation or plan
confidence below 0.80 grants no tools. No order is selected by position in the text.

The support prompt directs the agent to refuse disallowed disclosure and perform
authorized lookups in the same response. One router run and one support-agent run
remain the execution architecture. Legacy injected routers without component
bindings retain the earlier conservative all-target/ambiguity behavior.

## Central data contracts

`src/agentguard/tool_policy.py` owns tool authority and possible internal fields,
plus each capability's `exposed_fields`. Required facts must be both provided by
the selected tool and permitted by that capability's disclosure contract.

| Capability | Agent-visible fields |
| --- | --- |
| order_status | found, order_id, status, carrier, tracking_number, estimated_delivery, delivered_at, error |
| return_eligibility | The operational fields above, plus eligible and reason |
| combined status/return | Union of those contracts; no additional internal fields |

The existing `order` envelope is preserved. Fields appear only where the underlying
result supplied them; null values remain null. Return results retain operational
order context to cover supported combined questions without a second lookup.
Neither `customer_id` nor `total` is exposed. New undeclared fields are excluded by
default. Claimed administrator/support authority does not change these contracts.

`project_tool_result()` makes a new result containing allowed scalar values and
the projected order envelope. Arbitrary nested values under allowed field names
are excluded. It does not recompute eligibility, rewrite error/reason strings,
normalize absent/null values, read fixtures, or modify internal business records.

## SDK boundary

The per-request SDK function wrapper checks the exact `(tool, order_id)` grant
before executing the backing business function. An unauthorized argument yields a
generic scope error without running the function. An authorized result is projected
inside the wrapper before returning to the SDK. The response model and captured
tool-output trajectory therefore receive only projected results, including when
the backing function returned a rich internal record.

The installed SDK supports this through the public `function_tool` boundary;
no SDK limitation or post-response filtering is involved. The static tool wrappers
also project results, and the global support-agent template still has no tools.

Underlying repository functions and their deterministic return contracts are
unchanged, including internal customer IDs and totals. Runtime modules do not
import datasets or evaluators. Privacy assertions, required-tool checks and release
gates remain independent and unchanged. Injection trajectory reconciliation and
null/absence grounding normalization are outside this change.

## Validation

Offline tests mock semantic planning and use a scripted SDK model with tracing
disabled. They verify partial fulfillment, entity roles, ambiguity, target
authorization, least-privilege fields, rich-result immutability, missing orders,
nullable operational values, minimum tool sets, and actual model-visible tool
results. This validates the architecture and boundary; it does not measure live
semantic planning or answer-generation accuracy. No live evaluations are run.
