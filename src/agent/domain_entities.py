"""Parse business identifiers without interpreting instructions or business facts."""

from dataclasses import dataclass
import re


# Public order-ID syntax: ASCII ORD- followed by one or more ASCII digits.
# Reject fragments within larger identifiers, hyphenated tokens or decimals.
# This validates syntax, not existence; only a business tool can establish that.
_ORDER_ID = re.compile(r"(?<![\w-])ORD-[0-9]+(?![\w-]|\.[0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedEntities:
    order_ids: tuple[str, ...]


def extract_entities(text: str) -> ExtractedEntities:
    """Normalize and deduplicate in first-occurrence order across the entire input.

    Quoting and claimed control authority do not affect lexical extraction. No
    identifier is selected as the target here, and no order fixtures are read.
    """
    return ExtractedEntities(tuple(dict.fromkeys(match.group().upper() for match in _ORDER_ID.finditer(text))))
