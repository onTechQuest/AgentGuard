"""Offline, payload-free evaluation identities and write-once run artifacts.

Canonicalization is deliberately typed and conservative. Callers pass approved
configuration, never arbitrary environment/client/request objects.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from uuid import UUID, uuid4

import yaml

UNKNOWN, ABSENT, DISABLED = "UNKNOWN", "ABSENT", "DISABLED"
SCHEMA_VERSION = 1
METADATA = frozenset({"category", "tier", "risk", "test_intent", "coverage_tags"})
EXPECTATIONS = frozenset({"expected_output", "expected_tools", "expected_contains", "forbidden_contains",
    "expected_behavior", "expected_injection_label", "expected_authoritative_facts", "required_tools",
    "allowed_tools", "prohibited_actions", "allowed_fields", "legacy_compatibility", "forbidden_claims"})


def _validate(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _validate(item)
        return
    raise ValueError("Canonical content must be finite, JSON-typed data")


def canonical_json(value):
    _validate(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(kind, content):
    return hashlib.sha256(canonical_json(dict(kind=kind, canonicalization_version=1,
                                             content=content)).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("Duplicate or non-string mapping key")
        result[key] = value
    return result


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    return _pairs((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value)


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_document(text, *, format="json"):
    if format == "json":
        value = json.loads(text, object_pairs_hook=_pairs)
    elif format in {"yaml", "yml"}:
        value = yaml.load(text, Loader=_UniqueLoader)
    else:
        raise ValueError("Unsupported document format")
    _validate(value)
    return value


def read_document(path):
    path = Path(path)
    return parse_document(path.read_text(encoding="utf-8-sig"), format=path.suffix[1:])


def scenario_identity(scenario):
    unknown = set(scenario) - METADATA - EXPECTATIONS - {"id", "input"}
    if unknown:
        raise ValueError("Unclassified scenario fields; update lineage partition schema")
    if not isinstance(scenario.get("id"), str) or not scenario["id"].strip():
        raise ValueError("Scenario ID required")
    parts = dict(content_fingerprint=fingerprint("scenario-content", {"input": scenario.get("input", ABSENT)}),
        metadata_fingerprint=fingerprint("scenario-metadata", {k: scenario[k] for k in METADATA if k in scenario}),
        expected_behavior_fingerprint=fingerprint("scenario-expectation", {k: scenario[k] for k in EXPECTATIONS if k in scenario}))
    return dict(scenario_id=scenario["id"], **parts,
                scenario_fingerprint=fingerprint("scenario-v1", parts))


def scenario_index(scenarios):
    rows = [scenario_identity(s) for s in scenarios]
    if len({r["scenario_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate scenario ID")
    return rows


def dataset_identity(kind, scenarios):
    rows = scenario_index(scenarios)
    return dict(schema_version=1, count=len(rows), fingerprint=fingerprint("dataset", {
        "kind": kind, "schema_version": 1, "scenarios": sorted(rows, key=lambda r: r["scenario_id"])}))


def source_identity(root):
    root = Path(root)
    def git(*args):
        try:
            return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *args],
                cwd=root, capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    commit = git("rev-parse", "HEAD")
    state = git("status", "--porcelain", "--untracked-files=all")
    # Explicit approved inventory: never .env, reports, arbitrary untracked files.
    files = {}
    for folder in ("src", "scripts", "config", "evals/datasets"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".yml"}:
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    path = root / "requirements.txt"
    if path.exists():
        files["requirements.txt"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(git_commit=commit or UNKNOWN, dirty_state=UNKNOWN if state is None else "DIRTY" if state else "CLEAN",
                source_fingerprint=fingerprint("source-inventory-v1", files) if files else UNKNOWN)


@dataclass(frozen=True)
class EvaluationRunManifest:
    """Canonical bytes are the immutable storage; returned mappings are copies."""
    _json: str

    @classmethod
    def create(cls, document):
        required = {"manifest_schema_version", "canonicalization_version", "run_id", "timestamp", "suite",
            "execution_mode", "source", "production_models", "judge_models", "datasets", "scenario_count",
            "scenario_set_fingerprint", "execution_order_fingerprint", "fingerprints", "libraries", "hosting",
            "lineage_status", "unavailable_fields"}
        if not required <= document.keys() or document["manifest_schema_version"] != 1:
            raise ValueError("Invalid evaluation manifest")
        if UUID(document["run_id"]).hex != document["run_id"]:
            raise ValueError("Canonical UUID run ID required")
        if document["execution_mode"] not in {"live", "offline_fixture", "replay"}:
            raise ValueError("Unknown execution mode")
        if document["lineage_status"] not in {"FULL_LINEAGE", "PARTIAL_LINEAGE", "LEGACY_UNVERSIONED"}:
            raise ValueError("Unknown lineage status")
        return cls(canonical_json(document))

    def to_dict(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return fingerprint("evaluation-run-manifest", self.to_dict())

    @property
    def run_id(self):
        return self.to_dict()["run_id"]


def publish(path, document):
    """Atomic no-replace publication on Windows and POSIX via an exclusive link."""
    path = Path(path)
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(document) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Fails if destination exists; no overwrite race.
    finally:
        temporary.unlink(missing_ok=True)


class RunArtifacts:
    def __init__(self, root, manifest, scenarios):
        EvaluationRunManifest.create(manifest.to_dict())
        self.manifest = manifest
        self.path = Path(root) / "reports" / "evaluations" / manifest.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.executed = []
        self.observed_models = []
        self.aggregate = {}
        self.evaluation_complete = False
        publish(self.path / "scenarios.json", dict(self.reference, scenarios=scenarios))
        publish(self.path / "manifest.json", manifest.to_dict())

    @property
    def reference(self):
        return dict(run_id=self.manifest.run_id, manifest_digest=self.manifest.digest)

    def observe(self, scenario_id, *, completed=True):
        self.executed.append(dict(scenario_id=scenario_id, completed=completed))
        return self.executed[-1]

    def finish(self, *, state="COMPLETED", failures=()):
        from src.agentguard.lineage_adapters import plain
        results = dict(result_schema_version=1, **self.reference, completion_state=state,
            executed_scenario_count=len(self.executed), completed_scenario_count=sum(r["completed"] for r in self.executed),
            executions=self.executed, failures=list(failures), aggregate_results=plain(self.aggregate),
            observed_model_identities=self.observed_models)
        publish(self.path / "results.json", results)
        publish(self.path / "completion.json", dict(completion_schema_version=1, **self.reference,
            state=state, results_digest=fingerprint("evaluation-results", results),
            timestamp=datetime.now(timezone.utc).isoformat()))


def load_run(path):
    path = Path(path)
    manifest = EvaluationRunManifest.create(read_document(path / "manifest.json"))
    scenarios = read_document(path / "scenarios.json")
    reference = dict(run_id=manifest.run_id, manifest_digest=manifest.digest)
    if any(scenarios.get(k) != v for k, v in reference.items()):
        raise ValueError("Scenario manifest reference mismatch")
    rows = scenarios["scenarios"]
    if fingerprint("scenario-set", sorted(rows, key=lambda r: r["scenario_id"])) != manifest.to_dict()["scenario_set_fingerprint"]:
        raise ValueError("Scenario index integrity failure")
    result = dict(manifest=manifest.to_dict(), scenarios=rows, completion_state="INCOMPLETE")
    if (path / "results.json").exists():
        result["results"] = read_document(path / "results.json")
        if any(result["results"].get(k) != v for k, v in reference.items()):
            raise ValueError("Result manifest reference mismatch")
    if (path / "completion.json").exists():
        completion = read_document(path / "completion.json")
        if (any(completion.get(k) != v for k, v in reference.items()) or "results" not in result or
                completion["results_digest"] != fingerprint("evaluation-results", result["results"]) or
                completion.get("state") != result["results"].get("completion_state")):
            raise ValueError("Completion integrity failure")
        result["completion_state"] = completion["state"]
    return result


_active = ContextVar("evaluation_lineage", default=None)


def current_run():
    return _active.get()


def lineage_entry(function):
    """Close results even on early returns; abrupt termination leaves INCOMPLETE."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _active.set(None)
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            if current_run():
                current_run().finish(state="INCOMPLETE", failures=[type(error).__name__])
            raise
        else:
            if current_run():
                run = current_run()
                run.finish(state="COMPLETED" if run.evaluation_complete else "INCOMPLETE")
            return result
        finally:
            _active.reset(token)
    return wrapped


def start_run(root, *, suite, functional=(), safety=(), execution_mode="live", evaluation_config=None,
              hosting=None, repetitions=1, effective_runtime_policy=None):
    from src.agentguard.lineage_adapters import build_manifest
    manifest, rows = build_manifest(root, suite=suite, functional=list(functional), safety=list(safety),
        execution_mode=execution_mode, evaluation_config=evaluation_config, hosting=hosting, repetitions=repetitions,
        effective_runtime_policy=effective_runtime_policy)
    run = RunArtifacts(root, manifest, rows)
    _active.set(run)
    return run


def read_legacy(path):
    """Read-only evidence adapter; never infer a commit from today's checkout."""
    report = read_document(path)
    keys = ("git_commit", "sdk_versions", "suite", "started_at", "timestamp", "source_fingerprint")
    provenance = {k: report.get(k, UNKNOWN) for k in keys} if isinstance(report, dict) else {k: UNKNOWN for k in keys}
    return dict(lineage_status="PARTIAL_LINEAGE" if any(v != UNKNOWN for v in provenance.values()) else "LEGACY_UNVERSIONED",
                provenance=provenance)
