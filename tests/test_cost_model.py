"""Synthetic prices below are arithmetic fixtures, never provider pricing."""
from dataclasses import replace
import json

import pytest

from src.agentguard.cost_model import (ComponentUsage, Pricing, ProductionObservation, Tokens,
    aggregate, avoided_work, component_summary, cost, mean_tokens, projection, statistics, velocity)
from src.agentguard.cost_evidence import EvidenceLedger, from_telemetry
from scripts.analyze_cost_baseline import build_baseline, main


def observation(recovery=False, **overrides):
    components = [ComponentUsage('router', 1, Tokens(60, 10, 70), 'test-model'),
                  ComponentUsage('synthesis', 1, Tokens(20, 10, 30), 'test-model')]
    if recovery:
        components.insert(1, ComponentUsage('recovery', 1, Tokens(40, 10, 50), 'test-model'))
    args = dict(identity='recovery' if recovery else 'normal', source='synthetic', population='test', outcome='success',
        usage_completeness='COMPLETE', tokens=Tokens(120, 30, 150) if recovery else Tokens(80, 20, 100),
        logical_calls=3 if recovery else 2, provider_dispatches=3 if recovery else 2, components=tuple(components),
        model='test-model', recovery_count=int(recovery))
    return ProductionObservation(**(args | overrides))


def pricing(**overrides):
    return Pricing(**(dict(model='test-model', effective_date='2026-01-01', source='Synthetic unit fixture only',
        verified=True, input_usd_per_million=2, output_usd_per_million=8) | overrides))


def zero(**overrides):
    return observation(tokens=Tokens(0, 0, 0, 0), components=(), logical_calls=0, provider_dispatches=0,
                       zero_work_proven=True, **overrides)


@pytest.mark.parametrize('row,archetype', [
    (observation(), 'NORMAL_2_CALL'), (observation(True), 'RECOVERY_3_CALL'),
    (zero(admitted=False), 'ADMISSION_REJECTED'),
    (zero(expired_before_service=True, outcome='deadline_rejection'), 'DEADLINE_BEFORE_SERVICE'),
    (observation(deadline_after_dispatch=True, outcome='deadline_rejection', usage_completeness='PARTIAL'), 'DEADLINE_AFTER_DISPATCH'),
    (observation(outcome='provider_failure', components=(ComponentUsage('router', 1, Tokens()),)), 'EARLY_FAILURE'),
    (observation(logical_calls=3), 'OTHER_OBSERVED'),
])
def test_archetypes_require_execution_evidence(row, archetype):
    assert row.archetype == archetype


def test_no_guessed_zero_or_split():
    row = observation(tokens=Tokens(total_tokens=100))
    assert mean_tokens([row]).input_tokens is None
    assert aggregate([row])['complete_tokens']['input_tokens']['count'] == 0
    assert observation(admitted=False).archetype == 'UNCLASSIFIED'
    with pytest.raises(ValueError):
        observation(zero_work_proven=True)


@pytest.mark.parametrize('values', [dict(total_tokens=-1), dict(total_tokens=True), dict(total_tokens=float('nan')),
                                   dict(input_tokens=1, output_tokens=2, total_tokens=4), dict(input_tokens=1, cached_input_tokens=2)])
def test_invalid_usage_rejected(values):
    with pytest.raises(ValueError):
        Tokens(**values)


def test_partial_usage_separate_and_unavailable_not_zero():
    result = aggregate([observation(), observation(tokens=Tokens(total_tokens=50), usage_completeness='PARTIAL'),
                        observation(tokens=Tokens(), usage_completeness='UNAVAILABLE')])
    assert result['complete_tokens']['total_tokens']['mean'] == 100
    assert result['partial_known_tokens']['total_tokens']['sum'] == 50
    assert result['usage_completeness'] == {'COMPLETE': 1, 'PARTIAL': 1, 'UNAVAILABLE': 1}
    with pytest.raises(ValueError):
        mean_tokens([observation(usage_completeness='PARTIAL')])


def test_percentiles():
    result = statistics([None, 10, 20, 30, 40])
    assert result == dict(count=4, sum=100, mean=25, p50=25, p90=37, p95=38.5, max=40)
    assert statistics([])['mean'] is None


@pytest.mark.parametrize('rate,expected', [(0,100),(.01,100.5),(.05,102.5),(.1,105),(.25,112.5)])
def test_recovery_mixture(rate, expected):
    result = projection([observation()], [observation(True)], rate)
    assert result['tokens']['total_tokens'] == pytest.approx(expected)
    assert result['logical_model_calls'] == 2+rate
    assert result['cost']['usd'] is None


@pytest.mark.parametrize('n', [1000,10000,100000,1000000])
def test_scale_calls_tokens_and_configured_cost(n):
    result = projection([observation()], [observation(True)], .1, n, pricing())
    assert result['tokens']['total_tokens'] == pytest.approx(n*105)
    assert result['tokens']['input_tokens'] == pytest.approx(n*84)
    assert result['tokens']['output_tokens'] == pytest.approx(n*21)
    assert result['logical_model_calls'] == pytest.approx(n*2.1)
    assert result['recovery_calls'] == pytest.approx(n*.1)
    assert result['cost']['usd'] == pytest.approx(n*(84*2+21*8)/1e6)


def test_recovery_missing_split_does_not_infect_zero_rate_or_invent_positive_rate_split():
    r = observation(True, tokens=Tokens(total_tokens=150))
    assert projection([observation()], [r], 0)['tokens']['input_tokens'] == 80
    assert projection([observation()], [r], .1)['tokens']['input_tokens'] is None
    assert projection([observation()], [r], .1, pricing=pricing())['cost']['usd'] is None


@pytest.mark.parametrize('rate', [-1, 1.1, float('nan'), True])
def test_invalid_rates(rate):
    with pytest.raises(ValueError):
        projection([observation()], [observation(True)], rate)


def test_velocity_and_backpressure():
    assert velocity(10,20,8504,8)['tokens_per_second'] == 1063
    rows = [zero(admitted=False, identity=str(i)) for i in range(5)]
    result = avoided_work(rows, [observation()])
    assert result['observed_rejected_tokens'] == result['observed_rejected_tool_calls'] == 0
    assert result['estimated_tokens'] == 500 and result['estimated_model_calls'] == 10
    assert 'NOT_ACTUAL_SAVINGS' in result['kind']
    with pytest.raises(ValueError):
        velocity(1,2,100,0)


def test_pricing_availability_and_model_mismatch():
    t = Tokens(1000000, 1000000, 2000000)
    assert cost(t, 'test-model')['mode'] == 'TOKEN_ONLY'
    assert cost(t, 'test-model')['usd'] is None
    assert cost(t, 'test-model', pricing(verified=False))['usd'] is None
    assert cost(t, 'test-model', pricing(output_usd_per_million=None))['mode'] == 'TOKEN_ONLY'
    assert cost(t, 'another-model', pricing())['reason'] == 'MODEL_MISMATCH_OR_UNKNOWN'
    assert cost(t, 'test-model', pricing())['usd'] == 10
    assert cost(t, 'test-model', pricing(), complete=False)['usd'] is None
    assert cost(Tokens(total_tokens=100), 'test-model', pricing())['usd'] is None


def test_cached_pricing_needs_actual_cache_counter():
    p = pricing(cached_input_usd_per_million=.5)
    assert cost(Tokens(100,10,110), 'test-model', p)['reason'] == 'CACHED_INPUT_UNAVAILABLE'
    assert cost(Tokens(100,10,110,40), 'test-model', p)['usd'] == pytest.approx((60*2+40*.5+10*8)/1e6)


@pytest.mark.parametrize('changes', [dict(effective_date='unknown'), dict(source=''), dict(input_usd_per_million=-1),
                                    dict(verified='true'), dict(output_usd_per_million=float('inf'))])
def test_pricing_metadata_validation(changes):
    with pytest.raises(ValueError):
        pricing(**changes)


def test_component_accounting_not_added_to_request_total():
    rows = [observation(), observation(True)]
    assert aggregate(rows)['complete_tokens']['total_tokens']['sum'] == 250
    parts = component_summary(rows)
    assert parts['router']['tokens_per_component_per_request']['total_tokens']['sum'] == 140
    assert parts['recovery']['calls'] == 1
    assert cost(rows[1].components[1].tokens, 'test-model', pricing())['usd'] == pytest.approx(.00016)


def telemetry():
    return dict(request_id='real-id', terminal_status='success', usage_completeness='COMPLETE',
        observed_usage=dict(input_tokens=80,output_tokens=20,total_tokens=100), planning_summary=dict(recovery_count=0),
        component_spans=[dict(component='primary_router',logical_model_calls=1,input_tokens=60,output_tokens=10,total_tokens=70),
                         dict(component='synthesis',logical_model_calls=1,input_tokens=20,output_tokens=10,total_tokens=30)],
        evaluation_usage=dict(total_tokens=99999))


def test_production_telemetry_ignores_judge_tokens():
    result = from_telemetry(telemetry(), provider_dispatches=2)
    assert result.tokens.total_tokens == 100 and result.archetype == 'NORMAL_2_CALL'


def audit_document():
    return dict(complete=True, started_at='unique-campaign', scenarios=[dict(scenario_id='case', usage_reconciled=True,
        production_usage=dict(input_tokens=80, output_tokens=20, total_tokens=100),
        components=[dict(component='router', model_responses=1,input_tokens=60,output_tokens=10,total_tokens=70),
                    dict(component='agent', model_responses=1,input_tokens=20,output_tokens=10,total_tokens=30)],
        evaluation_usage=dict(total_tokens=999))])


def test_report_adapter_separates_judges_and_never_uses_summary():
    doc = audit_document()
    doc['summary'] = {'total_tokens': 10000000}
    ledger = EvidenceLedger(); ledger.ingest(doc, 'audit')
    row = next(iter(ledger.rows.values()))
    assert row.tokens.total_tokens == 100 and row.provider_dispatches is None
    assert ledger.evaluation_usage[0]['usage']['total_tokens'] == 999


def test_no_double_counting_copies_and_conflicts():
    ledger = EvidenceLedger()
    doc = audit_document()
    ledger.ingest(doc, 'original'); ledger.ingest(doc, 'copy')
    assert len(ledger.rows) == 1
    assert ledger.excluded[-1]['reason'] == 'DUPLICATE_DOCUMENT'
    row = next(iter(ledger.rows.values()))
    ledger.add(replace(row, source='repacked'))
    assert len(ledger.rows) == 1
    ledger.add(replace(row, tokens=Tokens(total_tokens=200)))
    assert not ledger.rows and ledger.excluded[-1]['reason'] == 'CONFLICTING_REQUEST_EVIDENCE'


def test_incomplete_untrusted_and_unreconciled_reports_excluded(tmp_path):
    ledger = EvidenceLedger()
    doc = audit_document(); doc['complete'] = False
    ledger.ingest(doc, 'partial')
    doc = audit_document(); doc['scenarios'][0]['usage_reconciled'] = False
    ledger.ingest(doc, 'untrusted')
    ledger.ingest({'scope':'loopback','tokens':100}, 'loopback')
    ledger.read(tmp_path/'missing.json')
    assert not ledger.rows and len(ledger.excluded) == 4


def test_missing_evidence_produces_explicit_report_not_live_work(tmp_path):
    result = build_baseline(tmp_path)
    assert result['pricing_mode'] == 'TOKEN_ONLY'
    assert result['counts']['total_records'] == 0
    assert result['projection_unavailable_reason']
    out = tmp_path/'result.json'
    main(['--reports-root',str(tmp_path),'--output',str(out)])
    assert json.loads(out.read_text())['scope'] == 'OFFLINE_REPORT_ONLY'
    with pytest.raises(FileExistsError):
        main(['--reports-root',str(tmp_path),'--output',str(out)])


def staged_document():
    row = dict(request_id='admitted', outcome='success', usage_completeness='COMPLETE',
        observed_usage=dict(input_tokens=80,output_tokens=20,total_tokens=100), logical_model_calls=2, http_dispatches=2,
        components={'primary_router':dict(calls=1,known_tokens=70,resolved_models=['test-model']),
                    'synthesis':dict(calls=1,known_tokens=30,resolved_models=['test-model'])},
        dispatch_evidence=[dict(request_id='admitted')], business_pass=True, recovery_count=0)
    return dict(campaign='13D.5', stages={'3':dict(status='COMPLETE', admitted=1, requests=[row], stop_reasons=[],
        admissions=[dict(request_id='admitted',accepted=True),
                    dict(request_id='rejected',accepted=False,reason='capacity_exhausted')])})


def test_concurrency_parent_summary_and_collector_not_counted_twice():
    doc = staged_document()
    doc['stages']['3']['collector'] = {'requests': doc['stages']['3']['requests']}
    doc['campaign_totals'] = {'total_tokens': 100}
    ledger = EvidenceLedger(); ledger.ingest(doc, 'live')
    assert len(ledger.rows) == 2
    assert sum(r.tokens.total_tokens for r in ledger.rows.values()) == 100
    assert ledger.rows['rejected'].archetype == 'ADMISSION_REJECTED'
    assert ledger.rows['admitted'].components[0].tokens.input_tokens is None


def test_rejected_but_dispatched_evidence_is_not_zero_work():
    doc = staged_document()
    doc['stages']['3']['requests'][0]['dispatch_evidence'].append(dict(request_id='rejected'))
    ledger = EvidenceLedger(); ledger.ingest(doc, 'bad')
    assert 'rejected' not in ledger.rows
    assert ledger.excluded[-1]['reason'] == 'REJECTION_HAS_EXECUTION_EVIDENCE'


def test_partial_concurrent_usage_not_promoted_by_known_tokens():
    doc = staged_document()
    row = doc['stages']['3']['requests'][0]
    row['usage_completeness'] = 'PARTIAL'
    ledger = EvidenceLedger(); ledger.ingest(doc, 'partial')
    assert aggregate([ledger.rows['admitted']])['complete_tokens']['total_tokens']['mean'] is None


def test_expired_queued_request_needs_no_dispatch_and_no_tool_evidence():
    doc = staged_document()
    row = doc['stages']['3']['requests'][0]
    row.update(outcome='deadline_rejection', logical_model_calls=0,http_dispatches=0,components={},
               dispatch_evidence=[], operations=[],
               expired_before_service=True,expired_without_dispatch_or_tool=True, observed_usage=None,
               usage_completeness='UNAVAILABLE')
    ledger = EvidenceLedger(); ledger.ingest(doc, 'expired')
    result = ledger.rows['admitted']
    assert result.archetype == 'DEADLINE_BEFORE_SERVICE' and result.tokens.total_tokens == 0


def test_recovery_collector_total_only_and_unknown_model_preserved():
    doc = dict(diagnostic=dict(functional_pass=True,tool_pass=True,argument_pass=True,grounding_pass=True),
        admission=dict(admitted=1), requests=[dict(request_id='recovery-id',outcome='success', tokens=1644,
        usage_completeness='COMPLETE',logical_model_calls=3,provider_dispatches=3,recovery_count=1,
        router_ms=20,recovery_ms=10,synthesis_ms=5)])
    ledger = EvidenceLedger(); ledger.ingest(doc, 'diagnostic')
    row = ledger.rows['recovery-id']
    assert row.archetype == 'RECOVERY_3_CALL'
    assert row.tokens.input_tokens is None and row.model is None
    assert all(c.tokens.total_tokens is None for c in row.components)


@pytest.mark.parametrize('source,accepted', [('production',True),('controlled',False)])
def test_reliability_selected_recovery_probe_is_not_forced_or_retried(source, accepted):
    row = dict(source=source,recovery_probe=True,scenario_id='selected',repetition=1,status='completed',
        dataset='safety',required_operation_count=1,usage_completeness='COMPLETE',models=['test-model'],
        tokens=dict(production_total=dict(total_tokens=100),primary_router=dict(total_tokens=70),synthesis=dict(total_tokens=30)),
        attempts=[dict(component=c,attempt_number=1,transport_attempts_observed=None) for c in ('primary_router','synthesis')])
    doc = dict(complete=True,suite='reliability',started_at='campaign',observations=[row],evaluator_usage=dict(tokens=1000))
    ledger = EvidenceLedger(); ledger.ingest(doc,'reliability')
    assert bool(ledger.rows) == accepted
    if accepted:
        result = next(iter(ledger.rows.values()))
        assert result.archetype == 'NORMAL_2_CALL' and result.recovery_count == 0
        assert 'selected_recovery_probe' in result.population and result.provider_dispatches is None
        assert result.tokens.total_tokens == 100
