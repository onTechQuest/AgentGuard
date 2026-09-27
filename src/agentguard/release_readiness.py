"""Offline shadow decisions. Never imported by the production or CI release path."""
from collections import Counter
from copy import deepcopy
from pathlib import Path

import yaml

from src.agentguard.slo import number, compare

SECTIONS = ('QUALITY', 'SAFETY', 'RELIABILITY', 'CONCURRENCY / ISOLATION',
            'CONFIGURATION', 'PERFORMANCE', 'CONSUMPTION', 'SLO OBSERVATION')
READINESS = {'EXISTING_ENFORCED', 'CANDIDATE_ENFORCE_READY', 'REPORT_ONLY',
             'INSUFFICIENT_EVIDENCE', 'NOT_APPLICABLE'}
INVARIANTS = {'fabrication_violations', 'authorization_violations', 'hidden_retries',
              'duplicate_operations', 'request_id_collision', 'mixed_tool_result', 'telemetry_contamination'}
POLICY_PATHS = {'agentguard_retries_disabled': 'model_retry_policy',
    'agents_sdk_retries_disabled': 'lower_layer_retry_policy.agents_sdk_max_retries',
    'openai_retries_disabled': 'lower_layer_retry_policy.openai_client_max_retries',
    **{k: k for k in ('request_deadline_ms', 'router_allowance_ms', 'recovery_reserve_ms', 'synthesis_allowance_ms')}}


def load_candidate_spec(path):
    doc = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if doc.get('version') != 1 or doc.get('mode') != 'SHADOW':
        raise ValueError('Only version 1 SHADOW candidate specifications are supported')
    seen = set()
    for gate in doc['candidates']:
        metric, source = gate['metric'], gate['source']
        allowed = (source == 'slo' and metric in INVARIANTS or
                   source == 'policy' and POLICY_PATHS.get(metric) == gate.get('path') or
                   source == 'concurrency' and metric in {'max_workers', 'queue_capacity'})
        if (not allowed or metric in seen or gate['section'] not in SECTIONS or
                gate['comparison'] not in {'==', '<='} or not gate.get('promotion')):
            raise ValueError('Invalid/duplicate candidate or attempted statistical SLO promotion')
        seen.add(metric)
    if seen != INVARIANTS | POLICY_PATHS.keys() | {'max_workers', 'queue_capacity'}:
        raise ValueError('Required candidate coverage is incomplete')
    if not doc.get('promotion_requirements') or not doc.get('statistical_promotion_requirements'):
        raise ValueError('Promotion criteria are required')
    return doc


def _row(metric, section, expected, actual, status, readiness, source, reason,
         *, strength='MODERATE', required=True, candidate=False, **extra):
    return dict(metric=metric, section=section, expected=expected, actual=actual,
        status=status, readiness_class=readiness, evidence_strength=strength,
        source_population=source, reason=reason, required_scope=required,
        enforcement='SHADOW' if candidate else 'REFERENCE_ONLY',
        shadow_result=({'PASS': 'WOULD_PASS', 'FAIL': 'WOULD_FAIL'}.get(status, 'NO_DATA') if candidate else None),
        **extra)


def _lookup(data, path):
    for part in path.split('.'):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _valid(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _valid(actual[k], v) for k, v in expected.items())
    if type(expected) is bool:
        return type(actual) is bool
    return number(actual) and actual >= 0


def _matches(actual, expected, comparison):
    if isinstance(expected, dict):
        return all(_matches(actual[k], v, '==') for k, v in expected.items())
    return compare(actual, expected, comparison)


def _candidate(gate, actual, source, reason, *, applicable=True, complete=True, strength='MODERATE'):
    expected = gate['expected']
    valid = _valid(actual, expected)
    # A known violation remains visible even when other coverage is incomplete.
    violated = valid and not _matches(actual, expected, gate['comparison'])
    status = ('NOT_APPLICABLE' if applicable is False else 'FAIL' if violated else
              'INSUFFICIENT_DATA' if applicable is None or not complete or not valid else 'PASS')
    readiness = ('NOT_APPLICABLE' if applicable is False else 'INSUFFICIENT_EVIDENCE'
                 if status == 'INSUFFICIENT_DATA' else 'CANDIDATE_ENFORCE_READY')
    return _row(gate['metric'], gate['section'], {'comparison': gate['comparison'], 'value': expected},
        actual, status, readiness, source, reason, candidate=True,
        strength='INSUFFICIENT' if status == 'INSUFFICIENT_DATA' and strength == 'MODERATE' else strength,
        promotion=gate['promotion'], candidate_class='CANDIDATE_ENFORCE_READY')


def existing_gate_rows(config, result=None, *, source='existing quality-gate result', performance=None):
    """Retain actual existing decisions; never infer quality from load success."""
    checks = (result or {}).get('checks', [])
    by_metric = {c['metric']: c for c in checks}
    if len(by_metric) != len(checks):
        raise ValueError('Duplicate existing gate result')
    rows = []
    for metric, rule in config['quality_gates'].items():
        comparison = '>=' if 'minimum' in rule else '<='
        threshold = next(iter(rule.values()))
        check = by_metric.get(metric)
        section = ('SAFETY' if metric in {'safety_pass_rate', 'prompt_injection_failures',
            'unsupported_action_failures', 'data_protection_failures', 'tool_policy_failures'} else
            'PERFORMANCE' if metric == 'p95_latency_ms' else 'CONSUMPTION' if metric == 'average_tokens_per_run' else 'QUALITY')
        status, actual, reason, population = 'INSUFFICIENT_DATA', None, 'No retained existing gate result; no new evaluation executed.', source
        if check:
            actual = check.get('actual')
            if check.get('threshold') != threshold or check.get('comparison') != comparison:
                reason = 'Saved result does not match current gate configuration.'
            elif check.get('enforced') is False:
                status, reason = 'NOT_APPLICABLE', 'Existing caller explicitly deferred this check; enforcement semantics preserved.'
            elif type(check.get('passed')) is bool:
                status = 'PASS' if check['passed'] else 'FAIL'
                reason = 'Existing gate decision retained, including fail-closed unavailable metrics.'
        if metric == 'p95_latency_ms' and performance is not None and status in {'INSUFFICIENT_DATA', 'NOT_APPLICABLE'}:
            q = performance.get('qualification', {})
            actual = q.get('p95_latency_ms')
            population = 'saved sequential performance qualification'
            if q.get('threshold_ms') == threshold and type(q.get('passed')) is bool:
                status = 'PASS' if q['passed'] else 'FAIL'
                reason = 'Existing sequential qualification verdict retained; not a load SLO.'
            else:
                status, reason = 'INSUFFICIENT_DATA', 'Missing/mismatched sequential qualification evidence.'
        rows.append(_row(metric, section, {'comparison': comparison, 'value': threshold}, actual,
            status, 'EXISTING_ENFORCED', population, reason,
            strength='MODERATE' if status in {'PASS', 'FAIL'} else 'INSUFFICIENT'))
    return rows


def evaluate_release_readiness(spec, slo_report, quality_config, *, quality_result=None,
                               policy_snapshots=None, concurrency_profiles=None, performance=None):
    """Return an advisory decision, not the decision used by release CI.

    policy_snapshots: source label -> sanitized declared/effective policy mapping.
    concurrency_profiles: source label -> {active: bool, max_workers, queue_capacity}.
    Missing activation is unknown; explicit active=False is the only N/A shortcut.
    """
    if spec['mode'] != 'SHADOW':
        raise ValueError('Cannot activate candidate enforcement')
    rows = existing_gate_rows(quality_config, quality_result, performance=performance)
    slo_checks = slo_report.get('checks', [])
    for gate in spec['candidates']:
        if gate['source'] == 'slo':
            observations = [c for c in slo_checks if c['metric'] == gate['metric'] and
                c['population'].startswith(spec['invariant_population_prefix'])]
            if not observations:
                rows.append(_candidate(gate, None, 'qualified runtime evidence', 'No qualified invariant observation.'))
            for c in observations:
                m = c.get('measurement', {})
                rows.append(_candidate(gate, m.get('value'), c['population'], c['reason'],
                    complete=(c['result'] in {'PASS', 'MISS'} and not m.get('missing', 0)
                              and c.get('evidence_strength') in {'MODERATE', 'STRONG'}),
                    strength=c.get('evidence_strength', 'INSUFFICIENT')))
        elif gate['source'] == 'policy':
            for source, policy in (policy_snapshots or {'policy evidence unavailable': {}}).items():
                # Checked-in JSON intentionally lacks lower-layer declarations.
                # Do not invent them from the application retry flag.
                actual = _lookup(policy, gate['path'])
                if isinstance(gate['expected'], dict) and isinstance(actual, dict):
                    actual = {k: actual.get(k) for k in gate['expected']}
                rows.append(_candidate(gate, actual, source,
                    'Configuration snapshot comparison only; does not establish observed transport suppression.'))
        else:
            for source, profile in (concurrency_profiles or {'deployment concurrency unavailable': {}}).items():
                active = profile.get('active')
                if active is not None and type(active) is not bool:
                    raise ValueError('Concurrency activation must be explicit boolean')
                value = profile.get(gate['metric'])
                valid_count = type(value) is int and value >= (1 if gate['metric'] == 'max_workers' else 0)
                rows.append(_candidate(gate, value if valid_count else None, source,
                    'Qualification boundary only; not a throughput SLO or an optimal deployment size.',
                    applicable=active))
    # Statistical targets never become candidates, even if all samples pass.
    for c in slo_checks:
        if c['metric'] in INVARIANTS:
            continue
        status = {'PASS': 'PASS', 'MISS': 'MISS', 'INSUFFICIENT_DATA': 'INSUFFICIENT_DATA',
                  'NOT_APPLICABLE': 'NOT_APPLICABLE'}[c['result']]
        readiness = ('INSUFFICIENT_EVIDENCE' if c['evidence_strength'] == 'INSUFFICIENT' else 'REPORT_ONLY')
        if c.get('reason') == 'NO_APPLICABLE_OBSERVATIONS':
            readiness = 'NOT_APPLICABLE'
        section = ('CONSUMPTION' if 'token' in c['metric'] or c['metric'] in {'usage_completeness', 'logical_calls_per_request'} else 'SLO OBSERVATION')
        rows.append(_row(c['metric'], section, {'comparison': c['comparison'], 'value': c['target']},
            c['measurement']['value'], status, readiness, c['population'], c['reason'],
            strength=c['evidence_strength'], required=False, slo_status=c['status'],
            observed_comparison=c.get('observed_comparison'),
            promotion=spec['statistical_promotion_requirements']))
    decision = ('FAIL' if any(r['status'] == 'FAIL' for r in rows) else
                'REVIEW_REQUIRED' if any(r['status'] == 'MISS' for r in rows) else
                'INSUFFICIENT_EVIDENCE' if any(r['required_scope'] and r['status'] == 'INSUFFICIENT_DATA' for r in rows) else 'PASS')
    shadow = Counter(r['shadow_result'] for r in rows if r['shadow_result'])
    return dict(schema_version=1, milestone='13E.3', mode='SHADOW', advisory_decision=decision,
        ci_exit_code=0, changes_current_release_decision=False,
        sections={s: [r for r in rows if r['section'] == s] for s in SECTIONS},
        shadow_counts={s: shadow[s] for s in ('WOULD_PASS', 'WOULD_FAIL', 'NO_DATA')},
        promotion_requirements=deepcopy(spec['promotion_requirements']),
        caveats=['Historical transport-probe intermittency is retained; latest 13E.2 authoritative run was 1958 PASS. No new transport gate inferred.',
                 'Saved qualification populations are not proof of current deployment configuration; review revision/source attribution before promotion.',
                 'Missing quality/safety results are not reconstructed from functional load observations.',
                 'Report-only SLO observations and candidate verdicts cannot change the CI release decision.'])
