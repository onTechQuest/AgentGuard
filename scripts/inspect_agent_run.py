"""Temporary diagnostic: python scripts/inspect_agent_run.py."""

from pathlib import Path
from pprint import pprint
import sys

# Allow direct execution from any working directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.support_agent import run_support_agent_detailed


def main() -> None:
    result = run_support_agent_detailed("Where is order ORD-1001?")

    print("=== Final output ===")
    print(result.final_output)

    for index, item in enumerate(result.new_items, start=1):
        print(f"\n=== Item {index}: {type(item).__name__} ===")
        # Public fields/properties verified against the installed SDK. Avoid
        # dumping the agent, model, or client, which may hold configuration.
        for name in ("type", "tool_name", "call_id", "description", "title", "output"):
            if hasattr(item, name):
                print(f"{name}:")
                pprint(getattr(item, name), width=100, sort_dicts=False)

        if hasattr(item, "raw_item"):
            raw_item = item.raw_item
            print(f"raw_item ({type(raw_item).__name__}):")
            # SDK raw items can be Pydantic response objects or dictionaries.
            if callable(getattr(raw_item, "model_dump", None)):
                raw_item = raw_item.model_dump()
            pprint(raw_item, width=100, sort_dicts=False)


if __name__ == "__main__":
    main()
