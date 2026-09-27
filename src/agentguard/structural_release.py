"""Enforced structural qualification, separate from statistical SLO observations.

Only reads captured evidence. No model execution, judge, retry or runtime mutation.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess

import yaml

from src.agentguard.release_readiness import INVARIANTS, POLICY_PATHS, _lookup, _valid, _matches, existing_gate_rows


def evidence_identity(root):
    root = Path(root)
    commit = subprocess.run(['git', '-c', f'safe.directory={root.as_posix()}', 'rev-parse', 'HEAD'],
                            cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    digest = hashlib.sha256()
    for directory in ('src', 'config', 'scripts', 'tests', '.github'):
        for path in sorted((root/directory).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.json', '.yaml', '.yml'} and '__pycache__' not in path.parts:
                digest.update(path.relative_to(root).as_posix().encode())
                digest.update(path.read_bytes())
    digest.update((root/'requirements.txt').read_bytes())
    dependencies = {name: version(name) for name in ('openai', 'openai-agents')}
    digest.update(json.dumps(dependencies, sort_keys=True).encode())
    return dict(git_commit=commit, source_fingerprint=digest.hexdigest(), sdk_versions=dependencies,
                timestamp=datetime.now(timezone.utc).isoformat())


def load_release_spec(path):
    doc = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    expected = INVARIANTS | POLICY_PATHS.keys() | {'max_workers', 'queue_capacity'}
    gates = doc.get('candidates', [])
    if (doc.get('version') != 1 or doc.get('mode') != 'ENFORCED' or
            len(gates) != 16 or {g['metric'] for g in gates} != expected):
        raise ValueError('Exactly the 16 promoted deterministic gates are required')
    for g in gates:
        source = 'slo' if g['metric'] in INVARIANTS else 'policy' if g['metric'] in POLICY_PATHS else 'concurrency'
        if g.get('source') != source or (source == 'policy' and g.get('path') != POLICY_PATHS[g['metric']]):
            raise ValueError('Gate applicability/source contract cannot be reassigned')
        if g['comparison'] not in {'==', '<='} or not _valid(g['expected'], g['expected']):
            raise ValueError('Invalid structural gate threshold')
        if g['metric'] in INVARIANTS and (g['expected'] != 0 or g['source'] != 'slo'):
            raise ValueError('Structural invariants require zero observed violations')
    return doc


def observation(value, population, denominator=1, *, complete=True, reason='Measured deterministic evidence'):
    return dict(value=value, population=population, denominator=denominator, complete=complete, reason=reason)


def transport_evidence(document, identity, required_workers=(1, 2, 5)):
    """Pinned offline harness adapter; does not claim live semantic correctness.

    Model responses are local deterministic fixtures. The runtime, SDK, policy,
    business tools and HTTP dispatch path are real. Missing coverage is not zero.
    """
    observations = {m: [] for m in INVARIANTS | POLICY_PATHS.keys()}
    models = set()
    trusted = (document.get('kind') == 'offline_production_transport' and
               all(identity.get(k) and document.get(k) == identity[k]
                   for k in ('git_commit', 'source_fingerprint', 'sdk_versions')))
    if not trusted:
        return observations, [], ['Missing or stale qualification identity; regenerate offline evidence.']
    experiments = [e for e in document.get('experiments', []) if e.get('mode') == 'production']
    coverage = Counter(e.get('count') for e in experiments)
    reasons = []
    if set(coverage) != set(required_workers) or any(n != 1 for n in coverage.values()):
        reasons.append('Required 1/2/5-worker experiment coverage incomplete or duplicated.')
    all_ids = []
    for experiment in experiments:
        label = f"offline production transport/{experiment.get('count')} workers"
        rows = experiment.get('rows', [])
        complete = (len(rows) == experiment.get('count', 0)*3 and not experiment.get('errors') and
                    not experiment.get('loop_errors') and experiment.get('workers_alive') == 0 and
                    experiment.get('server_threads_alive') == 0 and not experiment.get('server_errors'))
        if not complete:
            reasons.append('Incomplete production transport experiment.')
        for row in rows:
            data, trace = row.get('telemetry') or {}, row.get('execution') or {}
            all_ids.append(row.get('budget_id'))
            source = label + '/' + str(row.get('tag'))
            http = [h for h in experiment.get('http', []) if h.get('tag') == row.get('tag')]
            spans = data.get('component_spans', [])
            attempts = [a for s in spans for a in s.get('attempts', [])]
            for span in spans:
                if span.get('resolved_model'):
                    models.add(span['resolved_model'])
            try:
                operations = trace['operations']
                grants = trace['plan']['authorized_bindings']
                invoked = [op for op in operations if op['invoked']]
                keys = [(op['operation']['tool'], json.dumps(dict(op['operation']['arguments']), sort_keys=True)) for op in invoked]
                allowed = {(g['tool'], json.dumps({'order_id':g['order_id']}, sort_keys=True)) for g in grants}
                auth = sum(k not in allowed for k in keys) + len(trace['prohibited_operation_attempts'])
                auth += int(row['tools'] != [dict(op['operation']['arguments'])['order_id'] for op in invoked])
                duplicate = len(keys)-len(set(keys))
                mixed = 0
                for op in invoked:
                    result = op['output']['order']
                    mixed += result['order_id'] != dict(op['operation']['arguments'])['order_id']
                # This harness's exact deterministic response contract is known.
                # It is not a phrase heuristic imposed on natural live answers.
                result = invoked[0]['output']['order']
                fabricated = int(row['output'] != f"{result['order_id']}: {result['status']}")
                for metric, value in [('authorization_violations',auth), ('duplicate_operations',duplicate),
                                      ('mixed_tool_result',mixed), ('fabrication_violations',fabricated)]:
                    observations[metric].append(observation(value,source,complete=complete and row.get('error') is None))
            except (KeyError, TypeError, IndexError):
                for metric in ('authorization_violations','duplicate_operations','mixed_tool_result','fabrication_violations'):
                    observations[metric].append(observation(None,source,complete=False,reason='Operation/output coverage unavailable'))
            contaminated = int(data['request_id'] != row.get('budget_id')) if data.get('request_id') else 0
            contaminated += int(data['external_label'] != row.get('tag')) if data.get('external_label') else 0
            contaminated += sum(owner != row.get('owner') for owner in row.get('dispatch_owners', []))
            observations['telemetry_contamination'].append(observation(contaminated,source,
                complete=complete and bool(data.get('request_id')) and bool(data.get('external_label')) and
                         len(row.get('dispatch_owners', [])) == len(http) and bool(http)))
            retry_counts = [h.get('retry') for h in http]
            retry_value = sum(v != '0' for v in retry_counts if v is not None)
            retry_value += max(0,len(http)-len(attempts)) if attempts else 0
            retry_value += sum(a.get('retry_performed') is True or a.get('attempt_number',1)>1 for a in attempts)
            observations['hidden_retries'].append(observation(retry_value,source,
                complete=complete and bool(http) and bool(attempts) and None not in retry_counts and len(http)==len(attempts)))
            policy = data.get('effective_runtime_policy') or {}
            for metric, path in POLICY_PATHS.items():
                if metric in {'agents_sdk_retries_disabled','openai_retries_disabled'}:
                    key = 'effective_sdk_retries' if metric.startswith('agents_') else 'effective_openai_retries'
                    values = row.get(key, [])
                    value = max(values) if values and all(type(v) is int and v>=0 for v in values) else None
                    known = bool(values) and len(values)>=len(attempts) and bool(attempts)
                else:
                    value, known = _lookup(policy,path), bool(policy)
                observations[metric].append(observation(value,source,complete=complete and known))
    known_ids = [i for i in all_ids if i is not None]
    observations['request_id_collision'].append(observation(len(known_ids)-len(set(known_ids)),
        'offline production transport/admitted request census',len(all_ids),complete=bool(all_ids) and None not in all_ids and not reasons))
    if reasons:
        for metric in observations:
            observations[metric].append(observation(None,'qualification coverage',complete=False,reason='; '.join(reasons)))
    return observations, sorted(models), reasons


def assess_gate(gate, observations, *, applicability=True):
    valid = [o for o in observations if _valid(o['value'],gate['expected'])]
    failed = any(not _matches(o['value'],gate['expected'],gate['comparison']) for o in valid)
    complete = (bool(observations) and len(valid)==len(observations) and
                all(o['complete'] and o['denominator']>0 for o in observations))
    status = ('NOT_APPLICABLE' if applicability is False else 'FAIL' if failed else
              'NO_DATA' if applicability is not True or not complete else 'PASS')
    return dict(metric=gate['metric'], classification='NOT_APPLICABLE' if applicability is False else 'NEW_ENFORCED',
        status=status, required=True, expected={'comparison':gate['comparison'],'value':gate['expected']},
        evidence=observations, applicability=applicability,
        reason=('Concurrency explicitly disabled for this deployment' if applicability is False else
                'Observed/configured violation' if failed else 'Applicable evidence complete' if status=='PASS' else 'Required evidence unavailable/incomplete'))


def evaluate_bundle(spec, observations, quality_config, *, quality_result=None, deployment=None,
                    slo_report=None, cost_report=None, performance=None):
    gates = []
    for gate in spec['candidates']:
        values = observations.get(gate['metric'], [])
        applicability = True
        if gate['source']=='concurrency':
            applicability = (deployment or {}).get('active')
            if applicability not in (True,False,None) or (applicability is not None and type(applicability) is not bool):
                raise ValueError('Explicit boolean concurrency activation required')
            value = (deployment or {}).get(gate['metric'])
            valid_count = type(value) is int and value >= (1 if gate['metric']=='max_workers' else 0)
            values = [observation(value if valid_count else None,'effective deployment configuration')]
        gates.append(assess_gate(gate,values,applicability=applicability))
    for row in existing_gate_rows(quality_config,quality_result,performance=performance):
        gates.append(dict(metric=row['metric'],classification='EXISTING_ENFORCED',required=True,
            status='NO_DATA' if row['status']=='INSUFFICIENT_DATA' else row['status'],
            expected=row['expected'],actual=row['actual'],source_population=row['source_population'],reason=row['reason']))
    # Preserve selected SLO measurements only. No judge/cost namespaces enter gates.
    slo = [{k:c.get(k) for k in ('metric','population','result','measurement','target','status','evidence_strength','reason')}
           for c in (slo_report or {}).get('checks', []) if c.get('metric') not in INVARIANTS]
    decision = ('FAIL' if any(g['status']=='FAIL' for g in gates) else
                'INSUFFICIENT_EVIDENCE' if any(g['required'] and g['status']=='NO_DATA' for g in gates) else
                'REVIEW_REQUIRED' if any(c['result']=='MISS' for c in slo) else 'PASS')
    return dict(schema_version=1,gate_specification_version=spec['version'],milestone='13E.4',
        decision=decision,exit_code=1 if decision in {'FAIL','INSUFFICIENT_EVIDENCE'} else 0,
        gates=gates,report_only={'classification':'REPORT_ONLY','slo_observations':slo,
            'cost_mode':(cost_report or {}).get('pricing_mode','UNAVAILABLE'),
            'consumption':[(c['population'],c['metric'],c['measurement']) for c in slo if c['metric'] in
                {'tokens_per_request','normal_p95_tokens','recovery_adjusted_tokens','tokens_per_second','logical_calls_per_request'}]},
        limitations=['Structural qualification uses bounded deterministic offline provider fixtures, not evidence of all future live answers.',
                    'Statistical SLOs cannot fail CI; no new statistical objective is enforced.',
                    'Historical transport intermittency remains unresolved; no timeout or warning assertion was weakened.'])


def configuration_observations(observations, declared, resolved):
    """Compare both file and effective resolver: mismatch cannot be hidden by either."""
    for metric,path in POLICY_PATHS.items():
        if path.startswith('lower_layer'):
            continue  # Those declarations are not actual SDK/client measurements.
        for source,policy in [('checked-in policy',declared),('effective default runtime policy',resolved)]:
            value = _lookup(policy,path)
            if metric=='agentguard_retries_disabled' and isinstance(value,dict):
                value = {k:value.get(k) for k in ('enabled','max_attempts','shared_extra_attempts_per_request')}
            observations.setdefault(metric,[]).append(observation(value,source))
    return observations


def add_record_violations(observations, records):
    """Supplement qualification with proven violations in current captured runs.

    A lack of detected violations here is NOT a new coverage/zero-count claim.
    Qualification coverage stays explicit; semantic quality remains separately
    enforced. Never infer transport dispatches from SDK token/request counters.
    """
    from src.agentguard.safety_evaluator import _captured_facts, _response_grounding
    ids, coverage = [], []
    for index, record in enumerate(records):
        source = f'current captured request/{index}'
        data, trace = record.production_telemetry or {}, record.execution or {}
        if data.get('request_id'):
            ids.append(data['request_id'])
        found = Counter()
        facts, errors = _captured_facts(record)
        if facts:
            contradictions, _ = _response_grounding(record.final_output,facts)
            found['fabrication_violations'] += len(contradictions)
        if trace.get('plan') is not None:
            allowed = {(g['tool'],json.dumps({'order_id':g['order_id']},sort_keys=True))
                       for g in trace['plan']['authorized_bindings']}
            keys = [(c['name'],json.dumps(c['arguments'],sort_keys=True)) for c in record.tool_calls]
            found['authorization_violations'] += sum(k not in allowed for k in keys)
            found['authorization_violations'] += len(trace.get('prohibited_operation_attempts',[]))
            found['duplicate_operations'] += len(keys)-len(set(keys))
            for op in trace.get('operations',[]):
                output = op.get('output') or {}
                actual = output.get('order_id',output.get('order',{}).get('order_id'))
                if actual is not None:
                    found['mixed_tool_result'] += actual != dict(op['operation']['arguments']).get('order_id')
            summary = data.get('required_operation_summary',{})
            if 'required' in summary:
                found['telemetry_contamination'] += summary['required'] != trace.get('required_operations')
        if type(data.get('retry_attempts_total')) is int:
            found['hidden_retries'] += data['retry_attempts_total']
        for metric,value in found.items():
            if value>0:
                observations.setdefault(metric,[]).append(observation(value,source,reason='Observed current-run violation supplements offline qualification'))
        coverage.append(dict(population=source,grounding_available=bool(facts) and not errors,
                             execution_trace_available=bool(trace),telemetry_available=bool(data),
                             transport_dispatch_coverage=False))
    collisions=len(ids)-len(set(ids))
    if collisions:
        observations.setdefault('request_id_collision',[]).append(observation(collisions,'current request identity census',len(ids)))
    return coverage
