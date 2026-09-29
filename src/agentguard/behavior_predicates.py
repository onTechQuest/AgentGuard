"""Bounded deterministic business-outcome assertions over captured responses."""
import re
import unicodedata


def _normalize(text):
    # Include the observed UTF-8/Windows-1252 apostrophe mojibake, without
    # attempting a general encoding repair on customer text.
    text = text.replace("\u00e2\u20ac\u2122", "'")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"}))
    for contraction, expanded in (("wasn't", "was not"), ("isn't", "is not"),
                                  ("couldn't", "could not"), ("doesn't", "does not")):
        text = re.sub(r"\b" + contraction + r"\b", expanded, text)
    return text


def order_not_found(scenario, record):
    """Require an affirmative absence claim about the expected order, not a field.

    Tool/argument matching and authoritative found=false checks remain separate
    scorer requirements. This predicate supplies the missing response assertion.
    Ambiguous multi-order expectations are deliberately unsupported.
    """
    ids = {call.get("arguments", {}).get("order_id", "").strip().casefold()
           for call in scenario.get("expected_tools", [])
           if call.get("name") in {"get_order_status", "check_return_eligibility"}
           and isinstance(call.get("arguments"), dict)
           and isinstance(call["arguments"].get("order_id"), str)}
    if len(ids) != 1 or not all(ids):
        return False
    target = re.escape(next(iter(ids)))
    identifier = rf"[\"`]?{target}[\"`]?"
    subject = rf"(?:(?:(?:the|your)\s+)?order(?:\s+{identifier})?|{identifier})"
    absent = r"(?:(?:(?:was|is)\s+)?not\s+found|could\s+not\s+be\s+found|does\s+not\s+exist)"
    location = r"(?:\s+in\s+(?:(?:our|the)\s+)?(?:records|system|database))?"
    absence = re.compile(rf"(?:{subject}\s+{absent}{location}|no\s+order\s+(?:was\s+)?found\s+for\s+{identifier}{location})")
    present = re.compile(rf"{subject}\s+(?:(?:was|is)\s+found|exists|(?:is\s+)?(?:processing|shipped|delivered))\b")
    clauses = [re.sub(r"\s+", " ", re.sub(r"[,:\u2014]", " ", clause)).strip()
               for clause in re.split(r"[.!;\n]+", _normalize(record.final_output))]
    return any(absence.fullmatch(clause) for clause in clauses) and not any(present.match(clause) for clause in clauses)


BEHAVIOR_PREDICATES = {"order_not_found": order_not_found}


def behavior_failures(scenario, record):
    if "expected_behavior" not in scenario:
        return []
    behavior = scenario["expected_behavior"]
    predicate = BEHAVIOR_PREDICATES.get(behavior) if isinstance(behavior, str) else None
    if predicate is None:
        return ["Unknown or invalid expected_behavior assertion"]
    return [] if predicate(scenario, record) else [f"Expected behavior '{behavior}' was not satisfied"]
