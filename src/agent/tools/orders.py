import json
from datetime import date
from pathlib import Path


DATA_FILE = Path(__file__).resolve().parents[3] / "data" / "orders.json"
RETURN_EVALUATION_DATE = date(2026, 9, 10)
RETURN_WINDOW_DAYS = 30


def load_orders() -> list[dict]:
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_order_status(order_id: str) -> dict:
    normalized_order_id = order_id.strip().upper()

    for order in load_orders():
        if order["order_id"] == normalized_order_id:
            return {
                "found": True,
                "order": order,
            }

    return {
        "found": False,
        "error": f"Order {normalized_order_id} was not found.",
    }


def check_return_eligibility(order_id: str) -> dict:
    """Evaluate returns as of 2026-09-10, including day 30 after delivery."""
    result = get_order_status(order_id)
    if not result["found"]:
        return {**result, "eligible": False, "reason": result["error"]}

    order = result["order"]
    if order["status"] != "delivered":
        return {**result, "eligible": False, "reason": "Order has not been delivered."}

    try:
        delivered_at = date.fromisoformat(order["delivered_at"])
    except (KeyError, TypeError, ValueError):
        return {
            **result,
            "eligible": False,
            "reason": "Order has a missing or invalid delivery date.",
        }

    days_since_delivery = (RETURN_EVALUATION_DATE - delivered_at).days
    if days_since_delivery < 0:
        return {**result, "eligible": False, "reason": "Delivery date is in the future."}
    if days_since_delivery > RETURN_WINDOW_DAYS:
        return {
            **result,
            "eligible": False,
            "reason": "The 30-day return window has expired.",
        }

    return {
        **result,
        "eligible": True,
        "reason": "Order was delivered within the 30-day return window.",
    }
