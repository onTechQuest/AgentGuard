"""Offline design assessments never invoke production, judges, or release gates."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
import yaml

from src.agentguard.slo import (SLOSpec, RequestEvidence, PopulationEvidence, Measurement,
    denominators, error_budget, evaluate_spec, evaluate_scorecard, measure)
from src.agentguard.slo_evidence import from_concurrency, from_cost, from_telemetry, from_diagnostic
from scripts.analyze_slo_baseline import load_spec, build_report, main

ROOT = Path(__file__).resolve().parents[1]


def spec(**changes):
    values = dict(metric='request_success', domain='RELIABILITY', classification='STATISTICAL_SLO',
        population='admitted', target=.99, window='qualification_batch', comparison='>=',
        evidence_strength='WEAK', status='PROVISIONAL', enforcement='REPORT_ONLY', aggregation='rate',
        minimum_samples=1, definition='Observed successful contract', denominator='All admitted', rationale='Offline test')
    return SLOSpec(**(values | changes))


def request(n=0, *, admitted=True, outcome='success', recovery=0, **metrics):
    return RequestEvidence(str(n), admitted, outcome, metrics, recovery)


def population(*rows):
    return PopulationEvidence('offline', requests=list(rows))


def test_spec_covers_taxonomy_classifies_every_metric_and_forbids_new_enforcement():
    doc, specs = load_spec(ROOT/'config/slo-report-only.yaml')
    assert {s.domain for s in specs} == {'CORRECTNESS','SAFETY','RELIABILITY','PERFORMANCE','CAPACITY','CONSUMPTION','ISOLATION'}
    assert all(s.enforcement == 'REPORT_ONLY' and s.rationale and s.denominator for s in specs)
    by = {s.metric:s for s in specs}
    for metric in ('fabrication_violations','authorization_violations','duplicate_operations','telemetry_contamination','hidden_retries'):
        assert by[metric].classification == 'HARD_INVARIANT' and by[metric].target == 0
    assert by['request_success'].classification == 'STATISTICAL_SLO'
    assert by['capacity_rejection_rate'].target is None
    assert by['recovery_success'].target is None
    assert doc['candidate_reliability_objectives'] == [.99,.995,.999]


def test_capacity_denominator_separate_from_admitted_execution_budget():
    rows = [request(i, request_success=1, started=1, completed=1, admission_ratio=1, capacity_rejection_rate=0) for i in range(10)]
    rows += [request(i+10, admitted=False, outcome='capacity_rejected', admission_ratio=0, capacity_rejection_rate=1) for i in range(5)]
    p = population(*rows)
    d = denominators(p)
    assert d['offered'] == 15 and d['admitted'] == 10 and d['expected_capacity_rejections'] == 5
    assert d['success'] == 10 and d['unexpected_failure'] == 0 and d['execution_failure_rate'] == 0
    assert d['capacity_rejection_rate'] == pytest.approx(1/3)
    assert measure(spec(), p).denominator == 10
    assert measure(spec(metric='capacity_rejection_rate',population='offered'), p).value == pytest.approx(1/3)


@pytest.mark.parametrize('outcome', ['provider_failure','rate_limit','deadline_rejection','cancellation','unknown_failure'])
def test_admitted_failures_including_queued_expiry_consume_budget(outcome):
    p = population(request(0, request_success=1), request(1,outcome=outcome,request_success=0))
    assert denominators(p)['execution_failure_rate'] == .5
    assert evaluate_spec(spec(),p)['result'] == 'MISS'


def test_pending_or_unassessed_not_silently_success_or_removed_from_denominator():
    p = population(request(0,request_success=1), request(1,outcome=None))
    result = evaluate_spec(spec(),p)
    assert result['result'] == 'INSUFFICIENT_DATA'
    assert result['measurement']['denominator'] == 2 and result['measurement']['missing'] == 1
    assert denominators(p)['execution_failure_rate'] is None


def test_admitted_only_sample_cannot_claim_a_complete_offered_denominator():
    p = replace(population(request(admission_ratio=1)), admission_census_complete=False)
    assert denominators(p)['offered'] is None and denominators(p)['admission_ratio'] is None
    s = spec(metric='admission_ratio', population='offered')
    assert evaluate_spec(s,p)['result'] == 'INSUFFICIENT_DATA'


def test_missing_start_evidence_does_not_become_zero_starts():
    assert denominators(population(request()))['started'] is None


def test_policy_skips_are_not_missing_and_no_assessments_are_unknown():
    skipped = population(request(prompt_injection_resistance=None))
    s = spec(metric='prompt_injection_resistance')
    assert evaluate_spec(s, skipped)['result'] == 'NOT_APPLICABLE'
    assert evaluate_spec(s, population(request()))['result'] == 'INSUFFICIENT_DATA'


def test_invariant_known_violation_misses_even_with_incomplete_coverage():
    s = spec(metric='duplicate_operations', classification='HARD_INVARIANT', target=0, comparison='<=', aggregation='sum')
    assert evaluate_spec(s,population(request(0,duplicate_operations=1),request(1)))['result'] == 'MISS'
    assert evaluate_spec(s,population(request(0,duplicate_operations=0),request(1)))['result'] == 'INSUFFICIENT_DATA'
    assert evaluate_spec(s,population(request(duplicate_operations=0)))['result'] == 'PASS'


def test_percentile_boundary_and_insufficient_sample():
    p = population(*(request(i,end_to_end_ms=v) for i,v in enumerate([10,20,30,40])))
    s = spec(metric='end_to_end_ms',aggregation='p95',comparison='<=',target=38.5)
    assert evaluate_spec(s,p)['result'] == 'PASS'
    assert evaluate_spec(replace(s,target=38),p)['result'] == 'MISS'
    result = evaluate_spec(replace(s,minimum_samples=100),p)
    assert result['result'] == 'INSUFFICIENT_DATA' and result['observed_comparison'] is True
    assert evaluate_spec(s,replace(p,window='rolling_28d'))['reason'] == 'WINDOW_MISMATCH'


def test_latency_includes_errors_not_only_successes():
    p = population(request(0,end_to_end_ms=100),request(1,outcome='deadline_rejection',end_to_end_ms=20000))
    result = measure(spec(metric='end_to_end_ms',aggregation='p95'),p)
    assert result.samples == 2 and result.value == 19005


def test_recovery_valid_and_normal_guardrail_does_not_penalize_three_calls():
    p = population(request(0,logical_calls_per_request=2,recovery_rate=0),
                   request(1,recovery=1,logical_calls_per_request=3,recovery_rate=1,recovery_success=1))
    assert measure(spec(metric='recovery_rate'),p).value == .5
    s = spec(metric='logical_calls_per_request',population='normal',aggregation='mean',target=2,comparison='==')
    assert evaluate_spec(s,p)['result'] == 'PASS'
    assert measure(spec(metric='recovery_success',population='recovery'),p).value == 1
    assert evaluate_spec(spec(metric='recovery_success',population='recovery'),population(request(recovery=0)))['result'] == 'NOT_APPLICABLE'


def test_extra_normal_call_is_not_filtered_out_to_make_guardrail_pass():
    p = population(request(recovery=0,logical_calls_per_request=3))
    assert evaluate_spec(spec(metric='logical_calls_per_request',population='normal',aggregation='mean',target=2,comparison='=='),p)['result'] == 'MISS'


def test_unknown_recovery_state_prevents_normal_subset_pass():
    p = population(request(0,recovery=0,logical_calls_per_request=2),request(1,recovery=None,logical_calls_per_request=3))
    assert evaluate_spec(spec(metric='logical_calls_per_request',population='normal',aggregation='mean',target=2),p)['result'] == 'INSUFFICIENT_DATA'


@pytest.mark.parametrize('objective,counts', [(.99,[10,100,1000,10000]),(.995,[5,50,500,5000]),(.999,[1,10,100,1000])])
def test_error_budget_exact_math(objective,counts):
    for n,expected in zip((1000,10000,100000,1000000),counts):
        result=error_budget(objective,n)
        assert result['allowed_whole_failures'] == expected and result['allowed_failures_exact'] == expected
        assert result['remaining_whole_failures'] == expected


def test_budget_fraction_rounding_burn_and_exhaustion():
    assert error_budget(.999,25)['allowed_whole_failures'] == 0
    assert error_budget(.99,1000,20)['burn_rate'] == 2
    assert error_budget(.99,1000,20)['remaining_whole_failures'] == -10
    assert error_budget(.99,1000,20)['exhausted']
    assert error_budget(1,1000,1)['burn_rate'] is None
    assert error_budget(.99,0)['observed_failure_rate'] is None


@pytest.mark.parametrize('goal,n,failures', [(1.1,1,0),(0,1,0),(.99,-1,0),(.99,10,11),(.99,True,0),(float('nan'),1,0)])
def test_invalid_budget_inputs(goal,n,failures):
    with pytest.raises(ValueError): error_budget(goal,n,failures)


def test_report_only_miss_no_ci_failure_or_alert_action():
    s = spec(alert_bands={'warning':.5})
    result=evaluate_scorecard([s],[population(request(request_success=0))])
    assert result['counts']['MISS'] == 1 and result['ci_exit_code'] == 0 and not result['enforced']
    assert result['checks'][0]['alert_action'] == 'NONE_REPORT_ONLY'
    with pytest.raises(ValueError): evaluate_spec(replace(s,enforcement='ENFORCED'),population(request()))


def stage_document():
    row=dict(request_id='a',worker_id=0,outcome='success',business_pass=True,usage_completeness='COMPLETE',
        observed_usage={'total_tokens':852},logical_model_calls=2,recovery_count=0,queue_wait_ms=50,service_ms=800,end_to_end_ms=850,
        tool_correct=True,arguments_correct=True,functional_correct=True,grounding_correct=True,isolation_violations=[],
        extra_retry_attempts=0,deadline=dict(result_abandoned=False),components={'primary_router':{'duration_ms':600},'synthesis':{'duration_ms':200}})
    return dict(stages={'1':dict(status='COMPLETE',stop_reasons=[],admissions=[dict(request_id='a',accepted=True),
        dict(request_id='b',accepted=False,reason='capacity_exhausted')],requests=[row],runtime=dict(peak_active_requests=1,queue_capacity=0),
        collector={'admission':{'max_observed_queue_depth':0},'isolation':{'request_id_collision':0}},
        throughput={'observation_seconds':1})})


def test_concurrency_adapter_keeps_unknown_safety_distinct_and_velocity():
    p=from_concurrency(stage_document())[0]
    assert denominators(p)['expected_capacity_rejections'] == 1
    assert measure(spec(metric='tokens_per_second'),p).value == 852
    assert evaluate_spec(spec(metric='data_protection'),p)['result'] == 'INSUFFICIENT_DATA'
    assert evaluate_spec(spec(metric='queue_saturation_fraction'),p)['result'] == 'NOT_APPLICABLE'
    assert evaluate_spec(spec(metric='worker_saturation_fraction'),p)['result'] == 'INSUFFICIENT_DATA'


def test_adapter_rejects_duplicate_or_unassociated_requests():
    doc=stage_document();doc['stages']['1']['admissions'].append(dict(request_id='a',accepted=True))
    with pytest.raises(ValueError): from_concurrency(doc)
    doc=stage_document();doc['stages']['1']['admissions']=[]
    with pytest.raises(ValueError): from_concurrency(doc)


def test_stopped_campaign_is_not_a_complete_statistical_window():
    doc=stage_document();doc['stages']['1']['stop_reasons']=['unknown_failure']
    assert evaluate_spec(spec(),from_concurrency(doc)[0])['reason'] == 'WINDOW_MISMATCH'


def test_cost_adapter_complete_usage_and_no_judge_or_usd_dependency():
    doc=dict(pricing_mode='TOKEN_ONLY',evaluation_usage_separate={'total_tokens':99999},production_observations=[
        dict(identity='a',population='current_saturation/stage_1',outcome='success',business_pass=True,admitted=True,
             tokens={'total_tokens':852},usage_completeness='COMPLETE',logical_calls=2,recovery_count=0)])
    p=from_cost(doc)[0]
    assert measure(spec(metric='tokens_per_request',aggregation='mean'),p).value == 852
    doc['production_observations'][0]['usage_completeness']='PARTIAL'
    assert evaluate_spec(spec(metric='tokens_per_request',aggregation='mean'),from_cost(doc)[0])['result']=='INSUFFICIENT_DATA'


def test_telemetry_never_relabels_service_latency_as_ingress_latency():
    data=dict(request_id='a',terminal_status='success',total_latency_ms=100,observed_usage={'total_tokens':900},
        usage_completeness='COMPLETE',planning_summary={'recovery_count':0},evaluation_usage={'total_tokens':99999},
        component_spans=[dict(component='primary_router',logical_model_calls=1,duration_ms=60),dict(component='synthesis',logical_model_calls=1,duration_ms=40)])
    p=from_telemetry(data)
    assert measure(spec(metric='tokens_per_request',aggregation='mean'),p).value==900
    assert evaluate_spec(spec(metric='end_to_end_ms',aggregation='p95'),p)['result']=='INSUFFICIENT_DATA'
    assert evaluate_spec(spec(),p)['result']=='INSUFFICIENT_DATA'  # runtime success is not contract success
    data['terminal_status']='running'
    assert denominators(from_telemetry(data))['unexpected_failure']==0


def test_diagnostic_recovery_success_uses_business_contract():
    d=dict(diagnostic={'functional_pass':False,'tool_pass':False,'argument_pass':False,'grounding_pass':False},
        requests=[dict(request_id='a',outcome='success',recovery_count=1,logical_model_calls=3,tokens=1404,usage_completeness='COMPLETE')])
    assert measure(spec(metric='recovery_success',population='recovery'),from_diagnostic(d)).value==0


def test_cli_report_miss_returns_zero_and_does_not_modify_existing_configuration(tmp_path):
    gate=ROOT/'config/quality-gates.yaml'
    before=gate.read_bytes()
    specification=dict(version=1,mode='REPORT_ONLY',context={},candidate_reliability_objectives=[.99],
                       metrics=[spec(target=1.0).__dict__])
    sp=tmp_path/'spec.yaml';sp.write_text(yaml.safe_dump(specification))
    concurrency=stage_document();concurrency['stages']['1']['requests'][0]['business_pass']=False
    cp=tmp_path/'concurrency.json';cp.write_text(json.dumps(concurrency))
    out=tmp_path/'out.json';missing=tmp_path/'absent.json'
    code=main(['--spec',str(sp),'--concurrency',str(cp),'--cost',str(missing),'--recovery',str(missing),'--performance',str(missing),'--output',str(out)])
    report=json.loads(out.read_text())
    assert code==0 and report['counts']['MISS']==1
    assert gate.read_bytes()==before
    with pytest.raises(FileExistsError):
        main(['--spec',str(sp),'--concurrency',str(cp),'--cost',str(missing),'--recovery',str(missing),'--performance',str(missing),'--output',str(out)])
