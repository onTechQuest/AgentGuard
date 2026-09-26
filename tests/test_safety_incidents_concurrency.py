"""Controlled offline publication, including concurrent readers and first use."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Lock
from uuid import uuid4

import pytest

from src.agentguard import safety_incidents as incidents
from src.agentguard.evaluation_record import EvaluationRecord
from src.agentguard.safety_evaluator import SafetyScore

WAIT = 10


def evidence(index):
    request_id = uuid4().hex
    order_id = f"ORD-{5000 + index}"
    secret = f"private-value-{request_id}"
    scenario = {"id": request_id, "input": f"Where is {order_id}?", "password": secret}
    facts = {"found": True, "order": {"order_id": order_id, "status": "shipped"}}
    record = EvaluationRecord(request_id, scenario["input"], f"{request_id} {order_id} shipped. {secret}",
        [{"name": "get_order_status", "arguments": {"order_id": order_id}}], 0, 0, 0, 0, 0,
        tool_outputs=[{"name": "get_order_status", "call_id": request_id, "output": facts}],
        production_telemetry={"request_id": request_id, "terminal_status": "completed"})
    score = SafetyScore(request_id, False, False, None, None, True, [f"Synthetic failure {request_id}"])
    return scenario, record, score


def check_artifact(path, original):
    assert path is not None
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    scenario, record, score = original
    assert data["scenario_id"] == data["request_id"] == scenario["id"]
    assert data["final_response"] == record.final_output.replace(scenario["password"], "[REDACTED]")
    assert data["tool_calls"] == record.tool_calls
    assert data["projected_authoritative_facts"] == record.tool_outputs
    assert data["safety_score"]["failures"] == score.failures
    assert scenario["password"] not in text
    return data


@pytest.mark.skipif(os.name != "nt", reason="Windows extended namespace regression")
def test_existing_directory_namespace_alias_is_not_an_escape(tmp_path, monkeypatch):
    """Deterministically reproduce the exact alias observed in the failing trace."""
    original = Path.resolve
    def prefixed(path, *args, **kwargs):
        resolved = original(path, *args, **kwargs)
        if "reports" in path.parts and not str(resolved).startswith("\\\\?\\"):
            return Path("\\\\?\\" + str(resolved))
        return resolved
    monkeypatch.setattr(Path, "resolve", prefixed)
    sample = evidence(0)
    path = incidents.SafetyIncidentRecorder(tmp_path).retain(*sample)
    check_artifact(path, sample)


@pytest.mark.parametrize("count", [10, 25])
@pytest.mark.parametrize("ownership", ["shared", "same_run", "separate_runs"])
def test_repeated_concurrent_publication(tmp_path, monkeypatch, count, ownership):
    monkeypatch.setattr(incidents, "git_commit", lambda root: "1" * 40)
    recorders = [incidents.SafetyIncidentRecorder(tmp_path) for _ in range(1 if ownership == "shared" else count)]
    if ownership == "same_run":
        for recorder in recorders:
            recorder.run_id = recorders[0].run_id
    paths = []
    for repetition in range(5):
        samples = [evidence(repetition * count + index) for index in range(count)]
        before = deepcopy(samples)
        barrier = Barrier(count)
        def publish(index):
            barrier.wait(WAIT)
            return recorders[0 if ownership == "shared" else index].retain(*samples[index])
        with ThreadPoolExecutor(max_workers=count) as pool:
            completed = list(pool.map(publish, range(count)))
        assert samples == before
        assert None not in completed
        assert len(set(completed)) == count
        for path, sample in zip(completed, samples):
            artifact = check_artifact(path, sample)
            text = json.dumps(artifact)
            assert all(other[0]["id"] not in text for other in samples if other is not sample)
        paths.extend(completed)
    assert len(paths) == len(set(paths)) == count * 5
    assert len({path.parent for path in paths}) == (count if ownership == "separate_runs" else 1)
    assert set((tmp_path / "reports").rglob("*.json")) == set(paths)
    assert not list((tmp_path / "reports").rglob("*.tmp"))


def test_shared_first_use_initializes_metadata_once(tmp_path, monkeypatch):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    entered, release = Event(), Event()
    barrier = Barrier(25)
    calls, git_calls = [], []
    lock = Lock()
    def version(package):
        with lock:
            calls.append(package)
        if package == "openai-agents":
            entered.set()
            assert release.wait(WAIT)
        return "test-version"
    monkeypatch.setattr(incidents, "version", version)
    monkeypatch.setattr(incidents, "git_commit", lambda root: git_calls.append(root) or "1" * 40)
    def publish(index):
        barrier.wait(WAIT)
        return recorder.retain(*evidence(index))
    with ThreadPoolExecutor(max_workers=25) as pool:
        futures = [pool.submit(publish, i) for i in range(25)]
        try:
            assert entered.wait(WAIT)
        finally:
            release.set()
        paths = [future.result(WAIT) for future in futures]
    assert len(set(paths)) == 25 and None not in paths
    assert calls == ["openai-agents", "openai", "deepeval"]
    assert len(git_calls) == 1
    assert all(json.loads(p.read_text())["sdk_versions"] == dict.fromkeys(calls, "test-version") for p in paths)


def test_separate_first_use_does_not_globally_serialize_metadata(tmp_path, monkeypatch):
    recorders = [incidents.SafetyIncidentRecorder(tmp_path) for _ in range(25)]
    barrier = Barrier(25)
    original = incidents.version
    def version(package):
        if package == "openai-agents":
            barrier.wait(WAIT)
        return original(package)
    monkeypatch.setattr(incidents, "version", version)
    monkeypatch.setattr(incidents, "git_commit", lambda root: "1" * 40)
    with ThreadPoolExecutor(max_workers=25) as pool:
        paths = list(pool.map(lambda i: recorders[i].retain(*evidence(i)), range(25)))
    assert len(set(paths)) == 25 and None not in paths


@pytest.mark.parametrize("count", [10, 25])
def test_readers_only_see_complete_artifacts(tmp_path, monkeypatch, count):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    monkeypatch.setattr(incidents, "git_commit", lambda root: "1" * 40)
    staged = Barrier(count + 1)
    release = Event()
    publications = Queue()
    link = os.link
    samples = [evidence(index) for index in range(count)]
    def publish_link(source, target):
        # All writers overlap after writing, before making any final path visible.
        json.loads(Path(source).read_text(encoding="utf-8"))
        staged.wait(WAIT)
        assert release.wait(WAIT)
        link(source, target)
        publications.put(target)
    monkeypatch.setattr(incidents.os, "link", publish_link)
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(recorder.retain, *sample) for sample in samples]
        try:
            staged.wait(WAIT)
            assert not list((tmp_path / "reports").rglob("*.json"))
            assert len(list((tmp_path / "reports").rglob("*.tmp"))) == count
        finally:
            release.set()
        # Read while writers are publishing; no polling sleeps or JSON retries.
        observed = [json.loads(Path(publications.get(timeout=WAIT)).read_text(encoding="utf-8")) for _ in samples]
        paths = [future.result(WAIT) for future in futures]
    assert {d["scenario_id"] for d in observed} == {sample[0]["id"] for sample in samples}
    for path, sample in zip(paths, samples):
        check_artifact(path, sample)
    assert not list((tmp_path / "reports").rglob("*.tmp"))


def test_uuid_collision_never_overwrites_completed_artifact(tmp_path, monkeypatch):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    identifier = uuid4()
    monkeypatch.setattr(incidents, "uuid4", lambda: identifier)
    first = recorder.retain(*evidence(0))
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        recorder.retain(*evidence(1))
    assert first.read_bytes() == original
    assert not list(first.parent.glob("*.tmp"))


@pytest.mark.parametrize("phase", ["metadata", "serialization", "flush", "publication"])
def test_genuine_failure_propagates_and_leaves_no_partial_artifact(tmp_path, monkeypatch, phase):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    def fail(*args, **kwargs):
        raise ValueError("synthetic-publication-failure")
    if phase == "metadata":
        monkeypatch.setattr(incidents, "version", fail)
    elif phase == "serialization":
        monkeypatch.setattr(recorder, "_build", lambda *args: {"invalid": float("nan")})
    elif phase == "flush":
        monkeypatch.setattr(incidents.os, "fsync", fail)
    else:
        monkeypatch.setattr(incidents.os, "link", fail)
    with pytest.raises(ValueError):
        recorder.retain(*evidence(0))
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("run_id", ["../escape", "", "not-a-uuid", "/absolute/path"])
def test_invalid_run_id_fails_without_creating_output(tmp_path, run_id):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    recorder.run_id = run_id
    with pytest.raises(ValueError):
        recorder.retain(*evidence(0))
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("component", ["reports", "safety_incidents", "run"])
def test_resolved_destination_cannot_escape_its_parent(tmp_path, monkeypatch, component):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    name = recorder.run_id if component == "run" else component
    outside = tmp_path / "unrelated" / name
    outside.mkdir(parents=True)
    original = Path.resolve
    def redirected(path, *args, **kwargs):
        if path.name == name and "unrelated" not in path.parts:
            return outside
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="escapes reports"):
        recorder.retain(*evidence(0))
    assert not list(tmp_path.rglob("*.json"))
    assert not list(outside.iterdir())


def test_concurrent_uuid_collision_is_explicit_and_cannot_replace_winner(tmp_path, monkeypatch):
    recorder = incidents.SafetyIncidentRecorder(tmp_path)
    identifier = uuid4()
    monkeypatch.setattr(incidents, "uuid4", lambda: identifier)
    barrier = Barrier(2)
    original = os.link
    def simultaneous(source, target):
        barrier.wait(WAIT)
        original(source, target)
    monkeypatch.setattr(incidents.os, "link", simultaneous)
    samples = [evidence(0), evidence(1)]
    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recorder.retain, *sample) for sample in samples]
        for future in futures:
            try:
                outcomes.append(future.result(WAIT))
            except FileExistsError as error:
                outcomes.append(error)
    assert sum(isinstance(value, FileExistsError) for value in outcomes) == 1
    winner = next(i for i, value in enumerate(outcomes) if isinstance(value, Path))
    check_artifact(outcomes[winner], samples[winner])
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))
