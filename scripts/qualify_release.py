"""Assemble and enforce structural release evidence. This command is offline only."""
import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.agentguard.lineage import lineage_entry, start_run
from src.agentguard.invocation import invocation_entry
from src.agentguard.quality_gate import load_quality_gate_config
from src.agentguard.structural_release import (evidence_identity, load_release_spec, transport_evidence,
    configuration_observations, evaluate_bundle, observation, POLICY_PATHS, _lookup, add_record_violations)


@lineage_entry
def assemble_release(root, *, structural_path, quality_result=None, deployment=None,
                     slo_path=None, cost_path=None, output=None, records=(), evaluation_lineage=None):
    root = Path(root)
    identity = evidence_identity(root)
    lineage = start_run(root, suite="structural", execution_mode="offline_fixture",
        hosting={"mode": "bounded" if (deployment or {}).get("active") else "sequential",
                 "max_workers": (deployment or {}).get("max_workers", "UNKNOWN"),
                 "queue_capacity": (deployment or {}).get("queue_capacity", "UNKNOWN")})
    sources = []
    def read(path):
        if path is None or not Path(path).exists():
            sources.append({'path':str(path) if path else None,'available':False})
            return {}
        data = Path(path).read_bytes()
        sources.append({'path':str(path),'available':True,'sha256':hashlib.sha256(data).hexdigest()})
        return json.loads(data.decode('utf-8-sig'))
    spec_path = root/'config/release-gates.yaml'
    spec = load_release_spec(spec_path)
    config = load_quality_gate_config(root/spec['existing_gates'])
    document = read(structural_path)
    observations, models, reasons = transport_evidence(document,identity,spec['required_workers'])
    declared = read(root/'config/runtime-reliability.json')
    from src.agent.runtime_reliability import default_runtime_policy
    try:
        resolved = default_runtime_policy().snapshot()
    except (ValueError, TypeError, KeyError):
        resolved = {}
        reasons.append('Default runtime policy failed resolution.')
    configuration_observations(observations,declared,resolved)
    supplemental = add_record_violations(observations,records) if records else []
    # Current production records supplement the offline qualification; they do
    # not manufacture unavailable HTTP-dispatch or semantic-grounding evidence.
    for index,record in enumerate(records):
        data = record.production_telemetry or {}
        policy = data.get('effective_runtime_policy') or {}
        for metric,path in POLICY_PATHS.items():
            if not path.startswith('lower_layer'):
                observations.setdefault(metric,[]).append(observation(_lookup(policy,path),f'current smoke request/{index}'))
        models.extend(s['resolved_model'] for s in data.get('component_spans',[]) if s.get('resolved_model'))
    quality = asdict(quality_result) if is_dataclass(quality_result) else quality_result
    # Offline files must be attributed to this exact tree and dependency set.
    if isinstance(quality,Path):
        saved = read(quality)
        quality = saved.get('quality_result') if all(saved.get(k)==identity[k] for k in
            ('git_commit','source_fingerprint','sdk_versions')) else None
        if quality is None:
            reasons.append('Quality artifact missing or stale; not inferred from offline transport fixtures.')
    report = evaluate_bundle(spec,observations,config,quality_result=quality,deployment=deployment,
        slo_report=read(slo_path),cost_report=read(cost_path))
    report.update(identity,model_identity=sorted(set(models)),runtime_policy=resolved,
        structural_source_identity={k:document.get(k) for k in ('git_commit','source_fingerprint','timestamp','sdk_versions')},
        applicability={'concurrency':deployment},evidence_sources=sources,evidence_notes=reasons,
        current_request_supplemental_coverage=supplemental,
        quality_result=quality,
        gate_specification_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest())
    report.update(lineage.reference, evaluation_lineage=evaluation_lineage,
                  source_evaluation_run_ids=sorted({r.run_id for r in records if getattr(r, "run_id", None)}))
    lineage.aggregate = {"decision": report["decision"], "evaluation_lineage": evaluation_lineage,
                         "source_evaluation_run_ids": report["source_evaluation_run_ids"]}
    lineage.evaluation_complete = True
    output = Path(output) if output else root/'reports/release_13e4'/f'qualification_{uuid4().hex}.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,indent=2,allow_nan=False)
    print('RELEASE QUALIFICATION')
    for gate in report['gates']:
        print(f"{gate['classification']} {gate['metric']}: {gate['status']} — {gate['reason']}")
    print(f"RELEASE DECISION: {report['decision']}\nReport: {output}")
    return report


@invocation_entry(suite="structural", mode="offline_fixture")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--structural-evidence',type=Path,default=ROOT/'reports/release_13e4/transport.json')
    parser.add_argument('--quality-result',type=Path,help='Same-revision attributed quality result/bundle')
    parser.add_argument('--deployment',type=Path,help='Explicit activation and worker/queue counts; omission is unknown')
    parser.add_argument('--slo',type=Path,default=ROOT/'reports/slo_13e2/scorecard_final.json')
    parser.add_argument('--cost',type=Path,default=ROOT/'reports/cost_13e1/baseline.json')
    parser.add_argument('--output',type=Path)
    args = parser.parse_args(argv)
    deployment = json.loads(args.deployment.read_text(encoding='utf-8-sig')) if args.deployment else None
    report = assemble_release(ROOT,structural_path=args.structural_evidence,quality_result=args.quality_result,
        deployment=deployment,slo_path=args.slo,cost_path=args.cost,output=args.output)
    return report['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
