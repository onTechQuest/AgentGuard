"""Sanitized adapters for existing production evidence, never judge usage."""
from src.agentguard.slo import Measurement, PopulationEvidence, RequestEvidence, number

CHECKS = {'functional_accuracy': 'functional_correct', 'tool_accuracy': 'tool_correct',
          'argument_accuracy': 'arguments_correct', 'grounded_facts': 'grounding_correct',
          'prompt_injection_resistance': 'prompt_injection_pass', 'unsupported_action_protection': 'unsupported_action_pass',
          'data_protection': 'data_protection_pass', 'tool_policy_enforcement': 'tool_policy_pass'}
FAILURES = {'provider_failure_rate': 'provider_failure', 'rate_limit_rate': 'rate_limit',
            'deadline_rejection_rate': 'deadline_rejection', 'cancellation_rate': 'cancellation',
            'unknown_failure_rate': 'unknown_failure'}


def _checks(row, metrics):
    for metric, key in CHECKS.items():
        if key in row and (row[key] is None or type(row[key]) is bool):
            metrics[metric] = None if row[key] is None else int(row[key])


def _completion(outcome, business, metrics):
    if outcome in {'success', 'completed'}:
        if type(business) is bool:
            metrics['request_success'] = int(business)
        metrics['runtime_completion'] = 1
    elif outcome is not None:
        metrics['request_success'] = 0
        metrics['runtime_completion'] = 0
    for key, label in FAILURES.items():
        if outcome is not None:
            metrics[key] = int(outcome == label)


def _numeric(metrics, key, value):
    # Absence is unknown, not an applicable zero or a policy skip.
    if number(value) and value >= 0:
        metrics[key] = value


def _request(row):
    metrics = {'admission_ratio': 1, 'capacity_rejection_rate': 0, 'offered': 1, 'admitted': 1,
               'completed': int(row.get('outcome') is not None)}
    if 'worker_id' in row:
        metrics['started'] = int(row['worker_id'] >= 0)
    _checks(row, metrics)
    business = row.get('business_pass')
    if business is None and all(type(row.get(k)) is bool for k in ('tool_correct', 'arguments_correct', 'functional_correct', 'grounding_correct')):
        business = all(row[k] for k in ('tool_correct', 'arguments_correct', 'functional_correct', 'grounding_correct'))
    _completion(row.get('outcome'), business, metrics)
    for key in ('queue_wait_ms', 'service_ms', 'end_to_end_ms'):
        _numeric(metrics, key, row.get(key))
    for name, key, alternate in [('primary_router', 'router_ms', 'router_ms'), ('synthesis','synthesis_ms','synthesis_ms'),
                                  ('recovery_planner','recovery_ms','recovery_ms')]:
        value = row.get('components', {}).get(name, {}).get('duration_ms', row.get(alternate))
        _numeric(metrics, key, value)
    complete = row.get('usage_completeness')
    if complete is not None:
        metrics['usage_completeness'] = int(complete == 'COMPLETE')
    if complete == 'COMPLETE':
        _numeric(metrics, 'tokens_per_request', row.get('observed_usage', {}).get('total_tokens', row.get('tokens')))
        if 'tokens_per_request' in metrics:
            metrics['normal_p95_tokens'] = metrics['tokens_per_request']
    _numeric(metrics, 'logical_calls_per_request', row.get('logical_model_calls'))
    recovery = row.get('recovery_count')
    if type(recovery) is int and recovery >= 0:
        metrics['recovery_rate'] = int(recovery > 0)
        if recovery > 0:
            if 'request_success' in metrics:
                metrics['recovery_success'] = metrics['request_success']
        else:
            metrics['recovery_success'] = None
            metrics['recovery_ms'] = None
        if 'tokens_per_request' in metrics:
            metrics['recovery_adjusted_tokens'] = metrics['tokens_per_request'] - (852.16 + int(recovery > 0)*791.84)
    violations = row.get('isolation_violations')
    if isinstance(violations, list):
        mapping = {'fabrication_violations': {'conflicting_response_fact'},
            'authorization_violations': {'unauthorized_operation'},
            'duplicate_operations': {'duplicate_operation'},
            'mixed_tool_result': {'mixed_or_incorrect_authoritative_output'},
            'telemetry_contamination': {'telemetry_contamination', 'dispatch_contamination'},
            'hidden_retries': {'hidden_retry', 'extra_http_dispatch'}}
        for key, names in mapping.items():
            metrics[key] = sum(v in names for v in violations)
        metrics['tool_policy_enforcement'] = int(not metrics['authorization_violations'])
    if number(row.get('extra_retry_attempts')):
        metrics['hidden_retries'] = max(metrics.get('hidden_retries', 0), row['extra_retry_attempts'])
    if 'deadline' in row and row['deadline'].get('result_abandoned') is not None:
        metrics['late_abandoned_rate'] = int(bool(row['deadline']['result_abandoned']))
    elif type(row.get('late_result')) is bool:
        metrics['late_abandoned_rate'] = int(row['late_result'])
    return RequestEvidence(row['request_id'], True, row.get('outcome'), metrics, recovery)


def _single(value):
    return Measurement(value, 1 if number(value) else 0, 1, 0 if number(value) else 1, True)


def from_concurrency(report, source='concurrency'):
    populations = []
    seen = set()
    for key, stage in report.get('stages', {}).items():
        if stage.get('status') != 'COMPLETE' or stage.get('stop_reasons'):
            # Preserve stopped evidence for diagnostics but do not assess a
            # completed statistical window from a truncated campaign.
            window = 'incomplete_batch'
        else:
            window = 'qualification_batch'
        rows = {r['request_id']: r for r in stage.get('requests', [])}
        if len(rows) != len(stage.get('requests', [])):
            raise ValueError('Duplicate completion identity')
        requests = []
        for a in stage.get('admissions', []):
            if a['request_id'] in seen:
                raise ValueError('Duplicate admission identity')
            seen.add(a['request_id'])
            if a['accepted'] and a['request_id'] in rows:
                requests.append(_request(rows.pop(a['request_id'])))
            else:
                accepted = a['accepted']
                outcome = None if accepted else 'capacity_rejected' if a.get('reason') == 'capacity_exhausted' else 'other_rejection'
                requests.append(RequestEvidence(a['request_id'], accepted, outcome,
                    {'admission_ratio': int(accepted), 'capacity_rejection_rate': int(outcome == 'capacity_rejected'),
                     'offered': 1, 'admitted': int(accepted), 'completed': 0}))
        if rows:
            raise ValueError('Completion without matching admission')
        runtime = stage.get('runtime', {})
        admission = stage.get('collector', {}).get('admission', {})
        obs = {'active_workers': _single(runtime.get('peak_active_requests')),
               'queue_depth': _single(admission.get('max_observed_queue_depth')),
               'request_id_collision': _single(stage.get('collector', {}).get('isolation', {}).get('request_id_collision'))}
        if runtime.get('queue_capacity') == 0:
            obs['queue_saturation_fraction'] = Measurement(None, 0, 0, 0, False)
        seconds = stage.get('throughput', {}).get('observation_seconds')
        if number(seconds) and seconds > 0:
            obs['throughput'] = _single(sum(r.metrics.get('completed', 0) for r in requests)/seconds)
            admitted = [r for r in requests if r.admitted]
            if admitted and all('tokens_per_request' in r.metrics for r in admitted):
                obs['tokens_per_second'] = _single(sum(r.metrics['tokens_per_request'] for r in admitted)/seconds)
        populations.append(PopulationEvidence(f'{source}/stage_{key}', window, requests, obs,
            ['Finite burst; not a sustained or rolling production window.',
             'Peak workers/queue do not establish saturation duration; time-series SLIs unavailable.',
             'Missing safety classifiers are unassessed, never inferred PASS from functional correctness.']))
    return populations


def from_cost(report):
    groups = {}
    seen = set()
    for row in report.get('production_observations', []):
        if row['identity'] in seen:
            raise ValueError('Duplicate cost request identity')
        seen.add(row['identity'])
        # Cost populations remain separate. No judge namespace is read.
        key = 'cost/' + row['population']
        adapted = dict(request_id=row['identity'], outcome=row['outcome'], business_pass=row.get('business_pass'),
            tokens=row['tokens'].get('total_tokens'), usage_completeness=row['usage_completeness'],
            logical_model_calls=row['logical_calls'], recovery_count=row.get('recovery_count'))
        if row['admitted']:
            evidence = _request(adapted)
        else:
            evidence = RequestEvidence(row['identity'], False, row['outcome'],
                {'offered': 1, 'admitted': 0, 'admission_ratio': 0, 'capacity_rejection_rate': int(row['outcome'] == 'capacity_rejected')})
        groups.setdefault(key, []).append(evidence)
    return [PopulationEvidence(k, requests=v, admission_census_complete=False,
            notes=['Cost-only coverage: missing runtime/safety/latency evidence remains unavailable; a request subset is not a full ingress census.'])
            for k, v in groups.items()]


def from_diagnostic(report, source='recovery_diagnostic'):
    checks = report.get('diagnostic', {})
    business = all(checks.get(k) is True for k in ('functional_pass','tool_pass','argument_pass','grounding_pass'))
    available = all(type(checks.get(k)) is bool for k in ('functional_pass','tool_pass','argument_pass','grounding_pass'))
    rows = [_request(dict(r, business_pass=business if available else None)) for r in report.get('requests', [])]
    return PopulationEvidence(source, requests=rows,
        admission_census_complete=(report.get('admission', {}).get('offered') == len(rows) and
                                   report.get('admission', {}).get('admitted') == len(rows)), notes=[
        'Targeted recovery sample, not a representative production recovery cohort.',
        'Recovery stage duration is measured, but causal extra end-to-end latency is not identified.'])


def from_telemetry(data, *, business_checks=None, source='telemetry'):
    """One captured production request; safety checks must be supplied explicitly."""
    outcome = {'completed': 'success', 'success': 'success', 'failed': 'unknown_failure', 'running': None}.get(data.get('terminal_status'), data.get('terminal_status'))
    category = data.get('terminal_failure_category')
    outcome = {'RATE_LIMIT':'rate_limit','PROVIDER_ERROR':'provider_failure','DEADLINE_EXHAUSTED':'deadline_rejection',
               'CANCELLED':'cancellation'}.get(category, outcome)
    spans = data.get('component_spans', [])
    components = {}
    for component in ('primary_router','synthesis','recovery_planner'):
        selected = [s for s in spans if s.get('component') == component]
        if selected and all(number(s.get('duration_ms')) for s in selected):
            components[component] = {'duration_ms': sum(s['duration_ms'] for s in selected)}
    row = dict(request_id=data['request_id'], outcome=outcome, observed_usage=data.get('observed_usage') or {},
        usage_completeness=data.get('usage_completeness'), components=components,
        recovery_count=data.get('planning_summary', {}).get('recovery_count'),
        extra_retry_attempts=data.get('retry_attempts_total'), deadline={'result_abandoned': data.get('result_abandoned')})
    calls = [s.get('logical_model_calls') for s in spans if s.get('component') in ('primary_router','synthesis','recovery_planner')]
    if calls and all(type(c) is int for c in calls):
        row['logical_model_calls'] = sum(calls)
    evidence = _request(row)
    if business_checks:
        # Explicit checks only, no caller usage fields or evaluation/judge tokens.
        _checks(business_checks, evidence.metrics)
        expected = ('functional_correct','tool_correct','arguments_correct','grounding_correct')
        if all(type(business_checks.get(k)) is bool for k in expected):
            _completion(outcome, all(business_checks[k] for k in expected), evidence.metrics)
    return PopulationEvidence(source, requests=[evidence], admission_census_complete=False,
        notes=['Production total latency lacks hosting queue/terminal timestamps; not relabeled end-to-end.'])
