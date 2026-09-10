import json
from pathlib import Path


DATA_FILE = Path(__file__).resolve().parents[3] / "data" / "orders.json"


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