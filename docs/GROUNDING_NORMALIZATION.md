# Nullable fact grounding

`src/agentguard/grounding_normalization.py` centralizes typed fact comparison for
captured business outputs. This module is evaluation-only: production tools,
data-minimization policy, datasets and quality gates do not determine or change
its response interpretation.

## States and field semantics

`NormalizedFact` contains `field`, `state` and `value`.

| Evidence | State | Comparison behavior |
| --- | --- | --- |
| Explicit non-null scalar | present | Compare normalized values; preserve booleans distinctly from numbers |
| Explicit null on a declared nullable field | absent | Agree only with absence; contradict concrete values |
| Missing key, unsupported structure, or null without an absence contract | unknown | Insufficient evidence; never treat as authoritative absence |

`compare_facts()` returns `True` for agreement, `False` for contradiction, or
`None` for insufficient evidence. Unknown facts are not affirmative grounding
matches. Existing expected-authoritative-fact checks still fail if a required
field is missing, including when its expected value is null.

The nullable-field contracts are:

- `carrier`: no assigned carrier.
- `tracking_number`: no assigned tracking identifier.
- `delivered_at`: no recorded actual delivery date; this does not infer order status.
- `estimated_delivery`: no recorded delivery estimate; this does not infer whether delivery occurred.

False, zero and empty strings are not coerced to null. Other fields, such as
`status` or `eligible`, do not acquire absence semantics merely by containing null.

## Bounded response grammar

Response normalization recognizes explicit field labels and uses a shared grammar
for negative subjects, auxiliary negation and availability predicates. It covers
no-value, none/null, missing, unavailable and negated assignment/availability/
recording forms across the declared fields. It does not maintain per-scenario
phrases or a list of full-sentence exceptions. A contiguous coordinated subject
shares its predicate, so a denial covering both carrier and tracking normalizes
both fields to absent.

Separate assertions remain separate: a denial does not suppress a later concrete
tracking value. Concrete labeled values and explicit shipping-carrier assertions
retain contradiction checks. ISO dates and English month-name calendar dates
normalize to the same date while different dates remain contradictory. Actual
delivery dates are now included in extracted tool facts.

Questions, hypothetical language and explicit inability-to-confirm statements are
not factual assertions. Unrecognized predicates remain unknown rather than being
misread as tracking identifiers. This is a deterministic bounded grammar, not a
general natural-language judge. Unscoped claims across multiple orders are not
guessed onto a target; explicit order scopes and conjoined order clauses are kept
separate. General semantic evaluation remains independent.

## Integration and validation

Both functional and safety evaluation reach this comparison through the existing
captured-record grounding path. Existing status and eligibility checks remain in
place. Privacy evaluation still applies independently; absence agreement cannot
authorize a disclosure or override a privacy failure. No API calls or support-agent
executions occur here.

Offline tests cover state distinctions, both directions of absence contradictions,
coordinated denials, formatted dates, unknown evidence, order scope, existing
status/eligibility checks and independent privacy failures.
