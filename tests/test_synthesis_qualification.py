"""Offline analysis contracts; synthetic telemetry never invokes production."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.agentguard.synthesis_qualification import normalize, classify, summarize, replay, historical_populations

ROOT = Path(__file__).resolve().parents[1]


def observation(duration=11438.89, total=13663.03, remaining=17778, **changes):
    raw = dict(request_id="a" * 32, total_latency_ms=total, deadline_budget_ms=20000,
        deadline_exhausted=total >= 20000, terminal_failure_component="synthesis",
        terminal_failure_category="DEADLINE_EXHAUSTED", result_abandoned=True,
        retry_attempts_total=0, observed_usage={"input_tokens":10,"output_tokens":2,"total_tokens":12},
        usage_completeness="COMPLETE", component_spans=[
            dict(component="primary_router",duration_ms=2220.27,lower_layer_retries_configured=False),
            dict(component="required_execution",duration_ms=1.34),
            dict(component="synthesis",duration_ms=duration,remaining_budget_before_ms=remaining,
                 configured_stage_cap_ms=6000,allocated_allowance_ms=6000,late_completion=True,
                 result_accepted=False,lower_layer_retries_configured=False,resolved_model="gpt-5.6-luna")])
    raw.update(changes)
    return dict(scenario_id="missing_order_001",repetition=1,status="failed",production_telemetry=raw)


def test_reported_observation_stage_cap_not_overall_deadline():
    r = normalize(observation())
    assert r["classification"] == "STAGE_ALLOWANCE_EXCEEDED"
    assert r["overall_deadline_exhausted"] is False
    assert r["retry_attempts_total"] == 0 and r["lower_layer_retries_configured"] is False
    assert r["remaining_request_budget_before_synthesis_ms"] == 17778
    assert r["configured_synthesis_allowance_ms"] == r["allocated_synthesis_allowance_ms"] == 6000
    assert r["component_latency_ms"]["required_operations"] == 1.34
    assert r["model_identities"] == ["gpt-5.6-luna"]
    assert r["production_tokens"]["total_tokens"] == 12


@pytest.mark.parametrize("total,flag,expected", [(20000,False,"OVERALL_REQUEST_DEADLINE"),
    (19999,False,"STAGE_ALLOWANCE_EXCEEDED"),(13663,True,"OVERALL_REQUEST_DEADLINE")])
def test_overall_precedence(total, flag, expected):
    assert normalize(observation(total=total,deadline_exhausted=flag))["classification"] == expected


@pytest.mark.parametrize("category", ["PROVIDER_ERROR","RATE_LIMIT","NETWORK_ERROR","TIMEOUT"])
def test_provider_classification(category):
    row = normalize(observation(terminal_failure_category=category))
    assert row["classification"] == "PROVIDER_FAILURE"
    assert replay([row])["candidates"]["S12"]["unknown_or_not_replayable"] == 1


def test_retry_evidence_is_not_hidden_or_assumed_zero():
    row = normalize(observation(retry_attempts_total=1,terminal_failure_category="UNKNOWN_INTERNAL_FAILURE"))
    assert row["classification"] == "RETRY_AMPLIFICATION"
    assert replay([row])["candidates"]["S12"]["accepted_results"] == 0
    row["retry_attempts_total"] = None
    assert classify(row) == "UNKNOWN"


def test_percentiles_and_strict_exceedances():
    rows = [normalize(observation(duration=n*1000,total=n*1000)) for n in range(1,41)]
    summary = summarize(rows)
    synthesis = summary["distributions_ms"]["synthesis"]
    assert synthesis["mean"] == 20500
    assert (synthesis["p50"],synthesis["p90"],synthesis["p95"],synthesis["p99"],synthesis["max"]) == (20000,36000,38000,None,40000)
    assert summary["exceedances"]["synthesis"] == {"6000":34,"8000":32,"10000":30,"12000":28}
    assert summary["exceedances"]["total_request"] == {"7500":33,"10000":30,"15000":25,"20000":20}
    assert summarize(rows * 3)["distributions_ms"]["synthesis"]["p99"] == 40000


@pytest.mark.parametrize("name,accepted,rejected", [("S6",0,1),("S8",0,1),("S10",0,1),("S12",1,0),("REMAINING",1,0)])
def test_counterfactual_late_return(name, accepted, rejected):
    row = normalize(observation())
    result = replay([row])["candidates"][name]
    assert result["accepted_results"] == accepted
    assert result["stage_cap_rejected_results"] == result["late_result_count"] == rejected
    assert result["overall_deadline_rejected_results"] == 0
    assert result["acceptance_rate"] == accepted
    assert result["max_accepted_total_latency_ms"] == (13663.03 if accepted else None)


@pytest.mark.parametrize("duration,total,remaining,decision", [
    (6000,8000,18000,"stage_cap_rejected"), (5999,8000,18000,"accepted"),
    (6000,8000,6000,"overall_deadline_rejected"),(1000,20000,1500,"overall_deadline_rejected"),
    (19000,19500,19500,"stage_cap_rejected")])
def test_replay_boundaries(duration,total,remaining,decision):
    row = normalize(observation(duration=duration,total=total,remaining=remaining))
    # Completion evidence is independent of the current status used by the test.
    row["status"] = "completed"
    row["classification"] = None
    assert replay([row])["candidates"]["S6"]["decisions"][0]["decision"] == decision
    if duration == 19000:
        assert replay([row])["candidates"]["REMAINING"]["accepted_results"] == 1


@pytest.mark.parametrize("missing", ["remaining_request_budget_before_synthesis_ms","retry_attempts_total",
    "lower_layer_retries_configured","total_request_ms","request_deadline_ms"])
def test_unknown_not_fabricated(missing):
    row = normalize(observation())
    row[missing] = None
    result = replay([row])["candidates"]["REMAINING"]
    assert result["unknown_or_not_replayable"] == 1 and result["acceptance_rate"] is None
    assert result["max_accepted_total_latency_ms"] is None


def test_missing_snapshot_and_censored_timeout():
    row = normalize(dict(scenario_id="case",status="failed"))
    assert row["classification"] == "UNKNOWN"
    assert set(row["component_latency_ms"].values()) == {None}
    assert summarize([row])["distributions_ms"]["synthesis"]["missing"] == 1
    row = normalize(observation(terminal_failure_category="TIMEOUT"))
    row["late_completion"] = False
    assert replay([row])["candidates"]["REMAINING"]["unknown_or_not_replayable"] == 1


def test_observed_overall_exhaustion_overrides_shorter_wrapper_duration():
    row = normalize(observation(deadline_exhausted=True))
    assert replay([row])["candidates"]["REMAINING"]["overall_deadline_rejected_results"] == 1


def test_historical_population_separation_and_tool_loop_exclusion():
    a = observation(); a.update(dataset="functional",source="production",candidate="L")
    b = deepcopy(a); b.update(dataset="safety")
    c = deepcopy(a); c.update(source="controlled")
    d = deepcopy(a); d.update(candidate="R")
    populations = historical_populations([("history",dict(observations=[a,b,c,d]))])
    assert len(populations) == 4 and all(p["summary"]["request_count"] == 1 for p in populations)
    legacy = dict(scenario_id="case",components=[dict(component="agent",latency_ms=9000,
                  model_responses=2,context_characters=dict(tool_definitions=100))])
    assert normalize(legacy)["component_latency_ms"]["synthesis"] is None
    legacy["components"][0].update(model_responses=1,context_characters=dict(tool_definitions=0))
    assert normalize(legacy)["component_latency_ms"]["synthesis"] == 9000


def test_no_mutation_and_no_payloads():
    source = observation()
    source["production_telemetry"].update(headers={"Authorization":"secret-value"},customer="private customer")
    original = deepcopy(source)
    row = normalize(source)
    before = deepcopy(row)
    replay([row]); summarize([row])
    assert source == original and row == before
    assert "secret-value" not in json.dumps(row) and "private customer" not in json.dumps(row)


def test_router_stage_cap_classification():
    raw = observation()
    t = raw["production_telemetry"]
    t.update(terminal_failure_component="primary_router",total_latency_ms=14000)
    t["component_spans"][0].update(duration_ms=14000,configured_stage_cap_ms=13000)
    assert normalize(raw)["classification"] == "STAGE_ALLOWANCE_EXCEEDED"


def test_analysis_cli_has_no_runtime_imports_or_policy_mutation(tmp_path):
    dataset = json.loads((ROOT / "evals/datasets/functional.json").read_text())
    rows = []
    for rep in range(1,6):
        for s in dataset:
            if s["tier"] == "smoke":
                row = observation(); row.update(scenario_id=s["id"],repetition=rep); rows.append(row)
    source = tmp_path / "raw.json"
    source.write_text(json.dumps(dict(complete=True,observations=rows)))
    protected = [*list((ROOT / "src/agent").glob("*.py")), ROOT / "config/quality-gates.yaml",
                 ROOT / "config/runtime-reliability.json", ROOT / "evals/datasets/functional.json",source]
    before = {str(p):p.read_bytes() for p in protected}
    code = '''
import sys, importlib.abc, runpy
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {"openai", "agents", "deepeval", "src.agent"} or fullname.startswith("src.agent."):
            raise AssertionError("Analysis imported runtime: " + fullname)
sys.meta_path.insert(0, BlockRuntime())
sys.argv = ["analyze_synthesis_latency.py", "--input", sys.argv[1], "--output-dir", sys.argv[2], "--historical"]
runpy.run_path("scripts/analyze_synthesis_latency.py", run_name="__main__")
'''
    result = subprocess.run([sys.executable,"-c",code,str(source),str(tmp_path / "analysis")],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    campaign = json.loads((tmp_path / "analysis/campaign.json").read_text())
    assert campaign["status"] == "COMPLETE" and len(campaign["observations"]) == 40
    assert all(p.read_bytes() == before[str(p)] for p in protected)
