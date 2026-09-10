"""Customer support agent backed by deterministic order tools."""

from dotenv import load_dotenv
from agents import Agent, Runner, function_tool

from src.agent.tools import orders

load_dotenv()


@function_tool
def get_order_status(order_id: str) -> dict:
    """Look up an order's status, shipping, and delivery information by order ID.

    Args:
        order_id: The customer's order ID, such as ORD-1001 (case-insensitive).
    """
    return orders.get_order_status(order_id)


@function_tool
def check_return_eligibility(order_id: str) -> dict:
    """Check whether an order can be returned, with a reason, as of 2026-09-10.

    Args:
        order_id: The customer's order ID, such as ORD-1003 (case-insensitive).
    """
    return orders.check_return_eligibility(order_id)


support_agent = Agent(
    name="AgentGuard Support Agent",
    instructions=(
        "Help customers with order status and return eligibility. "
        "Use the available tools whenever answering order-specific questions. "
        "Ask for the order ID if the customer has not provided one. "
        "Never invent order status, shipping, delivery, or eligibility information. "
        "If an order does not exist, clearly say so. "
        "Explain the reason returned by the tool when an order is not eligible. "
        "Return eligibility is evaluated as of the fixed date 2026-09-10. "
        "Do not claim an action was performed unless a tool actually performed it. "
        "These tools only look up information; they cannot initiate returns, "
        "issue refunds, or modify orders."
    ),
    tools=[get_order_status, check_return_eligibility],
)


def run_support_agent(user_message: str) -> str:
    """Run the support agent and return its final text response."""
    result = Runner.run_sync(support_agent, user_message)
    return result.final_output


if __name__ == "__main__":
    user_message = input("Ask about your order: ").strip()
    if user_message:
        print(run_support_agent(user_message))
