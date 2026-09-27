"""Generate a report-only baseline from saved evidence. No runtime/provider imports."""
import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.cost_evidence import EvidenceLedger
from src.agentguard.cost_model import (Pricing, Tokens, aggregate, avoided_work, component_summary,
    cost, mean_tokens, projection, velocity)

SOURCES = {
    'token_audit_smoke.json': 'historical_smoke_pre_optimization',
    'token_audit_smoke_optimized.json': 'historical_smoke_optimized_model_tool_execution',
    'token_audit_smoke_sandbox_attempt.json': 'incomplete_smoke_attempt',
    'latency_audit_smoke.json': 'historical_smoke_latency',
    'performance_qualification.json': 'historical_performance',
    'reliability_baseline.json': 'historical_reliability_baseline',
    'reliability_13c4b_pass1.json': 'historical_reliability_pass1',
    'reliability_13c4b_pass2.json': 'historical_reliability_pass2',
    'reliability_13c4d_tail_probe.json': 'selected_tail_probe',
    'reliability_13c4f_candidate_R.json': 'historical_candidate_R',
    'reliability_13c4g_candidate_R_tail.json': 'selected_candidate_R_tail',
    'concurrency_13d4_live/qualification.json': 'historical_concurrency_13d4',
    'concurrency_13d4b/order_status_004.json': 'historical_primary_diagnostic',
    'concurrency_13d4d/order_status_004.json': 'historical_uncertain_recovery',
    'concurrency_13d4g/order_status_004.json': 'current_recovery_validation',
    'concurrency_13d5_live/qualification.json': 'current_saturation',
}
ARCHETYPES = ('NORMAL_2_CALL', 'RECOVERY_3_CALL', 'EARLY_FAILURE', 'ADMISSION_REJECTED',
              'DEADLINE_BEFORE_SERVICE', 'DEADLINE_AFTER_DISPATCH', 'OTHER_OBSERVED', 'UNCLASSIFIED')


def summarize(rows, pricing):
    summary = aggregate(rows)
    summary['archetypes'] = {name: aggregate([r for r in rows if r.archetype == name]) for name in ARCHETYPES}
    summary['components'] = component_summary(rows)
    totals = {name: s['tokens_per_component_per_request']['total_tokens']['sum']
              for name, s in summary['components'].items()}
    observed = {k: v for k, v in totals.items() if v is not None}
    counts = {name: s['calls'] for name, s in summary['components'].items()}
    summary['component_dominance'] = dict(
        tokens=max(observed, key=observed.get) if observed else None,
        logical_calls=[name for name, count in counts.items() if count and count == max(counts.values())],
        observed_token_shares={k: v/sum(observed.values()) for k, v in observed.items()} if sum(observed.values()) else {},
        scope='Only available component token observations; unavailable components are not zero')
    summary['usd_per_request'] = aggregate_costs(rows, pricing)
    summary['component_usd'] = {name: [cost(c.tokens, c.model, pricing, complete=r.usage_completeness == 'COMPLETE')
        for r in rows for c in r.components if c.name == name and c.calls] for name in ('router', 'recovery', 'synthesis')}
    return summary


def aggregate_costs(rows, pricing):
    results = [cost(r.tokens, r.model, pricing, complete=r.usage_completeness == 'COMPLETE') for r in rows]
    available = [r['usd'] for r in results if r['usd'] is not None]
    return dict(available_count=len(available), unavailable_count=len(results)-len(available),
        mean_usd=sum(available)/len(available) if available else None,
        unavailable_reasons=dict(Counter(r['reason'] for r in results if r['reason'])))


def build_baseline(reports_root, pricing=None):
    root = Path(reports_root)
    ledger = EvidenceLedger()
    for name, population in SOURCES.items():
        ledger.read(root / name, population=population)
    # Inventory makes exclusion of derived replays, summaries, loopback/fault
    # fixtures, partial probes and report copies explicit, rather than ingesting
    # plausible-looking token counters recursively.
    ignored = [dict(source=p.relative_to(root).as_posix(), reason='OUTSIDE_CURATED_ORIGINAL_EVIDENCE_MANIFEST')
               for p in sorted(root.rglob('*.json'))
               if p.relative_to(root).as_posix() not in SOURCES and 'cost_13e1' not in p.parts]
    rows = list(ledger.rows.values())
    normal = [r for r in rows if r.population.startswith('current_saturation/') and
              r.archetype == 'NORMAL_2_CALL' and r.business_pass is True and r.usage_completeness == 'COMPLETE']
    recovery = [r for r in rows if r.population == 'current_recovery_validation' and
                r.archetype == 'RECOVERY_3_CALL' and r.business_pass is True and r.usage_completeness == 'COMPLETE']
    report = dict(schema_version=1, scope='OFFLINE_REPORT_ONLY', pricing_mode=pricing.mode if pricing else 'TOKEN_ONLY',
        pricing=asdict(pricing) if pricing else None, evidence_sources=ledger.sources,
        excluded=ledger.excluded, ignored_artifacts=ignored,
        production_observations=[dict(**asdict(r), archetype=r.archetype) for r in rows],
        evaluation_usage_separate=ledger.evaluation_usage,
        populations={p: summarize([r for r in rows if r.population == p], pricing) for p in sorted({r.population for r in rows})},
        counts=dict(production_request_observations=sum(r.admitted for r in rows),
                    zero_work_rejections=sum(not r.admitted for r in rows), total_records=len(rows),
                    archetypes=dict(Counter(r.archetype for r in rows)), usage_completeness=dict(Counter(r.usage_completeness for r in rows))),
        assumptions=[
            'Only allowlisted original report shapes are ingested. Identity/document deduplication prevents copies and nested summaries being counted again.',
            'Historical architecture epochs and selected tail populations remain separate. They are not pooled into current baseline percentiles.',
            'Logical calls are not HTTP dispatches. SDK response counts do not establish transport dispatch counts.',
            'Partial known tokens are lower bounds, excluded from complete-usage distributions and projections.',
            'USD unavailable without user-verified pricing, model match and measured input/output split; never priced from total tokens alone.',
            'Current recovery evidence is N=1, a different scenario from the normal sample: difference is descriptive, not a causal intervention estimate.',
            'Finite-burst rates are not sustained capacity or forecasts. No limits, CI failures, SLOs or gates are introduced.',
            'Collector-only recovery evidence retains total tokens and stage presence but lacks token splits and model identity.',
        ], current_normal=summarize(normal, pricing), current_recovery=summarize(recovery, pricing))
    report['budget_model'] = dict(mode='REPORT_ONLY', enforcement=False, thresholds=None,
        concepts=['tokens/request', 'logical calls/request', 'recovery rate', 'usage completeness', 'tokens/sec'])
    report['recovery_rate_projections'] = []
    report['scale_projections'] = []
    if normal and recovery:
        n, r = mean_tokens(normal), mean_tokens(recovery)
        nc = cost(n, normal[0].model, pricing)
        rc = cost(r, recovery[0].model, pricing)
        report['recovery_comparison'] = dict(normal_mean_tokens=n.total_tokens, recovery_observed_mean_tokens=r.total_tokens,
            incremental_tokens=r.total_tokens - n.total_tokens, normal_samples=len(normal), recovery_samples=len(recovery),
            normal_cost=nc, recovery_cost=rc,
            incremental_usd=rc['usd']-nc['usd'] if rc['usd'] is not None and nc['usd'] is not None else None)
        report['recovery_rate_projections'] = [projection(normal, recovery, rate, pricing=pricing) for rate in (0, .01, .05, .1, .25)]
        report['scale_projections'] = [projection(normal, recovery, rate, count, pricing) for count in (1000, 10000, 100000, 1000000) for rate in (0, .05, .1)]
    else:
        report['projection_unavailable_reason'] = 'MISSING_COMPLETE_CURRENT_NORMAL_OR_RECOVERY_EVIDENCE'
    report['concurrency_velocity'] = []
    path = root / 'concurrency_13d5_live/qualification.json'
    if path.exists():
        document = json.loads(path.read_text())
        for key, stage in document.get('stages', {}).items():
            selected = [r for r in rows if r.population == 'current_saturation/stage_' + key and r.admitted]
            seconds = stage.get('throughput', {}).get('observation_seconds')
            if not selected or seconds is None or any(r.usage_completeness != 'COMPLETE' for r in selected):
                continue
            report['concurrency_velocity'].append(dict(stage=key,
                **velocity(len(selected), sum(r.logical_calls for r in selected), sum(r.tokens.total_tokens for r in selected), seconds)))
        selected = [r for r in rows if r.population == 'current_saturation/stage_3']
        if any(not r.admitted for r in selected):
            report['stage3_backpressure'] = avoided_work([r for r in selected if not r.admitted], [r for r in selected if r.admitted])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports-root', type=Path, default=ROOT / 'reports')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/cost_13e1/baseline.json')
    parser.add_argument('--pricing', type=Path, help='Explicitly verified JSON pricing; omitted means TOKEN_ONLY')
    args = parser.parse_args(argv)
    pricing = Pricing(**json.loads(args.pricing.read_text())) if args.pricing else None
    report = build_baseline(args.reports_root, pricing)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never silently overwrite historical evidence.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(dict(output=str(args.output), mode=report['pricing_mode'], counts=report['counts'])))


if __name__ == '__main__':
    main()
