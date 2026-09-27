"""Shadow release design tests: synthetic evidence only, no provider imports/calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from scripts.analyze_release_readiness import main
from src.agentguard.quality_gate import load_quality_gate_config
from src.agentguard.release_readiness import (INVARIANTS, READINESS, SECTIONS,
    evaluate_release_readiness, existing_gate_rows, load_candidate_spec)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def spec():
    return load_candidate_spec(ROOT/'config/release-gates-candidate.yaml')


@pytest.fixture
def config():
    return load_quality_gate_config(ROOT/'config/quality-gates.yaml')


def check(metric, value=0, *, status='PASS', missing=0, strength='MODERATE', population='concurrency/stage_1'):
    return dict(metric=metric, population=population, result=status, reason='retained offline evidence',
        measurement={'value':value,'missing':missing}, evidence_strength=strength, status='PROVISIONAL',
        comparison='<=',target=0,observed_comparison=status=='PASS')


def inputs(config):
    checks = [dict(metric=m, threshold=next(iter(rule.values())), comparison='>=' if 'minimum' in rule else '<=',
        actual=next(iter(rule.values())),passed=True) for m,rule in config['quality_gates'].items()]
    policy = json.loads((ROOT/'config/runtime-reliability.json').read_text())
    policy['lower_layer_retry_policy'] = {'agents_sdk_max_retries':0,'openai_client_max_retries':0}
    return dict(quality_result={'checks':checks}, policy_snapshots={'offline effective snapshot':policy},
        concurrency_profiles={'offline deployment':{'active':True,'max_workers':5,'queue_capacity':5}})


def run(spec,config, *, checks=None, **overrides):
    kwargs=inputs(config)|overrides
    return evaluate_release_readiness(spec, {'checks':checks if checks is not None else [check(m) for m in INVARIANTS]},config,**kwargs)


def rows(report):
    return [r for section in report['sections'].values() for r in section]


def metric(report,name):
    return next(r for r in rows(report) if r['metric']==name)


def test_complete_scoped_shadow_pass_and_all_sections(spec,config):
    r=run(spec,config)
    assert r['advisory_decision']=='PASS'
    assert r['shadow_counts']=={'WOULD_PASS':16,'WOULD_FAIL':0,'NO_DATA':0}
    assert set(r['sections'])==set(SECTIONS)
    assert all(x['readiness_class'] in READINESS for x in rows(r))
    assert all(all(k in x for k in ('metric','expected','actual','status','readiness_class','evidence_strength','source_population','reason')) for x in rows(r))


@pytest.mark.parametrize('name',sorted(INVARIANTS))
def test_each_invariant_zero_pass_positive_fail_even_if_prior_slo_label_wrong(spec,config,name):
    checks=[check(m,1 if m==name else 0) for m in INVARIANTS]
    r=run(spec,config,checks=checks)
    assert r['advisory_decision']=='FAIL'
    assert metric(r,name)['shadow_result']=='WOULD_FAIL'
    assert r['ci_exit_code']==0 and not r['changes_current_release_decision']


def test_known_violation_not_hidden_by_partial_coverage(spec,config):
    r=run(spec,config,checks=[check('hidden_retries',1,missing=1,status='INSUFFICIENT_DATA')])
    assert metric(r,'hidden_retries')['status']=='FAIL'


def test_missing_required_scope_not_a_pass(spec,config):
    r=run(spec,config,checks=[])
    assert r['advisory_decision']=='INSUFFICIENT_EVIDENCE'
    assert r['shadow_counts']['NO_DATA']==7
    assert metric(r,'fabrication_violations')['actual'] is None


def test_zero_with_missing_coverage_is_no_data(spec,config):
    r=run(spec,config,checks=[check('hidden_retries',missing=1,status='INSUFFICIENT_DATA')])
    assert metric(r,'hidden_retries')['shadow_result']=='NO_DATA'


def test_weak_invariant_evidence_cannot_qualify_candidate_from_pass_alone(spec,config):
    r=run(spec,config,checks=[check('hidden_retries',strength='WEAK')])
    assert metric(r,'hidden_retries')['shadow_result']=='NO_DATA'
    assert metric(r,'hidden_retries')['evidence_strength']=='WEAK'


@pytest.mark.parametrize('value,status,decision',[(.98,'MISS','REVIEW_REQUIRED'),(1,'PASS','PASS'),(.98,'INSUFFICIENT_DATA','PASS')])
def test_report_only_never_promoted_or_release_blocking(spec,config,value,status,decision):
    c=check('request_success',value,status=status,strength='WEAK');c.update(target=.99,comparison='>=')
    r=run(spec,config,checks=[*[check(m) for m in INVARIANTS],c])
    assert r['advisory_decision']==decision
    assert metric(r,'request_success')['readiness_class']=='REPORT_ONLY'
    assert metric(r,'request_success')['slo_status']=='PROVISIONAL'
    assert r['ci_exit_code']==0


@pytest.mark.parametrize('key,value', [('max_workers',6),('queue_capacity',6)])
def test_envelope_violation(spec,config,key,value):
    p={'active':True,'max_workers':5,'queue_capacity':5,key:value}
    r=run(spec,config,concurrency_profiles={'deployment':p})
    assert r['advisory_decision']=='FAIL' and metric(r,key)['shadow_result']=='WOULD_FAIL'


@pytest.mark.parametrize('value', [None,True,5.5,-1])
def test_invalid_worker_evidence_is_not_pass(spec,config,value):
    r=run(spec,config,concurrency_profiles={'deployment':{'active':True,'max_workers':value,'queue_capacity':5}})
    assert metric(r,'max_workers')['shadow_result']=='NO_DATA'


def test_explicit_inactive_concurrency_na_unknown_not_na(spec,config):
    r=run(spec,config,concurrency_profiles={'sequential':{'active':False}})
    assert metric(r,'max_workers')['readiness_class']=='NOT_APPLICABLE'
    assert r['advisory_decision']=='PASS'
    r=run(spec,config,concurrency_profiles={})
    assert metric(r,'max_workers')['status']=='INSUFFICIENT_DATA'


@pytest.mark.parametrize('key,value', [('enabled',True),('max_attempts',2),('shared_extra_attempts_per_request',1)])
def test_application_retry_policy_mismatch(spec,config,key,value):
    kw=inputs(config);kw['policy_snapshots']['offline effective snapshot']['model_retry_policy'][key]=value
    r=run(spec,config,**kw)
    assert metric(r,'agentguard_retries_disabled')['shadow_result']=='WOULD_FAIL'


@pytest.mark.parametrize('key,metric_name',[('agents_sdk_max_retries','agents_sdk_retries_disabled'),('openai_client_max_retries','openai_retries_disabled')])
def test_each_lower_layer_retry_setting_checked_separately(spec,config,key,metric_name):
    kw=inputs(config);kw['policy_snapshots']['offline effective snapshot']['lower_layer_retry_policy'][key]=2
    assert metric(run(spec,config,**kw),metric_name)['status']=='FAIL'


@pytest.mark.parametrize('field',['request_deadline_ms','router_allowance_ms','recovery_reserve_ms','synthesis_allowance_ms'])
def test_each_reliability_budget_configuration(spec,config,field):
    kw=inputs(config);kw['policy_snapshots']['offline effective snapshot'][field]+=1
    assert metric(run(spec,config,**kw),field)['shadow_result']=='WOULD_FAIL'


def test_lower_layer_absence_never_inferred_from_application_disable(spec,config):
    kw=inputs(config);del kw['policy_snapshots']['offline effective snapshot']['lower_layer_retry_policy']
    r=run(spec,config,**kw)
    assert metric(r,'agents_sdk_retries_disabled')['shadow_result']=='NO_DATA'
    assert metric(r,'openai_retries_disabled')['shadow_result']=='NO_DATA'


def test_existing_enforcement_false_unavailable_and_threshold_attribution(config):
    result={'checks':[dict(metric='functional_accuracy',actual=None,passed=False,threshold=.95,comparison='>=')]}
    r=existing_gate_rows(config,result)
    assert r[0]['status']=='FAIL' and r[0]['readiness_class']=='EXISTING_ENFORCED'
    result['checks'][0]['threshold']=.9
    assert existing_gate_rows(config,result)[0]['status']=='INSUFFICIENT_DATA'


def test_deferred_latency_uses_existing_sequential_verdict(config):
    result={'checks':[dict(metric='p95_latency_ms',actual=9000,passed=None,enforced=False,threshold=7500,comparison='<=')]}
    r=next(x for x in existing_gate_rows(config,result) if x['metric']=='p95_latency_ms')
    assert r['status']=='NOT_APPLICABLE'
    p={'qualification':dict(passed=True,p95_latency_ms=3100,threshold_ms=7500)}
    r=next(x for x in existing_gate_rows(config,result,performance=p) if x['metric']=='p95_latency_ms')
    assert r['status']=='PASS' and r['actual']==3100


def test_missing_existing_quality_cannot_be_inferred_from_load_success(spec,config):
    r=run(spec,config,quality_result=None)
    assert metric(r,'functional_accuracy')['status']=='INSUFFICIENT_DATA'
    assert r['advisory_decision']=='INSUFFICIENT_EVIDENCE'


def test_source_population_and_weak_evidence_preserved_no_usd_or_judge_tokens(spec,config):
    c=check('tokens_per_request',852.16,strength='WEAK',population='current_normal_consumption')
    c.update(target=1000)
    payload={'checks':[c], 'judge_usage':{'total_tokens':999999}, 'pricing_mode':'TOKEN_ONLY'}
    r=evaluate_release_readiness(spec,payload,config,**inputs(config))
    m=metric(r,'tokens_per_request')
    assert m['actual']==852.16 and m['source_population']=='current_normal_consumption'
    assert m['evidence_strength']=='WEAK' and m['readiness_class']=='REPORT_ONLY'
    assert '999999' not in json.dumps(r)


def test_statistical_candidate_promotion_and_activation_rejected(spec,tmp_path):
    for modification in ('metric','mode'):
        doc=deepcopy(spec)
        if modification=='mode':doc['mode']='ENFORCED'
        else:doc['candidates'][0]['metric']='request_success'
        p=tmp_path/f'{modification}.yaml';p.write_text(yaml.safe_dump(doc))
        with pytest.raises(ValueError):load_candidate_spec(p)


def test_cli_fail_is_advisory_and_preserves_ci_and_existing_files(tmp_path):
    tracked=[ROOT/'config/quality-gates.yaml',ROOT/'config/runtime-reliability.json',ROOT/'scripts/run_agentguard_eval.py',*ROOT.glob('.github/workflows/*')]
    before={p:p.read_bytes() for p in tracked}
    slo=tmp_path/'slo.json';slo.write_text(json.dumps({'checks':[check('hidden_retries',1)]}))
    absent=tmp_path/'absent.json';out=tmp_path/'report.json'
    args=['--slo',str(slo),'--concurrency',str(absent),'--performance',str(absent),'--output',str(out)]
    assert main(args)==0
    result=json.loads(out.read_text())
    assert result['advisory_decision']=='FAIL' and result['ci_exit_code']==0
    assert {p:p.read_bytes() for p in tracked}==before
    assert any(s.get('sha256') for s in result['sources'])
    with pytest.raises(FileExistsError):main(args)
