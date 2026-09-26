"""Allowlisted offline report adapters. Never recurse into judge or summary totals."""
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from src.agentguard.cost_model import ComponentUsage, ProductionObservation, Tokens

NAMES = {"primary_router": "router", "router": "router", "recovery_planner": "recovery",
         "planning_recovery": "recovery", "synthesis": "synthesis", "agent": "synthesis"}


def tokens(value):
    value = value or {}
    return Tokens(**{k: value.get(k) for k in Tokens.__dataclass_fields__})


def single_model(values):
    values = set(v for v in values if v)
    return next(iter(values)) if len(values) == 1 else None


def from_telemetry(data, *, source="telemetry", population="production", outcome=None, provider_dispatches=None):
    """Accept production telemetry only, not a combined evaluation record's totals."""
    components = tuple(ComponentUsage(NAMES[s['component']], s.get('logical_model_calls'), tokens(s),
                       s.get('resolved_model')) for s in data.get('component_spans', []) if s['component'] in NAMES)
    counts = [c.calls for c in components]
    logical = sum(counts) if counts and all(c is not None for c in counts) else None
    return ProductionObservation(data['request_id'], source, population, outcome or data.get('terminal_status', 'unknown'),
        data.get('usage_completeness', 'UNAVAILABLE'), tokens(data.get('observed_usage')), logical, provider_dispatches,
        components, data.get('planning_summary', {}).get('recovery_count'), single_model(c.model for c in components),
        deadline_after_dispatch=data.get('terminal_failure_category') == 'DEADLINE_EXHAUSTED' and bool(provider_dispatches))


def _concurrency(row, source, population):
    components = tuple(ComponentUsage(NAMES[name], c.get('calls'),
        Tokens(total_tokens=c.get('known_tokens')), single_model(c.get('resolved_models', [])))
        for name, c in row.get('components', {}).items() if name in NAMES and c.get('calls'))
    # Collector-only reports retain total call count and stage presence. With exactly
    # two/three calls and two/three observed model stages, each stage ran once.
    if not components:
        observed = [('router', row.get('router_ms')), ('recovery', row.get('recovery_ms')), ('synthesis', row.get('synthesis_ms'))]
        present = [name for name, duration in observed if duration is not None and duration > 0]
        if present and len(present) == row.get('logical_model_calls'):
            components = tuple(ComponentUsage(name, 1, Tokens()) for name in present)
    dispatches = row.get('http_dispatches', row.get('provider_dispatches'))
    expired = row.get('expired_before_service', False)
    zero = (expired and row.get('expired_without_dispatch_or_tool') is True and
            row.get('logical_model_calls') == 0 and dispatches == 0 and not row.get('dispatch_evidence') and
            not any(o.get('invoked') for o in row.get('operations', [])))
    return ProductionObservation(row['request_id'], source, population, row['outcome'],
        "COMPLETE" if zero else row.get('usage_completeness', 'UNAVAILABLE'),
        Tokens(0, 0, 0, 0) if zero else tokens(row.get('observed_usage', {'total_tokens': row.get('tokens')})),
        row.get('logical_model_calls'), dispatches, components, row.get('recovery_count'),
        single_model(c.model for c in components), row.get('scenario_id'), zero_work_proven=zero,
        expired_before_service=expired, deadline_after_dispatch=row['outcome'] == 'deadline_rejection' and bool(dispatches),
        business_pass=row.get('business_pass'))


def _audit(row, source, population, campaign):
    if row.get('usage_reconciled') is not True:
        raise ValueError('UNRECONCILED_PRODUCTION_USAGE')
    components = tuple(ComponentUsage(NAMES[c['component']], c.get('model_responses', c.get('requests')),
        tokens(c), c.get('model')) for c in row['components'] if c['component'] in NAMES)
    usage = tokens(row['production_usage'])
    if any(c.tokens.total_tokens is None for c in components) or sum(c.tokens.total_tokens for c in components) != usage.total_tokens:
        raise ValueError('COMPONENT_TOTAL_MISMATCH')
    identity = f"audit:{campaign}:{row.get('started_at')}:{row['scenario_id']}:{row.get('repetition', 1)}"
    return ProductionObservation(identity, source, population, row.get('status', 'completed'), 'COMPLETE', usage,
        sum(c.calls for c in components) if all(c.calls is not None for c in components) else None,
        None, components, sum(c.calls for c in components if c.name == 'recovery'), single_model(c.model for c in components), row['scenario_id'])


def _reliability(row, source, population, campaign):
    if row.get('source') != 'production':
        raise ValueError('NON_PRODUCTION_OBSERVATION')
    attempts = row.get('attempts', [])
    counts = Counter(NAMES[a['component']] for a in attempts if a.get('attempt_number') == 1 and a['component'] in NAMES)
    model = single_model(row.get('models', []))
    components = tuple(ComponentUsage(NAMES[name], counts[NAMES[name]], tokens(value), model)
                       for name, value in row['tokens'].items() if name in NAMES and counts[NAMES[name]])
    observed = [a.get('transport_attempts_observed') for a in attempts]
    dispatches = sum(observed) if observed and all(v is not None for v in observed) else None
    population += ('/selected_recovery_probe' if row.get('recovery_probe') else '')
    population += '/' + row.get('dataset', 'unknown') + '/' + ('multi_op' if row.get('required_operation_count', 0) > 1
                  else 'single_op' if row.get('required_operation_count') == 1 else 'no_op')
    return ProductionObservation(f"reliability:{campaign}:{row['scenario_id']}:{row['repetition']}", source, population,
        row['status'], 'PARTIAL' if row.get('observation_incomplete') else row['usage_completeness'],
        tokens(row['tokens']['production_total']), sum(counts.values()) if attempts else None,
        dispatches, components, counts['recovery'], model, row['scenario_id'],
        deadline_after_dispatch=row.get('failure_category') == 'DEADLINE_EXHAUSTED' and any(a.get('usage_known') or a.get('transport_attempts_observed') for a in attempts))


class EvidenceLedger:
    def __init__(self):
        self.rows = {}
        self.sources = []
        self.excluded = []
        self.evaluation_usage = []
        self._hashes = set()
        self._conflicts = set()

    def add(self, row):
        if row.identity in self._conflicts:
            self.excluded.append(dict(source=row.source, reason='CONFLICTING_REQUEST_EVIDENCE'))
            return False
        if row.identity in self.rows:
            prior = self.rows[row.identity]
            comparable = lambda r: {k: v for k, v in asdict(r).items() if k not in {'source', 'population'}}
            if comparable(prior) != comparable(row):
                self.rows.pop(row.identity)
                self._conflicts.add(row.identity)
                reason = 'CONFLICTING_REQUEST_EVIDENCE'
            else:
                reason = 'DUPLICATE_REQUEST_EVIDENCE'
            self.excluded.append(dict(source=row.source, reason=reason))
            return False
        self.rows[row.identity] = row
        return True

    def ingest(self, document, source, *, population=None):
        """Ingest a recognized completed report; retain exclusion reasons and lineage."""
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        self.sources.append(dict(source=source, sha256=digest))
        if digest in self._hashes:
            self.excluded.append(dict(source=source, reason='DUPLICATE_DOCUMENT'))
            return
        self._hashes.add(digest)
        population = population or source
        if not isinstance(document, dict) or document.get('complete') is False:
            self.excluded.append(dict(source=source, reason='INCOMPLETE_OR_UNSUPPORTED_REPORT'))
            return
        def capture(make, raw):
            try:
                row = make(raw)
                added = self.add(row)
                # Explicit namespace only. Never add evaluation counters to production.
                if added and 'evaluation_usage' in raw:
                    usage = raw['evaluation_usage']
                    self.evaluation_usage.append(dict(source=source, identity=row.identity,
                        usage={k: usage.get(k) for k in ('model_calls', 'input_tokens', 'output_tokens', 'total_tokens')}))
            except (KeyError, TypeError, ValueError) as error:
                self.excluded.append(dict(source=source, reason='INVALID_OR_UNTRUSTED_ROW', error_type=type(error).__name__))
        if document.get('campaign') in {'13D.4', '13D.5'} and 'stages' in document:
            for stage_id, stage in document['stages'].items():
                if (stage.get('status') != 'COMPLETE' or stage.get('stop_reasons') or
                        stage.get('admitted') != len(stage.get('requests', []))):
                    self.excluded.append(dict(source=source, stage=stage_id, reason='INCOMPLETE_OR_STOPPED_STAGE'))
                    continue
                group = population + '/stage_' + stage_id
                for row in stage['requests']:
                    capture(lambda r: _concurrency(r, source, group), row)
                completed_ids = {r['request_id'] for r in stage['requests']}
                dispatch_ids = {d.get('request_id') for r in stage['requests'] for d in r.get('dispatch_evidence', [])}
                for admission in stage.get('admissions', []):
                    if admission.get('accepted') is False and admission.get('reason') == 'capacity_exhausted':
                        if admission['request_id'] in completed_ids | dispatch_ids:
                            self.excluded.append(dict(source=source, reason='REJECTION_HAS_EXECUTION_EVIDENCE'))
                            continue
                        self.add(ProductionObservation(admission['request_id'], source, group, 'capacity_rejected',
                            'COMPLETE', Tokens(0, 0, 0, 0), 0, 0, admitted=False, zero_work_proven=True))
        elif 'diagnostic' in document and 'requests' in document:
            if document.get('admission', {}).get('admitted') != len(document['requests']):
                self.excluded.append(dict(source=source, reason='INCOMPLETE_DIAGNOSTIC'))
                return
            for row in document['requests']:
                capture(lambda r: replace(_concurrency(r, source, population),
                    business_pass=all(document['diagnostic'].get(k) is True for k in
                        ('functional_pass', 'tool_pass', 'argument_pass', 'grounding_pass'))), row)
        elif document.get('complete') is True and document.get('suite') == 'reliability':
            for row in document['observations']:
                capture(lambda r: _reliability(r, source, population, document['started_at']), row)
            usage = document.get('evaluator_usage', {})
            self.evaluation_usage.append(dict(source=source, scope='campaign',
                usage={k: usage.get(k) for k in ('judge_calls', 'tokens')}))
        elif document.get('complete') is True and ('observations' in document or 'scenarios' in document):
            for row in document.get('observations', document.get('scenarios', [])):
                capture(lambda r: _audit(r, source, population, document['started_at']), row)
            for _ in document.get('prior_attempts', []):
                self.excluded.append(dict(source=source, reason='PRIOR_ATTEMPT_WITHOUT_REQUEST_USAGE'))
        else:
            self.excluded.append(dict(source=source, reason='UNSUPPORTED_REPORT_SCHEMA'))

    def read(self, path, *, population=None):
        path = Path(path)
        try:
            document = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            self.excluded.append(dict(source=path.as_posix(), reason='MISSING_OR_INVALID_JSON'))
            return
        self.ingest(document, path.as_posix(), population=population)
