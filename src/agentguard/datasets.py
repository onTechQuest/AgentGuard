"""Validated evaluation datasets and stable smoke/full suite selection.

This module only reads local JSON. It never executes scenarios or initializes
evaluators. Full suites include smoke scenarios once, in their original order.
"""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from src.agentguard.tool_policy import ACTION_REGISTRY, TOOL_REGISTRY


DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[2] / "evals" / "datasets"
SUITES = ("smoke", "full")
RISK_LEVELS = ("critical", "high", "medium", "low")
TEST_INTENTS = (
    "baseline", "paraphrase", "boundary", "negative", "ambiguity", "adversarial",
    "false_positive_control", "false_negative_control",
)
SUPPORTED_TOOLS = tuple(tool.name for tool in TOOL_REGISTRY)
PROHIBITED_ACTIONS = tuple(name for name, action in ACTION_REGISTRY.items() if action.writes_state)
INJECTION_LABELS = ("resisted", "partially_followed", "followed_injection")
CATEGORIES = {
    "functional": frozenset({
        "order_status", "return_eligibility", "invalid_input", "missing_order",
        "ambiguity", "tool_routing", "grounded_response",
    }),
    "safety": frozenset({
        "prompt_injection", "tool_override", "unsupported_action",
        "data_protection", "conflicting_claim", "safety_control", "social_engineering",
    }),
}


class DatasetValidationError(ValueError):
    """Invalid dataset structure or metadata, safe to show without scenario text."""


@dataclass
class EvaluationDatasets:
    functional: list[dict]
    safety: list[dict]


def _validate_suite(suite: str) -> None:
    if suite not in SUITES:
        raise DatasetValidationError("Unsupported suite; choose smoke or full.")


def _select(scenarios: list[dict], suite: str) -> list[dict]:
    return [scenario for scenario in scenarios if suite == "full" or scenario["tier"] == "smoke"]


def _string_list(value, field: str, location: str, *, allowed=None, nonempty=False) -> None:
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise DatasetValidationError(f"{location}: {field} must be a {'nonempty ' if nonempty else ''}list of nonempty strings.")
    if len(value) != len(set(value)):
        raise DatasetValidationError(f"{location}: {field} contains duplicates.")
    if allowed is not None and any(item not in allowed for item in value):
        raise DatasetValidationError(f"{location}: {field} contains unsupported values.")


def _validate_facts(value, location: str) -> None:
    states = [value] if isinstance(value, dict) else value
    if not isinstance(states, list) or not states:
        raise DatasetValidationError(f"{location}: expected_authoritative_facts must be a nonempty mapping or list of mappings.")
    for state in states:
        prefix = f"{location}: expected_authoritative_facts"
        if not isinstance(state, dict):
            raise DatasetValidationError(f"{prefix} entries must be mappings.")
        tool = state.get("tool")
        if tool not in SUPPORTED_TOOLS or not isinstance(state.get("order_id"), str) or not state["order_id"].strip():
            raise DatasetValidationError(f"{prefix} requires a supported tool and nonempty order_id.")
        fields = {"found", "status", "carrier", "tracking_number", "estimated_delivery"}
        if tool == "check_return_eligibility":
            fields |= {"eligible", "reason"}
        if set(state) - (fields | {"tool", "order_id"}) or not (set(state) & fields):
            raise DatasetValidationError(f"{prefix} has unsupported or missing business fields.")
        for key in fields & state.keys():
            fact = state[key]
            if key in {"found", "eligible"}:
                valid = isinstance(fact, bool)
            elif key == "status":
                valid = fact in ("processing", "shipped", "delivered", "cancelled", "canceled")
            else:
                valid = isinstance(fact, str) and bool(fact.strip())
                if key in {"carrier", "tracking_number", "estimated_delivery"} and fact is None:
                    valid = True
                if key == "estimated_delivery" and isinstance(fact, str):
                    try:
                        valid = date.fromisoformat(fact).isoformat() == fact
                    except ValueError:
                        valid = False
            if not valid:
                raise DatasetValidationError(f"{prefix} has invalid {key}.")


def _validate_expectations(scenario: dict, dataset_type: str, location: str) -> None:
    if dataset_type == "functional":
        if not isinstance(scenario.get("expected_output"), str) or not scenario["expected_output"].strip():
            raise DatasetValidationError(f"{location}: expected_output must be a nonempty string.")
        for field in ("expected_contains", "forbidden_contains"):
            _string_list(scenario.get(field), field, location)
        calls = scenario.get("expected_tools")
        if not isinstance(calls, list):
            raise DatasetValidationError(f"{location}: expected_tools must be a list.")
        for call in calls:
            if (not isinstance(call, dict) or call.get("name") not in SUPPORTED_TOOLS
                    or not isinstance(call.get("arguments"), dict)
                    or set(call["arguments"]) != {"order_id"}
                    or not isinstance(call["arguments"]["order_id"], str)
                    or not call["arguments"]["order_id"].strip()):
                raise DatasetValidationError(f"{location}: expected_tools requires supported names and order_id arguments.")
        required = [call["name"] for call in calls]
    else:
        if scenario.get("expected_behavior") not in ("safe", "refuse_unsupported_action", "protect_data"):
            raise DatasetValidationError(f"{location}: expected_behavior is unsupported or missing.")
        _string_list(scenario.get("required_tools"), "required_tools", location, allowed=SUPPORTED_TOOLS)
        if "legacy_compatibility" in scenario and type(scenario["legacy_compatibility"]) is not bool:
            raise DatasetValidationError(f"{location}: legacy_compatibility must be a boolean.")
        if "forbidden_claims" in scenario:
            if scenario.get("legacy_compatibility") is not True:
                raise DatasetValidationError(f"{location}: forbidden_claims requires explicit legacy_compatibility; use structured safety expectations.")
            _string_list(scenario["forbidden_claims"], "forbidden_claims", location)
        required = scenario["required_tools"]
    if "allowed_tools" in scenario:
        _string_list(scenario["allowed_tools"], "allowed_tools", location, allowed=SUPPORTED_TOOLS)
        if set(required) - set(scenario["allowed_tools"]):
            raise DatasetValidationError(f"{location}: required tools must be allowed_tools.")
    if "prohibited_actions" in scenario:
        _string_list(scenario["prohibited_actions"], "prohibited_actions", location, allowed=PROHIBITED_ACTIONS, nonempty=True)
    if "allowed_fields" in scenario:
        _string_list(scenario["allowed_fields"], "allowed_fields", location)
    if "expected_injection_label" in scenario and scenario["expected_injection_label"] not in INJECTION_LABELS:
        raise DatasetValidationError(f"{location}: expected_injection_label is unsupported.")
    if "expected_authoritative_facts" in scenario:
        _validate_facts(scenario["expected_authoritative_facts"], location)
        states = scenario["expected_authoritative_facts"]
        for state in ([states] if isinstance(states, dict) else states):
            if state["tool"] not in required:
                raise DatasetValidationError(f"{location}: authoritative fact tool must be required.")


def load_dataset(
    path: str | Path, *, dataset_type: str, suite: str = "smoke",
) -> list[dict]:
    """Validate every row, then select a suite without changing fields or order.

    Categories are scoped to functional/safety datasets; register new categories
    in CATEGORIES and the coverage matrix before adding scenarios using them.
    Invalid full-only rows also fail smoke loading so hidden schema errors cannot
    accumulate. IDs must be nonempty and unique within the dataset.
    """
    _validate_suite(suite)
    if not isinstance(dataset_type, str) or dataset_type not in CATEGORIES:
        raise DatasetValidationError("Unsupported dataset type; choose functional or safety.")
    try:
        with Path(path).open(encoding="utf-8") as file:
            scenarios = json.load(file)
    except (ValueError, UnicodeError) as error:
        raise DatasetValidationError(f"{dataset_type} dataset must contain valid UTF-8 JSON.") from error
    if not isinstance(scenarios, list):
        raise DatasetValidationError(f"{dataset_type} dataset must be a JSON array of scenarios.")
    seen = set()
    for index, scenario in enumerate(scenarios, start=1):
        location = f"{dataset_type} dataset row {index}"
        if not isinstance(scenario, dict):
            raise DatasetValidationError(f"{location}: scenario must be an object.")
        for field in ("id", "input"):
            if not isinstance(scenario.get(field), str) or not scenario[field].strip():
                raise DatasetValidationError(f"{location}: {field} must be a nonempty string.")
        if scenario["id"] in seen:
            raise DatasetValidationError(f"{location}: duplicate scenario ID.")
        seen.add(scenario["id"])
        for field, allowed in (
            ("category", CATEGORIES[dataset_type]), ("tier", SUITES), ("risk", RISK_LEVELS),
            ("test_intent", TEST_INTENTS),
        ):
            if field not in scenario:
                raise DatasetValidationError(f"{location}: missing required metadata '{field}'.")
            value = scenario[field]
            if not isinstance(value, str) or value not in allowed:
                raise DatasetValidationError(
                    f"{location}: unsupported {field}; allowed values: {', '.join(sorted(allowed))}."
                )
        _string_list(scenario.get("coverage_tags"), "coverage_tags", location, nonempty=True)
        _validate_expectations(scenario, dataset_type, location)
    return _select(scenarios, suite)


def load_datasets(
    dataset_dir: str | Path = DEFAULT_DATASET_DIR, *, suite: str = "smoke",
) -> EvaluationDatasets:
    """Load both datasets before execution and reject cross-dataset duplicate IDs."""
    _validate_suite(suite)
    directory = Path(dataset_dir)
    functional = load_dataset(directory / "functional.json", dataset_type="functional", suite="full")
    safety = load_dataset(directory / "safety.json", dataset_type="safety", suite="full")
    if {scenario["id"] for scenario in functional} & {scenario["id"] for scenario in safety}:
        raise DatasetValidationError("Duplicate scenario ID across functional and safety datasets.")
    return EvaluationDatasets(_select(functional, suite), _select(safety, suite))
