"""14B.1 offline contracts: observers consume evidence, never execute it."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agentguard import lineage as l, lineage_adapters as adapters
from src.agentguard.artifact_validation import schema
from src.agentguard.invocation import Invocation, invocation_entry, current_invocation
from src.agentguard.metric_registry import METRICS, aggregate
from src.agentguard import evaluation_record as er
from src.agentguard.failure_evidence import retain_failure
from test_run_agentguard_eval import run_setup, runner

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONAL = dict(id="f", input="PRIVATE_PROMPT", tier="smoke", expected_output="PRIVATE_RESPONSE")
SAFETY = dict(id="s", input="PRIVATE_PROMPT", tier="smoke", expected_behavior="protect_data")


@pytest.fixture
def active(tmp_path):
    token = l._active.set(None)
    run = l.start_run(tmp_path,suite="smoke",functional=[FUNCTIONAL],safety=[SAFETY],execution_mode="offline_fixture")
    yield run
    l._active.reset(token)


def record(telemetry=True):
    return SimpleNamespace(execution_error=None, input="PRIVATE_PROMPT",final_output="PRIVATE_RESPONSE",tool_calls=[],
        latency_ms=120, input_tokens=10, output_tokens=2,total_tokens=12,
        production_telemetry=dict(request_id="request-1",total_latency_ms=100,retry_attempts_total=0,
            observed_usage=dict(input_tokens=10,output_tokens=2,total_tokens=12),usage_completeness="COMPLETE",
            terminal_failure_category=None, component_spans=[dict(component="primary_router",duration_ms=30,resolved_model="observed-model"),
            dict(component="synthesis",duration_ms=60,resolved_model="observed-model"),dict(component="required_execution",duration_ms=10)]) if telemetry else None)


def finish(active):
    active.finish(state="INCOMPLETE")
    return l.load_run(active.path)["results"]


def test_functional_observation_and_models(active):
    score = SimpleNamespace(functional_pass=True,tool_pass=True,argument_pass=False,factual_grounding_pass=None)
    semantic = SimpleNamespace(answer_relevancy_score=.9,correctness_score=.8,hallucination_score=None,
                               answer_relevancy_reason="SECRET_SENTINEL")
    active.capture(FUNCTIONAL,record=record(),deterministic=score,semantic=semantic)
    result = finish(active)
    row = result["observations"][0]
    assert row["scenario_fingerprint"] == active.scenario_rows["f"]["scenario_fingerprint"]
    assert row["population"] == "functional" and row["completed"] is True
    assert row["deterministic"]["argument_pass"] is False
    assert row["semantic"]["answer_relevancy"]["score"] == .9
    assert row["semantic"]["faithfulness"]["score"] is None
    assert row["semantic"]["faithfulness"]["evaluator_available"] is False
    assert row["semantic"]["correctness"]["judge_identity"]["resolved"] == "UNKNOWN"
    assert row["operational"]["retry_count"] == 0
    assert row["operational"]["router_latency_ms"] == 30
    assert row["operational"]["synthesis_latency_ms"] == 60
    assert row["operational"]["required_operation_latency_ms"] == 10
    assert row["operational"]["recovery_latency_ms"] is None
    assert result["observed_model_identities"] == [dict(model="observed-model",immutable_revision="UNKNOWN",request_id="request-1",scenario_id="f",repetition=1)]
    assert all(secret not in json.dumps(result) for secret in ("PRIVATE_PROMPT","PRIVATE_RESPONSE","SECRET_SENTINEL"))


def test_reliability_snapshot_survives_partial_measurement(active):
    from src.agentguard.reliability import measure_request
    from src.agentguard.reliability_policy import PolicyCandidate
    from src.agent.telemetry import ProductionExecutionTelemetry, ComponentSpan
    attempt = active.observe("f", completed=False)
    context = SimpleNamespace(production_telemetry=ProductionExecutionTelemetry(
        request_id="request-1", total_latency_ms=100,
        component_spans=[ComponentSpan("synthesis", 0, duration_ms=60, resolved_model="observed-model")]))
    execute = Mock(return_value=SimpleNamespace(context_wrapper=context))
    ticks = iter([1.0, 1.12])
    measured = measure_request(FUNCTIONAL, candidate=PolicyCandidate("baseline_no_retry"),
                               run=execute, clock=lambda: next(ticks))
    attempt["completed"] = True
    active.capture(FUNCTIONAL, attempt=attempt, measurement=measured)
    result = finish(active)
    assert execute.call_count == 1
    operational = result["observations"][0]["operational"]
    assert operational["request_id"] == "request-1"
    assert operational["model_identities"][0]["model"] == "observed-model"
    assert operational["synthesis_latency_ms"] == 60
    assert result["observed_model_identities"][0]["request_id"] == "request-1"


def test_safety_semantic_composite_separate(active):
    score = SimpleNamespace(passed=True,prompt_injection_label="followed_injection",prompt_injection_verdict="resisted",
        prompt_injection_classification="DETERMINISTIC_OVERRIDE_RESISTED",factual_grounding_pass=True,
        injection_evidence=SimpleNamespace(required_tools_satisfied=True,authoritative_sources=[("secret", "customer")]),
        failures=["SECRET_SENTINEL"])
    active.capture(SAFETY,record=record(),safety=score)
    row = finish(active)["observations"][0]
    assert row["population"] == "safety"
    assert row["safety"]["semantic_classifier_result"] == "followed_injection"
    assert row["safety"]["composite_verdict"] == "resisted"
    assert row["safety"]["final_pass"] is True
    assert row["safety"]["deterministic_evidence"]["required_tools_satisfied"] is True
    assert "SECRET_SENTINEL" not in json.dumps(row) and "customer" not in json.dumps(row)


def test_missing_measurements_and_denominators(active):
    empty = record(False); empty.input_tokens=empty.output_tokens=empty.total_tokens=None
    active.capture(FUNCTIONAL,record=empty)
    result = finish(active)
    metrics = {r["metric_id"]:r for r in result["continuous_aggregates"] if r["population"] == "functional"}
    for name in ("functional_accuracy","production_tokens","retry_count","answer_relevancy"):
        assert metrics[name]["value"] is None and metrics[name]["denominator"] == metrics[name]["sample_count"] == 0
        assert metrics[name]["missing_count"] == 1
        assert metrics[name]["population_complete"] is False
    assert metrics["total_latency_ms"]["value"] == 120


def test_failed_observation_linkage(active):
    active.observe("f",completed=False)
    retain_failure(FUNCTIONAL,ValueError("SECRET_SENTINEL"))
    results = finish(active)
    row = results["observations"][0]
    assert row["completed"] is False
    assert row["failure_evidence_reference"] == dict(**active.reference,scenario_id="f",repetition=1)
    assert results["completion_state"] == "INCOMPLETE"
    assert "SECRET_SENTINEL" not in json.dumps(results)


def test_registry_is_versioned_and_has_no_thresholds():
    for metric in METRICS:
        assert metric["version"] == 1 and metric["required_lineage"]
        assert {"metric_id","family","unit","aggregation","population","direction"} <= metric.keys()
        assert not {"threshold","drift_threshold","severity"} & metric.keys()
    semantic = next(m for m in METRICS if m["metric_id"] == "correctness")
    assert "judge_identity_known_equal" in semantic["required_lineage"]


def test_repetition_identity(tmp_path):
    m, rows = adapters.build_manifest(tmp_path,suite="performance",functional=[FUNCTIONAL],safety=[],repetitions=5,execution_mode="offline_fixture")
    run = l.RunArtifacts(tmp_path,m,rows)
    for _ in range(5):
        attempt=run.observe("f"); run.capture(FUNCTIONAL,attempt=attempt,record=record())
    run.finish()
    results = l.load_run(run.path)["results"]
    assert [r["repetition"] for r in results["observations"]] == [1,2,3,4,5]
    assert [r["repetition"] for r in results["executions"]] == [1,2,3,4,5]
    assert results["protocol"]["suite"] == "performance" and results["protocol"]["repetitions"] == 5
    metric = next(r for r in results["continuous_aggregates"] if r["metric_id"] == "production_tokens" and r["population"] == "functional")
    assert metric["denominator"] == metric["sample_count"] == 5 and metric["value"] == 12


@pytest.mark.parametrize("change,expected", [(False,"MATCH"),(True,"CHANGED"),(None,"UNKNOWN")])
def test_source_integrity_preserves_manifest(tmp_path,monkeypatch,change,expected):
    start=dict(source_fingerprint="a"*64,dirty_state="CLEAN",git_commit="b"*40)
    monkeypatch.setattr(adapters,"source_identity",lambda root:dict(start))
    m,rows=adapters.build_manifest(tmp_path,suite="smoke",functional=[FUNCTIONAL],safety=[])
    run=l.RunArtifacts(tmp_path,m,rows);before=(run.path/'manifest.json').read_bytes()
    end=dict(start)
    if change is True:end["source_fingerprint"]="c"*64
    if change is None:end["git_commit"]="UNKNOWN"
    monkeypatch.setattr(l,"source_identity",lambda root:end)
    run.observe("f");run.finish()
    loaded=l.load_run(run.path)
    assert loaded["results"]["source_integrity"]["state"] == expected
    assert (run.path/'manifest.json').read_bytes() == before


def rewrite_results(run, mutate):
    p=run.path/'results.json';d=json.loads(p.read_text());mutate(d);p.write_text(json.dumps(d))
    p=run.path/'completion.json';c=json.loads(p.read_text());c['results_digest']=l.fingerprint('evaluation-results',d);p.write_text(json.dumps(c))


@pytest.mark.parametrize("kind", ["count","duplicate","unknown","fingerprint","run_id","version","repetition","aggregate","source","missing"])
def test_validation_rejects_internally_rehashed_invalid_artifacts(active,kind):
    active.capture(FUNCTIONAL,record=record());active.capture(SAFETY,record=record());active.finish()
    def mutate(d):
        if kind=='count':d['executed_scenario_count']=99
        elif kind=='duplicate':d['observations'][1]=deepcopy(d['observations'][0])
        elif kind=='unknown':d['observations'][0]['scenario_id']='unknown'
        elif kind=='fingerprint':d['observations'][0]['scenario_fingerprint']='a'*64
        elif kind=='run_id':d['observations'][0]['run_id']='b'*32
        elif kind=='version':d['observation_contract_version']=2
        elif kind=='repetition':d['observations'][0]['repetition']=2
        elif kind=='aggregate':d['continuous_aggregates'][0]['value']=999
        elif kind=='source':d['source_integrity']['end_fingerprint']='b'*64
        elif kind=='missing':d['executions'].pop();d['observations'].pop();d['executed_scenario_count']=1;d['completed_scenario_count']=1
    rewrite_results(active,mutate)
    with pytest.raises(ValueError):l.load_run(active.path)


def test_old_v1_artifacts_remain_readable(active):
    active.capture(FUNCTIONAL,record=record());active.capture(SAFETY,record=record());active.finish()
    def legacy(d):
        for key in ['observations','observation_contract_version','continuous_aggregates','metric_registry_version','source_integrity','protocol']:d.pop(key)
    rewrite_results(active,legacy)
    p=active.path/'completion.json';d=json.loads(p.read_text());d.pop('source_integrity');p.write_text(json.dumps(d))
    loaded=l.load_run(active.path)
    assert loaded['observation_lineage_status']=='PARTIAL_LINEAGE'
    assert loaded['observation_comparability']=='LIMITED_COMPARABILITY'


def receipt_for(root):
    paths=list((root/'reports/continuous_evaluation/invocations').glob('*/receipt.json'))
    assert len(paths)==1
    return json.loads(paths[0].read_text())


def test_receipt_created_before_evaluation_exact_handoff(run_setup,monkeypatch):
    original=run_setup.execute.side_effect
    def execute(scenario):
        receipt=current_invocation()
        assert receipt is not None and receipt.path.exists()
        assert json.loads(receipt.path.read_text())['process_state']=='RUNNING'
        assert receipt.document['evaluation_run_id']==l.current_run().manifest.run_id
        return next(original)
    run_setup.execute.side_effect=execute
    assert runner.main()==0
    receipt=receipt_for(runner.PROJECT_ROOT)
    schema(receipt,'evaluation-invocation.schema.json')
    assert receipt['process_state']=='COMPLETED' and receipt['process_exit_code']==0
    results=l.load_run(receipt['evaluation_artifact_path'])['results']
    assert results['run_id']==receipt['evaluation_run_id']
    assert len(results['observations'])==run_setup.execute.call_count==4
    assert receipt['comparison_id'] is receipt['baseline_id'] is None
    assert results['aggregate_results']['quality_result']['passed'] is True


def test_setup_failure_receipt_without_manifest(run_setup,monkeypatch):
    monkeypatch.setattr(runner,'load_datasets',Mock(side_effect=ValueError('SECRET_SENTINEL')))
    assert runner.main()==1
    receipt=receipt_for(runner.PROJECT_ROOT)
    assert receipt['process_state']=='SETUP_FAILED' and receipt['setup_error_type']=='ValueError'
    assert receipt['evaluation_run_id'] is None
    assert 'SECRET_SENTINEL' not in json.dumps(receipt)
    run_setup.execute.assert_not_called()


@pytest.mark.parametrize('cancelled',[False,True])
def test_failed_cancelled_receipts(run_setup,cancelled):
    error=KeyboardInterrupt('SECRET_SENTINEL') if cancelled else ValueError('SECRET_SENTINEL')
    run_setup.execute.side_effect=error
    if cancelled:
        with pytest.raises(KeyboardInterrupt):runner.main()
    else:assert runner.main()==1
    receipt=receipt_for(runner.PROJECT_ROOT)
    assert receipt['process_state']==('CANCELLED' if cancelled else 'FAILED')
    assert receipt['process_exit_code']==(130 if cancelled else 1)
    assert l.load_run(receipt['evaluation_artifact_path'])['completion_state']=='INCOMPLETE'
    assert 'SECRET_SENTINEL' not in json.dumps(receipt)


def test_finalization_error_is_in_receipt(run_setup,monkeypatch):
    monkeypatch.setattr(l.RunArtifacts,'finish',Mock(side_effect=OSError('SECRET_SENTINEL')))
    with pytest.raises(OSError):runner.main()
    receipt=receipt_for(runner.PROJECT_ROOT)
    assert receipt['finalization_error_type']=='OSError' and receipt['process_state']=='FAILED'
    assert 'SECRET_SENTINEL' not in json.dumps(receipt)


def test_linked_release_handoff_and_no_directory_discovery(run_setup,monkeypatch):
    from scripts import qualify_release
    captured={}
    @l.lineage_entry
    def release(root, **kwargs):
        run=l.start_run(root,suite='structural',execution_mode='offline_fixture')
        run.aggregate={'evaluation_lineage':kwargs['evaluation_lineage']}
        run.evaluation_complete=True
        captured['receipt']=current_invocation().path
        captured['release']=run.path
        return {'decision':'PASS','exit_code':0}
    monkeypatch.setattr(qualify_release,'assemble_release',release)
    monkeypatch.setattr(Path,'glob',Mock(side_effect=AssertionError('No directory discovery')))
    assert runner.main(['--release-qualification'])==0
    receipt=json.loads(captured['receipt'].read_text())
    assert receipt['requested_release_qualification'] is True
    assert receipt['linked_release_artifact_path']==str(captured['release'].resolve())
    assert receipt['evaluation_run_id']!=receipt['linked_release_run_id']
    release_result=l.load_run(receipt['linked_release_artifact_path'])['results']
    assert release_result['aggregate_results']['evaluation_lineage']['run_id']==receipt['evaluation_run_id']
    assert run_setup.execute.call_count==4


def test_receipt_created_state_is_published_first(tmp_path,monkeypatch):
    seen=[]
    original=Invocation.save
    def save(self,**changes):
        original(self,**changes)
        seen.append(json.loads(self.path.read_text())['process_state'])
    monkeypatch.setattr(Invocation,'save',save)
    @invocation_entry(suite='smoke',mode='offline_fixture')
    def invoke(argv=None,*,project_root=None):
        assert current_invocation().document['process_state']=='RUNNING'
        return 0
    invoke([],project_root=tmp_path)
    assert seen==['CREATED','RUNNING','COMPLETED']


@pytest.mark.parametrize('kind',['completion_version','result_version','manifest_version','digest','scenario_index'])
def test_artifact_versions_and_digests(active,kind):
    active.capture(FUNCTIONAL,record=record());active.capture(SAFETY,record=record());active.finish()
    name='completion.json' if kind=='completion_version' else 'manifest.json' if kind=='manifest_version' else 'scenarios.json' if kind=='scenario_index' else 'results.json'
    p=active.path/name;doc=json.loads(p.read_text())
    if kind=='completion_version':doc['completion_schema_version']=99
    elif kind=='manifest_version':doc['canonicalization_version']=99
    elif kind=='result_version':doc['result_schema_version']=99
    elif kind=='scenario_index':doc['scenarios'][0]['scenario_fingerprint']='a'*64
    else:doc['aggregate_results']['tampered']=True
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):l.load_run(active.path)


def test_source_fingerprint_includes_business_fixture(tmp_path):
    target=tmp_path/'data/orders.json';target.parent.mkdir();target.write_text('[]')
    before=l.source_identity(tmp_path)
    target.write_text('[{"fixture":1}]')
    assert l.source_identity(tmp_path)['source_fingerprint']!=before['source_fingerprint']


def test_semantic_availability_not_judge_equality(active):
    from src.agentguard.comparability import compare_runs
    active.capture(FUNCTIONAL,record=record(),semantic=SimpleNamespace(answer_relevancy_score=.9,correctness_score=.9,hallucination_score=.9))
    active.capture(SAFETY,record=record());active.finish()
    loaded=l.load_run(active.path)
    result=compare_runs(loaded,loaded)
    assert 'JUDGE_IDENTITY_UNKNOWN' in result['reason_codes']
    assert 'correctness' not in result['eligible_metrics']
