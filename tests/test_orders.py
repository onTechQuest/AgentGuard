import pytest

from src.agent.tools import orders
from src.agent.tools.orders import check_return_eligibility, get_order_status


def test_get_existing_order():
    result = get_order_status("ORD-1001")

    assert result["found"] is True
    assert result["order"]["order_id"] == "ORD-1001"
    assert result["order"]["status"] == "shipped"


def test_order_id_is_case_insensitive():
    result = get_order_status("ord-1002")

    assert result["found"] is True
    assert result["order"]["order_id"] == "ORD-1002"


def test_unknown_order():
    result = get_order_status("ORD-9999")

    assert result["found"] is False
    assert "not found" in result["error"].lower()


def test_delivered_order_is_eligible_for_return():
    result = check_return_eligibility("ORD-1003")

    assert result["found"] is True
    assert result["eligible"] is True
    assert result["order"]["order_id"] == "ORD-1003"


@pytest.mark.parametrize("order_id", ["ORD-1001", "ORD-1002"])
def test_non_delivered_order_is_not_eligible_for_return(order_id):
    result = check_return_eligibility(order_id)

    assert result["found"] is True
    assert result["eligible"] is False
    assert "not been delivered" in result["reason"]


def test_unknown_order_is_not_eligible_for_return():
    result = check_return_eligibility("ORD-9999")

    assert result["found"] is False
    assert result["eligible"] is False
    assert "not found" in result["reason"]


def test_return_order_id_is_case_insensitive():
    assert check_return_eligibility(" ord-1003 ") == check_return_eligibility("ORD-1003")


@pytest.mark.parametrize(
    ("delivered_at", "eligible", "reason"),
    [
        ("2026-09-10", True, "within"),
        ("2026-08-11", True, "within"),
        ("2026-08-10", False, "expired"),
        ("2026-09-11", False, "future"),
        (None, False, "missing or invalid"),
        ("invalid-date", False, "missing or invalid"),
    ],
)
def test_return_delivery_dates(monkeypatch, delivered_at, eligible, reason):
    monkeypatch.setattr(
        orders,
        "load_orders",
        lambda: [{"order_id": "ORD-TEST", "status": "delivered", "delivered_at": delivered_at}],
    )

    result = check_return_eligibility("ORD-TEST")

    assert result["eligible"] is eligible
    assert reason in result["reason"]


def test_missing_delivery_date_is_not_eligible(monkeypatch):
    monkeypatch.setattr(
        orders,
        "load_orders",
        lambda: [{"order_id": "ORD-TEST", "status": "delivered"}],
    )

    result = check_return_eligibility("ORD-TEST")

    assert result["eligible"] is False
    assert "missing or invalid" in result["reason"]


@pytest.mark.parametrize("order_id, delivered_at, age, eligible", [
    ("ORD-1030", "2026-08-11", 30, True),
    ("ORD-1031", "2026-08-10", 31, False),
])
def test_persistent_return_boundary_fixtures(order_id, delivered_at, age, eligible):
    from datetime import date

    result = check_return_eligibility(order_id)
    assert result["found"] is True
    assert result["order"]["status"] == "delivered"
    assert result["order"]["delivered_at"] == delivered_at
    assert (orders.RETURN_EVALUATION_DATE - date.fromisoformat(delivered_at)).days == age
    assert result["eligible"] is eligible
    assert ("within" if eligible else "expired") in result["reason"]


def test_fixture_ids_are_unique_and_original_orders_remain():
    fixtures = orders.load_orders()
    assert len({order["order_id"] for order in fixtures}) == len(fixtures)
    assert {order["order_id"] for order in fixtures} == {"ORD-1001", "ORD-1002", "ORD-1003", "ORD-1030", "ORD-1031"}
