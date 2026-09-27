"""Read saved evidence and emit a shadow scorecard; never run agents or judges."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.agentguard.quality_gate import load_quality_gate_config
from src.agentguard.release_readiness import load_candidate_spec, evaluate_release_readiness


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, default=ROOT/'config/release-gates-candidate.yaml')
    parser.add_argument('--slo', type=Path, default=ROOT/'reports/slo_13e2/scorecard_final.json')
    parser.add_argument('--concurrency', type=Path, default=ROOT/'reports/concurrency_13d5_live/qualification.json')
    parser.add_argument('--policy', type=Path, default=ROOT/'config/runtime-reliability.json')
    parser.add_argument('--performance', type=Path, default=ROOT/'reports/performance_qualification.json')
    parser.add_argument('--quality-result', type=Path, help='Saved QualityGateResult JSON; omitted evidence stays unavailable')
    parser.add_argument('--deployment', type=Path, help='Explicit {active, max_workers, queue_capacity}; omission is unknown, not inactive')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/release_13e3/scorecard.json')
    args = parser.parse_args(argv)
    sources = []
    def read(path):
        if path is None:
            return None
        if not path.exists():
            sources.append(dict(path=str(path), available=False))
            return {}
        raw = path.read_bytes()
        data = json.loads(raw.decode('utf-8-sig'))
        sources.append(dict(path=str(path), available=True, sha256=hashlib.sha256(raw).hexdigest()))
        return data
    spec = load_candidate_spec(args.spec)
    gates_path = ROOT/spec['existing_gates']
    config = load_quality_gate_config(gates_path)
    slo, concurrency, policy = read(args.slo), read(args.concurrency), read(args.policy)
    performance, quality = read(args.performance), read(args.quality_result)
    snapshots = {str(args.policy): policy, str(args.concurrency)+'#reliability_policy': concurrency.get('reliability_policy', {})}
    profiles = {}
    for key, stage in concurrency.get('stages', {}).items():
        runtime = stage.get('runtime', {})
        profiles[str(args.concurrency)+f'#stage_{key}'] = dict(active=True,
            max_workers=runtime.get('configured_workers'), queue_capacity=runtime.get('queue_capacity'))
    # Historical activation does not silently stand in for deployment activation.
    profiles['deployment configuration'] = read(args.deployment) or {}
    report = evaluate_release_readiness(spec, slo, config, quality_result=quality,
        policy_snapshots=snapshots, concurrency_profiles=profiles, performance=performance)
    for path in (args.spec, gates_path):
        sources.append(dict(path=str(path), available=True, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    report['sources'] = sources
    report['qualified_source_revision'] = concurrency.get('git_commit')
    report['quality_result_source'] = str(args.quality_result) if args.quality_result else None
    report['scope'] = 'Historical qualified observations plus current declared config; deployment/full release evidence may be absent.'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(dict(output=str(args.output), advisory_decision=report['advisory_decision'],
                          shadow_counts=report['shadow_counts'], ci_exit_code=0)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
