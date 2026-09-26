"""Offline 13E.2 design scorecard. MISS never becomes a CI/release failure."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.slo import (SLOSpec, PopulationEvidence, Measurement, error_budget, evaluate_scorecard)
from src.agentguard.slo_evidence import from_concurrency, from_cost, from_diagnostic, from_telemetry


def load_spec(path):
    document = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if document.get('version') != 1 or document.get('mode') != 'REPORT_ONLY':
        raise ValueError('13E.2 requires version 1 and REPORT_ONLY mode')
    specs = [SLOSpec(**(document.get('defaults', {}) | row)) for row in document['metrics']]
    if len({s.metric for s in specs}) != len(specs):
        raise ValueError('Duplicate metric specification')
    if any(s.enforcement != 'REPORT_ONLY' for s in specs):
        raise ValueError('Enforced objectives are not supported in 13E.2')
    return document, specs


def build_report(spec_document, specs, concurrency, cost_report, recovery, performance=None):
    populations = from_concurrency(concurrency)
    cost_populations = from_cost(cost_report)
    normal = [r for p in cost_populations if p.name.startswith('cost/current_saturation/') for r in p.requests if r.admitted]
    populations.append(PopulationEvidence('current_normal_consumption', requests=normal, admission_census_complete=False,
        notes=['Only token/call data pooled across the comparable 25-request normal population; no load latency pooling.']))
    recovered = from_diagnostic(recovery)
    contrast = cost_report.get('recovery_comparison', {})
    if contrast and recovered.requests:
        recovered.observations['recovery_incremental_tokens'] = Measurement(
            contrast.get('incremental_tokens'), contrast.get('recovery_samples', 0), contrast.get('recovery_samples', 0), 0, True)
        recovered.notes.append('Token difference is the 13E.1 cross-scenario contrast, not causal recovery overhead.')
    populations.append(recovered)
    scorecard = evaluate_scorecard(specs, populations)
    # None of these comparisons activate or alter an existing release requirement.
    scorecard.update(schema_version=1, milestone='13E.2', specification=[asdict(s) for s in specs],
        design_context=spec_document['context'],
        error_budget_examples=[error_budget(goal, n) for goal in spec_document['candidate_reliability_objectives']
                               for n in (1000,10000,100000,1000000)],
        sequential_performance_reference=(performance or {}).get('qualification'),
        current_request_deadline_ms=20000,
        cost_mode=cost_report.get('pricing_mode', 'UNAVAILABLE'),
        historical_evidence=cost_report.get('counts', {}),
        historical_consumption_populations={k: v.get('sample_count') for k, v in cost_report.get('populations', {}).items()},
        population_overlap='Cost and concurrency panels share request identities and are independent views; never sum panel denominators into unique traffic.',
        evidence_limitations=[
            '25 admitted functional burst requests cannot establish a rolling production availability or latency SLO.',
            'One recent successful recovery is not representative; older recovery contracts remain distinct.',
            'No time series for queue/worker saturation duration; peak values do not fill this gap.',
            'Safety classifier absence is unknown, not PASS. Explicit policy skips alone are NOT_APPLICABLE.',
            'No cancellation-origin attribution: admitted cancellations conservatively consume the success budget.',
            'Known violations can MISS even with incomplete coverage; zero observations cannot prove absence without coverage.',
            'Minimum sample screens are design assumptions, not statistical qualification. Future rolling windows need cohort maturity and timestamps.',
        ])
    return scorecard


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, default=ROOT/'config/slo-report-only.yaml')
    parser.add_argument('--concurrency', type=Path, default=ROOT/'reports/concurrency_13d5_live/qualification.json')
    parser.add_argument('--cost', type=Path, default=ROOT/'reports/cost_13e1/baseline.json')
    parser.add_argument('--recovery', type=Path, default=ROOT/'reports/concurrency_13d4g/order_status_004.json')
    parser.add_argument('--performance', type=Path, default=ROOT/'reports/performance_qualification.json')
    parser.add_argument('--telemetry', type=Path, help='Optional single production telemetry JSON; never executes a request')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/slo_13e2/scorecard.json')
    args = parser.parse_args(argv)
    doc, specs = load_spec(args.spec)
    missing = []
    def read(path):
        if not path.exists():
            missing.append(str(path))
            return {}
        return json.loads(path.read_text(encoding='utf-8-sig'))
    report = build_report(doc, specs, read(args.concurrency), read(args.cost), read(args.recovery), read(args.performance))
    if args.telemetry:
        report['additional_telemetry_scorecard'] = evaluate_scorecard(specs, [from_telemetry(read(args.telemetry))])
    report['missing_sources'] = missing
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(dict(output=str(args.output), mode='REPORT_ONLY', counts=report['counts'], ci_exit_code=0)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
