"""Deterministic enforced release tests; no model, provider or judge execution."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.agentguard.structural_release import (load_release_spec, observation, assess_gate,
    evaluate_bundle, configuration_observations, transport_evidence)
from src.agentguard.release_readiness import INVARIANTS

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_release_spec(ROOT/'config/release-gates.yaml')
METRICS = [g['metric'] for g in SPEC['candidates']]


def evidence():
    return {g['metric']:[observation(deepcopy(g['expected']),'offline controlled population',24)] for g in SPEC['candidates']}


def bundle(obs=None,**kwargs):
    return evaluate_bundle(SPEC,evidence() if obs is None else obs,{'quality_gates':{}},
        **({'deployment':{'active':True,'max_workers':5,'queue_capacity':5}}|kwargs))


@pytest.mark.parametrize('name',METRICS)
def test_every_promoted_gate_pass(name):
    r=bundle()
    g=next(g for g in r['gates'] if g['metric']==name)
    assert g['status']=='PASS' and g['classification']=='NEW_ENFORCED'
    assert r['decision']=='PASS' and r['exit_code']==0


@pytest.mark.parametrize('name',METRICS)
def test_every_promoted_gate_fail(name):
    values=evidence()
    if name=='agentguard_retries_disabled':
        values[name][0]['value']['enabled']=True
    else: values[name][0]['value']+=1
    kw={}
    if name in {'max_workers','queue_capacity'}:
        kw['deployment']={'active':True,'max_workers':5,'queue_capacity':5,name:6}
    r=bundle(values,**kw)
    assert next(g for g in r['gates'] if g['metric']==name)['status']=='FAIL'
    assert r['decision']=='FAIL' and r['exit_code']==1


@pytest.mark.parametrize('name',METRICS)
def test_missing_required_evidence_is_no_data(name):
    values=evidence();values.pop(name)
    kw={}
    if name in {'max_workers','queue_capacity'}:
        kw['deployment']={'active':True,'max_workers':5,'queue_capacity':5};kw['deployment'].pop(name)
    r=bundle(values,**kw)
    assert next(g for g in r['gates'] if g['metric']==name)['status']=='NO_DATA'
    assert r['decision']=='INSUFFICIENT_EVIDENCE' and r['exit_code']==1


def test_explicit_inactive_vs_unknown_concurrency():
    r=bundle(deployment={'active':False})
    assert r['decision']=='PASS'
    assert all(g['status']=='NOT_APPLICABLE' for g in r['gates'] if g['metric'] in {'max_workers','queue_capacity'})
    assert bundle(deployment=None)['decision']=='INSUFFICIENT_EVIDENCE'


@pytest.mark.parametrize('missing,decision',[(False,'REVIEW_REQUIRED'),(True,'INSUFFICIENT_EVIDENCE')])
def test_report_only_miss_and_required_no_data_precedence(missing,decision):
    obs=evidence()
    if missing:obs.pop('hidden_retries')
    r=bundle(obs,slo_report={'checks':[{'metric':'request_success','result':'MISS','measurement':{'value':.98}}]})
    assert r['decision']==decision
    assert r['exit_code']==(1 if missing else 0)


def test_statistical_insufficiency_not_enforced_and_no_judge_cost_contamination():
    r=bundle(slo_report={'checks':[{'metric':'tokens_per_request','result':'INSUFFICIENT_DATA','measurement':{'value':852.16}}],
                         'judge_tokens':999999},cost_report={'pricing_mode':'TOKEN_ONLY','judge_tokens':999999})
    assert r['decision']=='PASS' and '999999' not in json.dumps(r)
    assert r['report_only']['cost_mode']=='TOKEN_ONLY'


def test_required_zero_cannot_pass_incomplete_population():
    g=SPEC['candidates'][0]
    assert assess_gate(g,[observation(0,'missing',0)])['status']=='NO_DATA'
    assert assess_gate(g,[observation(0,'partial',complete=False)])['status']=='NO_DATA'
    assert assess_gate(g,[observation(1,'partial',complete=False)])['status']=='FAIL'


@pytest.mark.parametrize('field',['request_deadline_ms','router_allowance_ms','recovery_reserve_ms','synthesis_allowance_ms'])
def test_file_and_resolved_runtime_drift_each_checked(field):
    declared=json.loads((ROOT/'config/runtime-reliability.json').read_text())
    resolved=deepcopy(declared);resolved[field]+=100
    obs=configuration_observations(evidence(),declared,resolved)
    assert next(g for g in bundle(obs)['gates'] if g['metric']==field)['status']=='FAIL'


def test_retry_drift_and_no_lower_layer_inference():
    declared=json.loads((ROOT/'config/runtime-reliability.json').read_text())
    resolved=deepcopy(declared);resolved['model_retry_policy']['max_attempts']=2
    obs=configuration_observations({},declared,resolved)
    assert 'openai_retries_disabled' not in obs and 'agents_sdk_retries_disabled' not in obs
    assert next(g for g in bundle(obs)['gates'] if g['metric']=='agentguard_retries_disabled')['status']=='FAIL'


def test_missing_stale_transport_cannot_supply_zeros():
    obs,_,reasons=transport_evidence({},dict(git_commit='x',source_fingerprint='y',sdk_versions={}))
    assert reasons and all(v==[] for v in obs.values())
    assert bundle(obs)['decision']=='INSUFFICIENT_EVIDENCE'


def test_existing_gate_failure_and_deferred_latency_preserved():
    config={'quality_gates':{'functional_accuracy':{'minimum':.95},'p95_latency_ms':{'maximum':7500}}}
    quality={'checks':[{'metric':'functional_accuracy','actual':None,'threshold':.95,'comparison':'>=','passed':False},
        {'metric':'p95_latency_ms','actual':9000,'threshold':7500,'comparison':'<=','passed':None,'enforced':False}]}
    r=evaluate_bundle(SPEC,evidence(),config,quality_result=quality,deployment={'active':False})
    assert r['decision']=='FAIL'
    assert r['gates'][-1]['status']=='NOT_APPLICABLE'
    assert r['gates'][-2]['classification']=='EXISTING_ENFORCED'


def test_statistical_gate_promotion_rejected(tmp_path):
    import yaml
    doc=deepcopy(SPEC);doc['candidates'][0]['metric']='request_success'
    path=tmp_path/'gates.yaml';path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError):load_release_spec(path)


def test_ci_uses_existing_single_execution_runner_with_enforced_bundle():
    text=(ROOT/'.github/workflows/agentguard.yml').read_text()
    assert 'python scripts/run_agentguard_eval.py --suite smoke --release-qualification' in text
    assert 'AGENTGUARD_PRODUCTION_TRANSPORT_REPORT' in text
    assert 'analyze_slo_baseline.py' not in text


def transport_fixture():
    identity=dict(git_commit='local',source_fingerprint='same',sdk_versions={'openai':'pinned'})
    policy=json.loads((ROOT/'config/runtime-reliability.json').read_text())
    rows,http=[],[]
    for i in range(3):
        operation={'tool':'get_order_status','arguments':[['order_id','ORDER']], 'mode':'required'}
        rows.append(dict(tag=str(i),budget_id=str(i),owner=[1,2],error=None,tools=['ORDER'],
            effective_sdk_retries=[0,0],effective_openai_retries=[0,0],dispatch_owners=[[1,2],[1,2]],
            output='ORDER: processing',execution=dict(operations=[dict(operation=operation,invoked=True,output={'order':{'order_id':'ORDER','status':'processing'}})],
                plan={'authorized_bindings':[{'tool':'get_order_status','order_id':'ORDER'}]},prohibited_operation_attempts=[]),
            telemetry=dict(request_id=str(i),external_label=str(i),effective_runtime_policy=policy,
                component_spans=[{'attempts':[{'attempt_number':1},{'attempt_number':1}]}])))
        http.extend([dict(tag=str(i),retry='0'),dict(tag=str(i),retry='0')])
    exp=dict(mode='production',count=1,rows=rows,http=http,errors=[],loop_errors=[],workers_alive=0,server_threads_alive=0,server_errors=[])
    return dict(identity,kind='offline_production_transport',experiments=[exp]),identity


@pytest.mark.parametrize('metric',sorted(INVARIANTS))
def test_adapter_detects_each_structural_violation(metric):
    doc,identity=transport_fixture();e=doc['experiments'][0];r=e['rows'][0]
    if metric=='fabrication_violations':r['output']='ORDER: delivered'
    if metric=='authorization_violations':r['execution']['plan']['authorized_bindings']=[]
    if metric=='hidden_retries':e['http'][0]['retry']='1'
    if metric=='duplicate_operations':r['execution']['operations']*=2
    if metric=='request_id_collision':e['rows'][1]['budget_id']=r['budget_id']
    if metric=='mixed_tool_result':r['execution']['operations'][0]['output']['order']['order_id']='OTHER'
    if metric=='telemetry_contamination':r['telemetry']['request_id']='OTHER'
    obs,_,_=transport_evidence(doc,identity,(1,))
    gate=next(g for g in SPEC['candidates'] if g['metric']==metric)
    assert assess_gate(gate,obs[metric])['status']=='FAIL'


def test_adapter_complete_pass_and_missing_metadata_not_fake_violation():
    doc,identity=transport_fixture()
    obs,_,notes=transport_evidence(doc,identity,(1,))
    assert not notes
    assert all(assess_gate(g,obs[g['metric']])['status']=='PASS' for g in SPEC['candidates'] if g['source']!='concurrency')
    doc['experiments'][0]['rows'][0]['telemetry']={}
    doc['experiments'][0]['http'][0].pop('retry')
    obs,_,_=transport_evidence(doc,identity,(1,))
    for metric in ('telemetry_contamination','hidden_retries'):
        gate=next(g for g in SPEC['candidates'] if g['metric']==metric)
        assert assess_gate(gate,obs[metric])['status']=='NO_DATA'


def test_current_captured_violations_supplement_offline_qualification():
    from types import SimpleNamespace
    from src.agentguard.structural_release import add_record_violations
    record=SimpleNamespace(production_telemetry={'request_id':'one','retry_attempts_total':1},execution=None,
        tool_calls=[{'name':'get_order_status','arguments':{'order_id':'ORD-1001'}}],
        tool_outputs=[{'name':'get_order_status','output':{'order':{'order_id':'ORD-1001','status':'processing'}}}],
        final_output='ORD-1001 was delivered.')
    obs=evidence()
    coverage=add_record_violations(obs,[record,record])
    result=bundle(obs)
    for metric in ('fabrication_violations','hidden_retries','request_id_collision'):
        assert next(g for g in result['gates'] if g['metric']==metric)['status']=='FAIL'
    assert not coverage[0]['transport_dispatch_coverage']
