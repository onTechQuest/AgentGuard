"""Print a demo evaluation record: python scripts/inspect_evaluation_record.py."""

from pathlib import Path
from pprint import pprint
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentguard.evaluation_record import execute_scenario


def main() -> None:
    scenario = {"id": "demo_001", "input": "Where is order ORD-1001?"}
    record = execute_scenario(scenario)

    print("=== Evaluation record ===")
    print(f"Scenario ID: {record.scenario_id}")
    print(f"Input: {record.input}")
    print(f"\nFinal output:\n{record.final_output}")
    print("\nTool calls:")
    pprint(record.tool_calls, width=100, sort_dicts=False)
    print(f"\nlatency_ms: {record.latency_ms:.2f}")
    print(f"request_count: {record.request_count}")
    print(f"input_tokens: {record.input_tokens}")
    print(f"output_tokens: {record.output_tokens}")
    print(f"total_tokens: {record.total_tokens}")


if __name__ == "__main__":
    main()
