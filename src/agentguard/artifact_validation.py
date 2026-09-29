"""Validate persisted lineage and observation contracts without executing runs."""
from collections import Counter
from pathlib import Path
from functools import lru_cache
import re

from src.agentguard.lineage import fingerprint, read_document

SCHEMAS = Path(__file__).resolve().parents[2] / "config"


@lru_cache(maxsize=None)
def validator(filename):
    import jsonschema
    document = read_document(SCHEMAS / filename)
    jsonschema.Draft202012Validator.check_schema(document)
    return jsonschema.Draft202012Validator(document)


def schema(document, filename):
    import jsonschema
    try:
        validator(filename).validate(document)
    except jsonschema.ValidationError as error:
        raise ValueError("Artifact schema validation failed: " + filename) from error


def validate_loaded(result, completion=None):
    manifest, scenarios = result["manifest"], result["scenarios"]
    schema(manifest, "evaluation-run-manifest.schema.json")
    ids = [r["scenario_id"] for r in scenarios]
    if len(set(ids)) != len(ids) or len(ids) != manifest["scenario_count"]:
        raise ValueError("Scenario population mismatch")
    for row in scenarios:
        expected_fields = {"scenario_id", "scenario_fingerprint", "content_fingerprint", "metadata_fingerprint", "expected_behavior_fingerprint"}
        if set(row) != expected_fields or any(not re.fullmatch(r"[0-9a-f]{64}", row[k]) for k in expected_fields - {"scenario_id"}):
            raise ValueError("Invalid scenario index shape")
        parts = {k: row[k] for k in ("content_fingerprint", "metadata_fingerprint", "expected_behavior_fingerprint")}
        if fingerprint("scenario-v1", parts) != row["scenario_fingerprint"]:
            raise ValueError("Scenario identity mismatch")
    protocol = manifest.get("protocol")
    planned = manifest["planned_execution_count"]
    if protocol:
        if (protocol["protocol_version"] != 1 or protocol["suite"] != manifest["suite"]
                or protocol["execution_mode"] != manifest["execution_mode"]
                or protocol["hosting_mode"] != manifest["hosting"]["mode"]
                or set(protocol["scenario_populations"]) != set(ids)
                or planned != len(ids) * protocol["repetitions"]):
            raise ValueError("Protocol identity mismatch")
        repetitions = protocol["repetitions"]
        if fingerprint("execution-order", ids * repetitions) != manifest["execution_order_fingerprint"]:
            raise ValueError("Planned execution ordering mismatch")
        for population in ("functional", "safety"):
            selected = [r for r in scenarios if protocol["scenario_populations"][r["scenario_id"]] == population]
            expected = dict(schema_version=1, count=len(selected), fingerprint=fingerprint("dataset", dict(
                kind=population, schema_version=1, scenarios=sorted(selected, key=lambda r:r["scenario_id"]))))
            if manifest["datasets"][population]["selected"] != expected:
                raise ValueError("Selected dataset identity mismatch")
    else:
        repetitions = planned // len(ids) if ids and planned % len(ids) == 0 else 1
    results = result.get("results")
    if results is None:
        result["observation_lineage_status"] = "PARTIAL_LINEAGE"
        result["observation_comparability"] = "LIMITED_COMPARABILITY"
        return
    schema(results, "evaluation-result.schema.json")
    rows = results["executions"]
    if (results["executed_scenario_count"] != len(rows) or len(rows) > planned
            or results["completed_scenario_count"] != sum(r["completed"] for r in rows)):
        raise ValueError("Execution count mismatch")
    counts = Counter(r["scenario_id"] for r in rows)
    if set(counts) - set(ids) or any(n > repetitions for n in counts.values()):
        raise ValueError("Unknown or duplicate execution")
    if results["completion_state"] == "COMPLETED" and counts != Counter({sid: repetitions for sid in ids}):
        raise ValueError("Missing required terminal execution rows")
    if completion:
        schema(completion, "evaluation-completion.schema.json")
        if completion.get("source_integrity") != results.get("source_integrity"):
            raise ValueError("Source integrity reference mismatch")
    if "observation_contract_version" not in results:
        result["observation_lineage_status"] = "PARTIAL_LINEAGE"
        result["observation_comparability"] = "LIMITED_COMPARABILITY"
        return
    if not protocol or results["protocol"] != protocol:
        raise ValueError("Observation protocol missing or mismatched")
    observations = results["observations"]
    if len(observations) != len(rows):
        raise ValueError("Observation count mismatch")
    index = {r["scenario_id"]: r for r in scenarios}
    observed_keys = []
    for execution, observation in zip(rows, observations):
        schema(observation, "evaluation-observation.schema.json")
        sid, rep = observation["scenario_id"], observation["repetition"]
        if (sid not in index or not 1 <= rep <= repetitions or sid != execution["scenario_id"]
                or rep != execution.get("repetition", 1)
                or observation["completed"] != execution["completed"]
                or observation["completion_state"] != ("COMPLETED" if execution["completed"] else "INCOMPLETE")
                or observation["scenario_fingerprint"] != index[sid]["scenario_fingerprint"]
                or observation["population"] != protocol["scenario_populations"][sid]
                or any(observation[k] != results[k] for k in ("run_id", "manifest_digest"))):
            raise ValueError("Observation lineage mismatch")
        failure = execution.get("failure_evidence")
        if failure and (any(failure[k] != results[k] for k in ("run_id", "manifest_digest")) or failure["scenario_id"] != sid or failure["completed"] is not False):
            raise ValueError("Failure evidence linkage mismatch")
        reference = observation["failure_evidence_reference"]
        if (bool(failure) != bool(reference) or (reference and reference != dict(
                run_id=results["run_id"], manifest_digest=results["manifest_digest"], scenario_id=sid, repetition=rep))):
            raise ValueError("Failure evidence reference mismatch")
        observed_keys.append((sid, rep))
    if len(set(observed_keys)) != len(observed_keys):
        raise ValueError("Duplicate observation")
    from src.agentguard.metric_registry import aggregate, REGISTRY_VERSION
    if results["metric_registry_version"] != REGISTRY_VERSION or results["continuous_aggregates"] != aggregate(
            observations, population_complete=results["completion_state"] == "COMPLETED" and len(rows) == planned):
        raise ValueError("Continuous aggregate mismatch")
    integrity = results["source_integrity"]
    if integrity["start_source"] != manifest["source"] or integrity["start_fingerprint"] != manifest["source"]["source_fingerprint"]:
        raise ValueError("Source start identity mismatch")
    start, end = integrity["start_source"], integrity["end_source"]
    expected = "UNKNOWN" if any(v == "UNKNOWN" for source in (start, end) for v in source.values()) else "MATCH" if start == end else "CHANGED"
    if integrity["state"] != expected or integrity["end_fingerprint"] != end["source_fingerprint"]:
        raise ValueError("Source end identity mismatch")
    result["observation_lineage_status"] = manifest["lineage_status"]
    result["observation_comparability"] = "OBSERVATIONS_AVAILABLE"
