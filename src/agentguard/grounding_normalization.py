"""Typed facts and bounded, field-aware normalization for captured business data.

This is evaluation logic only. Missing evidence is never converted to business
absence, and text outside the claim grammar is not guessed into a concrete fact.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from types import MappingProxyType
from typing import Literal


@dataclass(frozen=True)
class FieldContract:
    label_pattern: str
    kind: Literal["text", "date"]
    null_meaning: str


NULLABLE_FIELDS = MappingProxyType({
    "estimated_delivery": FieldContract(
        r"estimated(?:_|\s+)delivery(?:\s+date)?|estimated to arrive on", "date", "No delivery estimate is recorded.",
    ),
    "delivered_at": FieldContract(
        r"delivered_at|delivery date|delivered on", "date", "No actual delivery date is recorded; does not establish order status.",
    ),
    "tracking_number": FieldContract(r"tracking(?:_number|\s+number)?", "text", "No tracking identifier is assigned."),
    "carrier": FieldContract(r"carrier", "text", "No carrier is assigned."),
})


@dataclass(frozen=True)
class NormalizedFact:
    field: str
    state: Literal["present", "absent", "unknown"]
    value: str | int | float | bool | None = None


@dataclass(frozen=True)
class FieldClaim:
    fact: NormalizedFact
    order_id: str | None


_MISSING = object()
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE = re.compile(rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}})\b", re.I)


def _value(field: str, value):
    if not isinstance(value, str):
        return value
    value = value.strip()
    if field in NULLABLE_FIELDS and NULLABLE_FIELDS[field].kind == "date":
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%d %B %Y"):
            try:
                return datetime.strptime(value.replace(",", ""), fmt).date().isoformat()
            except ValueError:
                pass
    return value.casefold()


def normalize_fact(field: str, facts: Mapping) -> NormalizedFact:
    value = facts.get(field, _MISSING)
    if value is _MISSING:
        return NormalizedFact(field, "unknown")
    if value is None:
        # Null interpretation is a domain contract, not Python truthiness.
        return NormalizedFact(field, "absent" if field in NULLABLE_FIELDS else "unknown")
    if type(value) not in (str, bool, int, float):
        return NormalizedFact(field, "unknown")
    return NormalizedFact(field, "present", _value(field, value))


def compare_facts(authoritative: NormalizedFact, response: NormalizedFact) -> bool | None:
    """True = agreement, False = contradiction, None = insufficient evidence."""
    if authoritative.field != response.field:
        raise ValueError("Grounding comparison requires the same field")
    if "unknown" in (authoritative.state, response.state):
        return None
    if authoritative.state != response.state:
        return False
    if authoritative.state == "absent":
        return True
    if isinstance(authoritative.value, bool) or isinstance(response.value, bool):
        return type(authoritative.value) is type(response.value) and authoritative.value == response.value
    return authoritative.value == response.value


_LABEL = "(?:" + "|".join(f"(?P<{field}>{contract.label_pattern})" for field, contract in NULLABLE_FIELDS.items()) + ")"
_LABEL_RE = re.compile(r"\b" + _LABEL + r"\b", re.I)
_COORDINATION = re.compile(r"\s*(?:,\s*(?:(?:and|or|nor)\s+)?|(?:and|or|nor)\s+)\s*", re.I)
_AUX = r"(?:is|are|was|were|has|have|had|been|be|yet|currently|already)"
# Shared grammatical forms across all nullable fields, not full-sentence exceptions.
_ABSENT = re.compile(
    rf"^(?:{_AUX}\s+)*(?:(?:none|null|missing|unavailable)\b|"
    rf"not\s+(?:{_AUX}\s+)*(?:assigned|available|provided|recorded|set)\b|no\s+value\b)", re.I,
)
_NO_SUBJECT = re.compile(r"\b(?:no|neither|without)(?:\s+(?:a|an|any))?\s*$", re.I)
_UNKNOWN = re.compile(rf"^(?:{_AUX}\s+)*(?:unknown|uncertain|not\s+(?:yet\s+)?known)\b", re.I)
_VALUE_LINK = re.compile(r"^\s*(?::|=|#|\bis\b)\s*", re.I)
_NONASSERTION = re.compile(
    r"^\s*(?:if|whether|suppose|pretend)\b|"
    r"\b(?:cannot|can not|could not|do not|will not|would not|unable to|refuse to)\s+"
    r"(?:say|confirm|claim|assert|pretend|verify|determine)\b", re.I,
)
_GRAMMAR_WORDS = frozenset({"not", "no", "none", "null", "missing", "unavailable", "unknown", "uncertain",
                            "assigned", "available", "provided", "recorded", "set", "yet", "pending", "being"})


def _text(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    # Normalize auxiliary negation without enumerating absence sentences.
    text = re.sub(r"\b(is|are|was|were|has|have|had|could|would|should|do|does|did)n['’]t\b", r"\1 not", text, flags=re.I)
    text = re.sub(r"\bcan't\b", "cannot", text, flags=re.I)
    return re.sub(r"[*`]", "", text)


def _claim(field: str, label: str, before: str, after: str) -> NormalizedFact:
    predicate = after.lstrip(" :=#\t")
    if _NO_SUBJECT.search(before) or _ABSENT.match(predicate):
        return NormalizedFact(field, "absent")
    if _UNKNOWN.match(predicate):
        return NormalizedFact(field, "unknown")
    link = _VALUE_LINK.match(after)
    implicit_date = label.casefold().endswith(" on")
    if not link and not implicit_date:
        return NormalizedFact(field, "unknown")
    value_text = after[link.end():] if link else after.lstrip()
    if NULLABLE_FIELDS[field].kind == "date":
        match = _DATE.match(value_text)
    else:
        match = re.match(r"[a-z0-9][a-z0-9-]*\b", value_text, re.I)
    if not match or match[0].casefold() in _GRAMMAR_WORDS:
        return NormalizedFact(field, "unknown")
    return NormalizedFact(field, "present", _value(field, match[0]))


def _assertion_segments(output: str):
    for sentence in re.finditer(r"([^.!?;\n]+)([.!?;\n]|$)", _text(output)):
        if sentence[2] == "?":
            continue
        for segment in re.split(
            r"\b(?:but|however|although|because)\b|"
            r"\b(?:and|or)\b(?=\s+ord-\d+\b)", sentence[1], flags=re.I,
        ):
            if not _NONASSERTION.search(segment):
                yield segment


def normalize_response_claims(output: str) -> list[FieldClaim]:
    """Normalize explicit field assertions, including coordinated absence subjects.

    Labels delimit field scope; a shared predicate applies only across a contiguous
    coordination of labels. Negation cannot leak into a subsequent assertion.
    Questions, hypothetical statements and epistemic disclaimers are not facts.
    """
    claims = []
    # Keep commas (calendar dates) and coordinated subjects intact.
    for segment in _assertion_segments(output):
        ids = list(dict.fromkeys(re.findall(r"\bord-\d+\b", segment.casefold())))
        # Multiple subjects without separate clauses are outside this bounded grammar.
        if len(ids) > 1:
            continue
        order_id = ids[0] if ids else None
        labels = list(_LABEL_RE.finditer(segment))
        index = 0
        while index < len(labels):
            end = index
            while end + 1 < len(labels) and _COORDINATION.fullmatch(segment[labels[end].end():labels[end + 1].start()]):
                end += 1
            before = segment[:labels[index].start()]
            limit = labels[end + 1].start() if end + 1 < len(labels) else len(segment)
            after = segment[labels[end].end():limit]
            for match in labels[index:end + 1]:
                claims.append(FieldClaim(_claim(match.lastgroup, match[0], before, after), order_id))
            index = end + 1
        # Preserve explicit shipping-carrier assertions without requiring a label.
        for match in re.finditer(r"\b(?:shipped|delivered)\s+(?:via|by|with)\s+(ups|fedex|dhl|usps)\b", segment, re.I):
            if not re.search(r"\b(?:not|never)\s+(?:been\s+)?$", segment[:match.start()], re.I):
                claims.append(FieldClaim(NormalizedFact("carrier", "present", match[1].casefold()), order_id))
    return claims


def nullable_fact_comparisons(output: str, captured: list[dict]):
    """Yield scoped evidence and comparisons for both agreement and contradiction."""
    by_order = {}
    for facts in captured:
        order_id = facts.get("order_id")
        key = order_id.casefold() if isinstance(order_id, str) else None
        by_order.setdefault(key, {}).update(facts)
    for claim in normalize_response_claims(output):
        facts = by_order.get(claim.order_id)
        if facts is None and len(by_order) == 1 and (claim.order_id is None or None in by_order):
            facts = next(iter(by_order.values()))
        if facts is not None:
            yield facts, claim.fact, compare_facts(normalize_fact(claim.fact.field, facts), claim.fact)


def nullable_fact_contradictions(output: str, captured: list[dict]) -> list[str]:
    failures = [f"Response {claim.field} contradicts captured tool output."
                for _, claim, agrees in nullable_fact_comparisons(output, captured) if agrees is False]
    return list(dict.fromkeys(failures))
