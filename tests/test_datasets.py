"""Local-file validation and filtering; no evaluators or live agents needed."""

from copy import deepcopy
import json

import pytest

from src.agentguard.datasets import (
    CATEGORIES, DEFAULT_DATASET_DIR, TEST_INTENTS, DatasetValidationError, load_dataset, load_datasets,
)


def scenario(identifier="case-1", *, dataset_type="functional", tier="smoke", **fields):
    expectations = (
        {"expected_output": "The order has shipped.", "expected_contains": [], "forbidden_contains": [], "expected_tools": []}
        if dataset_type == "functional" else
        {"expected_behavior": "safe", "required_tools": []}
    )
    return {
        "id": identifier, "input": "Where is order ORD-1001?",
        "category": "order_status" if dataset_type == "functional" else "prompt_injection",
        "tier": tier, "risk": "high", "test_intent": "baseline", "coverage_tags": ["status_grounding"],
        **expectations, **fields,
    }


def test_complete_suite_has_no_legacy_dependencies():
    full = load_datasets(suite="full")
    smoke = load_datasets(suite="smoke")
    rows = full.functional + full.safety
    assert len(rows) == len({row["id"] for row in rows}) == 70
    assert len(smoke.functional + smoke.safety) == 16
    assert len(full.functional) == 38 and len(full.safety) == 32
    for row in full.safety:
        assert "forbidden_claims" not in row
        assert not row.get("legacy_compatibility", False)
        assert (row.get("expected_authoritative_facts") or row.get("expected_injection_label")
                or row.get("prohibited_actions") or row.get("expected_behavior") == "protect_data")


@pytest.mark.parametrize("legacy", [None, False, "true", 1])
def test_legacy_phrases_require_explicit_boolean_opt_in(tmp_path, legacy):
    row = scenario(dataset_type="safety", forbidden_claims=["old phrase"])
    if legacy is not None:
        row["legacy_compatibility"] = legacy
    path = tmp_path / "safety.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="legacy_compatibility"):
        load_dataset(path, dataset_type="safety")


def test_legacy_compatibility_remains_available_by_explicit_opt_in(tmp_path):
    row = scenario(dataset_type="safety", legacy_compatibility=True, forbidden_claims=["old phrase"])
    path = tmp_path / "safety.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    assert load_dataset(path, dataset_type="safety") == [row]


@pytest.fixture
def write_dataset(tmp_path):
    def write(rows, dataset_type="functional"):
        path = tmp_path / f"{dataset_type}.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path
    return write


@pytest.mark.parametrize("dataset_type", ["functional", "safety"])
@pytest.mark.parametrize("tier", ["smoke", "full"])
@pytest.mark.parametrize("risk", ["critical", "high", "medium", "low"])
def test_valid_metadata_preserves_existing_fields(write_dataset, dataset_type, tier, risk):
    row = scenario(dataset_type=dataset_type, tier=tier, risk=risk,
                   expected_tools=[{"name": "get_order_status", "arguments": {"order_id": "ORD-1001"}}],
                   expected_output="Shipped.", custom_metadata={"notes": ["preserve"]})
    before = deepcopy(row)
    path = write_dataset([row], dataset_type)
    assert load_dataset(path, dataset_type=dataset_type, suite="full") == [before]


@pytest.mark.parametrize("field", ["category", "tier", "risk", "test_intent"])
def test_missing_metadata_has_clear_row_and_field_error(write_dataset, field):
    row = scenario()
    del row[field]
    with pytest.raises(DatasetValidationError, match=f"functional dataset row 1: missing required metadata '{field}'"):
        load_dataset(write_dataset([row]), dataset_type="functional")


@pytest.mark.parametrize("field, value", [
    ("tier", "nightly"), ("tier", "SMOKE"), ("tier", None), ("tier", []),
    ("category", "unknown"), ("category", "prompt_injection"), ("category", ""), ("category", {}),
    ("risk", "urgent"), ("risk", "HIGH"), ("risk", None), ("risk", 1),
])
def test_unsupported_metadata_is_rejected(write_dataset, field, value):
    with pytest.raises(DatasetValidationError, match=f"unsupported {field}; allowed values:"):
        load_dataset(write_dataset([scenario(**{field: value})]), dataset_type="functional")


def test_functional_category_is_invalid_in_safety_dataset(write_dataset):
    with pytest.raises(DatasetValidationError, match="safety dataset row 1: unsupported category"):
        load_dataset(write_dataset([scenario()], "safety"), dataset_type="safety")


@pytest.mark.parametrize("dataset_type, category", [
    (dataset_type, category) for dataset_type, categories in CATEGORIES.items() for category in sorted(categories)
])
def test_all_planned_categories_are_supported(write_dataset, dataset_type, category):
    row = scenario(dataset_type=dataset_type, category=category)
    assert load_dataset(write_dataset([row], dataset_type), dataset_type=dataset_type) == [row]


def test_smoke_and_full_filtering_preserve_file_order_without_duplicates(write_dataset):
    rows = [scenario("z-full", tier="full"), scenario("z-smoke"),
            scenario("a-full", tier="full"), scenario("a-smoke")]
    path = write_dataset(rows)
    smoke = load_dataset(path, dataset_type="functional", suite="smoke")
    full = load_dataset(path, dataset_type="functional", suite="full")
    assert smoke == [rows[1], rows[3]]
    assert full == rows
    assert all(row in full for row in smoke)
    assert len({row["id"] for row in full}) == len(full)
    assert load_dataset(path, dataset_type="functional") == smoke
    assert load_dataset(path, dataset_type="functional", suite="full") == full


def test_load_both_datasets_with_shared_suite(write_dataset, tmp_path):
    functional = [scenario("f-full", tier="full"), scenario("f-smoke")]
    safety = [scenario("s-smoke", dataset_type="safety"), scenario("s-full", dataset_type="safety", tier="full")]
    write_dataset(functional)
    write_dataset(safety, "safety")
    smoke = load_datasets(tmp_path)
    full = load_datasets(tmp_path, suite="full")
    assert smoke.functional == [functional[1]]
    assert smoke.safety == [safety[0]]
    assert full.functional == functional
    assert full.safety == safety


@pytest.mark.parametrize("dataset_type", ["functional", "safety"])
def test_repository_datasets_load(dataset_type):
    path = DEFAULT_DATASET_DIR / f"{dataset_type}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw
    assert load_dataset(path, dataset_type=dataset_type, suite="full") == raw
    assert load_dataset(path, dataset_type=dataset_type) == [row for row in raw if row["tier"] == "smoke"]
    assert getattr(load_datasets(suite="full"), dataset_type) == raw


def test_invalid_full_only_row_is_validated_in_smoke(write_dataset):
    path = write_dataset([scenario(), scenario("full-only", tier="full", risk="invalid")])
    with pytest.raises(DatasetValidationError, match="row 2: unsupported risk"):
        load_dataset(path, dataset_type="functional", suite="smoke")


@pytest.mark.parametrize("suite", ["nightly", "", None])
def test_invalid_suite_is_rejected_before_reading(tmp_path, suite):
    with pytest.raises(DatasetValidationError, match="Unsupported suite"):
        load_dataset(tmp_path / "missing.json", dataset_type="functional", suite=suite)
    with pytest.raises(DatasetValidationError, match="Unsupported suite"):
        load_datasets(tmp_path, suite=suite)


def test_invalid_dataset_type_is_rejected(tmp_path):
    with pytest.raises(DatasetValidationError, match="Unsupported dataset type"):
        load_dataset(tmp_path / "missing.json", dataset_type="unknown")


@pytest.mark.parametrize("rows", [{}, None, "scenario"])
def test_dataset_must_be_an_array(write_dataset, rows):
    with pytest.raises(DatasetValidationError, match="JSON array"):
        load_dataset(write_dataset(rows), dataset_type="functional")


@pytest.mark.parametrize("row", [None, "scenario", []])
def test_scenarios_must_be_objects(write_dataset, row):
    with pytest.raises(DatasetValidationError, match="row 1: scenario must be an object"):
        load_dataset(write_dataset([row]), dataset_type="functional")


@pytest.mark.parametrize("field", ["id", "input"])
@pytest.mark.parametrize("value", [None, "", "  ", 42])
def test_required_identity_and_input(write_dataset, field, value):
    with pytest.raises(DatasetValidationError, match=f"{field} must be a nonempty string"):
        load_dataset(write_dataset([scenario(**{field: value})]), dataset_type="functional")


def test_duplicate_ids_are_rejected_even_in_unselected_tier(write_dataset):
    with pytest.raises(DatasetValidationError, match="duplicate scenario ID"):
        load_dataset(write_dataset([scenario(), scenario(tier="full")]), dataset_type="functional")


def test_cross_dataset_duplicate_ids_are_rejected_before_filtering(write_dataset, tmp_path):
    write_dataset([scenario()])
    write_dataset([scenario(dataset_type="safety", tier="full")], "safety")
    with pytest.raises(DatasetValidationError, match="Duplicate scenario ID across"):
        load_datasets(tmp_path)


@pytest.mark.parametrize("contents", [b"not-json", b"[{", b"\xff"])
def test_invalid_json_has_safe_diagnostic(tmp_path, contents):
    path = tmp_path / "functional.json"
    path.write_bytes(contents)
    with pytest.raises(DatasetValidationError, match="functional dataset must contain valid UTF-8 JSON"):
        load_dataset(path, dataset_type="functional")


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "missing.json", dataset_type="functional")


def test_empty_datasets_and_empty_selection_are_not_fabricated(write_dataset, tmp_path):
    write_dataset([])
    write_dataset([scenario(dataset_type="safety", tier="full")], "safety")
    loaded = load_datasets(tmp_path)
    assert loaded.functional == loaded.safety == []


def test_default_dataset_location_does_not_depend_on_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_datasets().functional
    assert load_datasets().safety


@pytest.mark.parametrize("intent", TEST_INTENTS)
def test_all_test_intents_are_valid(write_dataset, intent):
    row = scenario(test_intent=intent)
    assert load_dataset(write_dataset([row]), dataset_type="functional") == [row]


@pytest.mark.parametrize("intent", [None, "random", "BASELINE", [], 1])
def test_invalid_test_intent(write_dataset, intent):
    with pytest.raises(DatasetValidationError, match="unsupported test_intent"):
        load_dataset(write_dataset([scenario(test_intent=intent)]), dataset_type="functional")


@pytest.mark.parametrize("tags", [None, [], "tag", [""], ["  "], [7], ["tag", "tag"]])
def test_invalid_coverage_tags(write_dataset, tags):
    with pytest.raises(DatasetValidationError, match="coverage_tags"):
        load_dataset(write_dataset([scenario(coverage_tags=tags)]), dataset_type="functional")


def test_missing_coverage_tags(write_dataset):
    row = scenario()
    del row["coverage_tags"]
    with pytest.raises(DatasetValidationError, match="coverage_tags"):
        load_dataset(write_dataset([row]), dataset_type="functional")


@pytest.mark.parametrize("field, value", [
    ("expected_output", ""), ("expected_output", None),
    ("expected_contains", "shipped"), ("forbidden_contains", [False]),
    ("expected_tools", None), ("expected_tools", ["get_order_status"]),
    ("expected_tools", [{"name": "cancel_order", "arguments": {"order_id": "ORD-1001"}}]),
    ("expected_tools", [{"name": "get_order_status", "arguments": {"order_id": None}}]),
    ("expected_tools", [{"name": "get_order_status", "arguments": {"customer_id": "CUST-001"}}]),
    ("allowed_tools", ["imaginary_tool"]),
])
def test_invalid_functional_expectation_schema(write_dataset, field, value):
    with pytest.raises(DatasetValidationError, match=field):
        load_dataset(write_dataset([scenario(**{field: value})]), dataset_type="functional")


@pytest.mark.parametrize("field, value", [
    ("required_tools", "get_order_status"), ("required_tools", ["imaginary_tool"]),
    ("expected_behavior", "permit_everything"), ("forbidden_claims", [None]),
    ("prohibited_actions", "cancel_order"), ("prohibited_actions", []),
    ("prohibited_actions", ["unknown_action"]), ("prohibited_actions", [{}]),
    ("expected_injection_label", None), ("expected_injection_label", "safe"),
    ("expected_injection_label", []), ("allowed_fields", "email"),
])
def test_invalid_safety_expectation_schema(write_dataset, field, value):
    row = scenario(dataset_type="safety", **{field: value})
    with pytest.raises(DatasetValidationError, match=field):
        load_dataset(write_dataset([row], "safety"), dataset_type="safety")


@pytest.mark.parametrize("value", [
    None, [], {}, "shipped", ["shipped"],
    {"tool": "get_order_status", "status": "shipped"},
    {"tool": "get_order_status", "order_id": "ORD-1001"},
    {"tool": "imaginary", "order_id": "ORD-1001", "status": "shipped"},
    {"tool": "get_order_status", "order_id": "ORD-1001", "unknown_field": True},
    {"tool": "get_order_status", "order_id": "ORD-1001", "eligible": True},
    {"tool": "get_order_status", "order_id": "ORD-1001", "found": "true"},
    {"tool": "get_order_status", "order_id": "ORD-1001", "status": "imaginary"},
    {"tool": "get_order_status", "order_id": "ORD-1001", "estimated_delivery": "2026-02-30"},
    {"tool": "get_order_status", "order_id": "ORD-1001", "carrier": []},
    {"tool": "check_return_eligibility", "order_id": "ORD-1001", "eligible": 1},
])
def test_invalid_authoritative_fact_schema(write_dataset, value):
    row = scenario(dataset_type="safety", required_tools=["get_order_status", "check_return_eligibility"],
                   expected_authoritative_facts=value)
    with pytest.raises(DatasetValidationError, match="expected_authoritative_facts"):
        load_dataset(write_dataset([row], "safety"), dataset_type="safety")


def test_expected_fact_tool_must_be_required(write_dataset):
    row = scenario(dataset_type="safety", expected_authoritative_facts={
        "tool": "get_order_status", "order_id": "ORD-1001", "status": "shipped",
    })
    with pytest.raises(DatasetValidationError, match="fact tool must be required"):
        load_dataset(write_dataset([row], "safety"), dataset_type="safety")


def test_required_tool_cannot_be_disallowed(write_dataset):
    row = scenario(dataset_type="safety", required_tools=["get_order_status"], allowed_tools=[])
    with pytest.raises(DatasetValidationError, match="required tools must be allowed_tools"):
        load_dataset(write_dataset([row], "safety"), dataset_type="safety")


@pytest.mark.parametrize("label", ["resisted", "partially_followed", "followed_injection"])
def test_injection_label_schema_allows_classifier_vocabulary(write_dataset, label):
    row = scenario(dataset_type="safety", expected_injection_label=label)
    assert load_dataset(write_dataset([row], "safety"), dataset_type="safety") == [row]
