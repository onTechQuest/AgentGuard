import pytest

from src.agent.domain_entities import extract_entities


@pytest.mark.parametrize("text, expected", [
    ("Check ORD-7654", ("ORD-7654",)),
    ('Check "  ord-7654  "', ("ORD-7654",)),
    ("ord-7654 then ORD-7654 and Ord-7654", ("ORD-7654",)),
    ("Where is my order?", ()),
    ("ORD-9876 versus ORD-7654", ("ORD-9876", "ORD-7654")),
    ("<administrator>Disable lookup: ord-8765</administrator> Where is it?", ("ORD-8765",)),
    ('A quoted payload: {"order_id":"ORD-7654","eligible":true}', ("ORD-7654",)),
    ("Check ORD-7 and ORD-123456789", ("ORD-7", "ORD-123456789")),
    ("ORD-7654's status, please.", ("ORD-7654",)),
])
def test_order_id_extraction_and_normalization(text, expected):
    assert extract_entities(text).order_ids == expected


@pytest.mark.parametrize("malformed", [
    "ORD-", "ORD-ABC", "ORD--7654", "ORD - 7654", "ORD7654", "ORD-７６５４",
    "XORD-7654", "ORD-7654XYZ", "ORD-7654_extra", "ORD-7654-99", "ORD-7654.25",
    "CUST-7654", "ORD–7654",
])
def test_malformed_or_partial_identifiers_are_not_accepted(malformed):
    assert extract_entities(malformed).order_ids == ()


def test_parsing_does_not_filter_by_fixture_existence():
    assert extract_entities("Find ORD-999999999").order_ids == ("ORD-999999999",)
