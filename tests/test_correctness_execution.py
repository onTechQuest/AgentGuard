"""Correctness runner owns the override; artifacts remain additive."""
from src.agentguard import lineage as l
from src.agentguard.correctness_execution import load_correctness_policy
from test_run_agentguard_eval import run_setup, runner


def test_runner_records_profile_without_changing_gates(run_setup):
    setup = run_setup
    assert runner.main(["--suite", "smoke"]) == 0
    paths = list((runner.PROJECT_ROOT / "reports/evaluations").iterdir())
    run = l.load_run(paths[0])
    assert run["manifest"]["protocol"]["execution_profile"] == load_correctness_policy().profile_identity()
    assert run["results"]["aggregate_results"]["quality_result"]["passed"] is True
    assert setup.execute.call_count == len(setup.scenarios) + len(setup.safety_scenarios)
    assert setup.gate.call_args.kwargs["latency_mode"] == "report_only"


def test_legacy_protocol_still_readable_and_distinct(tmp_path):
    token = l._active.set(None)
    try:
        scenarios = [dict(id="s", input="fixture")]
        old = l.start_run(tmp_path, suite="smoke", functional=scenarios)
        old.observe("s")
        old.finish()
        legacy = l.load_run(old.path)
        assert "execution_profile" not in legacy["manifest"]["protocol"]
        policy = load_correctness_policy()
        new = l.start_run(tmp_path, suite="smoke", functional=scenarios,
            effective_runtime_policy=policy.snapshot(), execution_profile=policy.profile_identity())
        new.observe("s")
        new.finish()
        current = l.load_run(new.path)
        assert legacy["manifest"]["protocol"] != current["manifest"]["protocol"]
        assert legacy["manifest"]["fingerprints"]["runtime_policy"] != current["manifest"]["fingerprints"]["runtime_policy"]
        from src.agentguard.continuous_comparison import exclusion
        from src.agentguard.metric_registry import METRICS
        metric = next(m for m in METRICS if m["metric_id"] == "total_latency_ms")
        views = [dict(manifest=run["manifest"], observation_contract_version=1, metric_registry_version=1)
                 for run in (legacy, current)]
        assert exclusion(metric, *views) == "OPERATIONAL_PROTOCOL_CHANGED"
    finally:
        l._active.reset(token)
