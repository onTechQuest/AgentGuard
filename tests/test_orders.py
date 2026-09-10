from src.agent.tools.orders import get_order_status


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