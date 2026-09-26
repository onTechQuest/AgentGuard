"""Report-only SLI arithmetic and SLO assessment; no runtime or release imports."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_FLOOR
import math

from src.agentguard.cost_model import statistics

CLASSIFICATIONS = {'HARD_INVARIANT', 'STATISTICAL_SLO', 'OPERATIONAL_GUARDRAIL', 'REPORT_ONLY'}
STRENGTHS = {'STRONG', 'MODERATE', 'WEAK', 'INSUFFICIENT'}
STATUSES = {'PROVISIONAL', 'CANDIDATE', 'QUALIFIED'}
COMPARISONS = {'<=', '>=', '=='}
POPULATIONS = {'offered', 'admitted', 'normal', 'recovery'}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class SLOSpec:
    metric: str
    domain: str
    classification: str
    population: str
    target: float | None
    window: str
    comparison: str
    evidence_strength: str
    status: str
    enforcement: str
    aggregation: str
    minimum_samples: int
    definition: str
    denominator: str
    rationale: str
    alert_bands: dict = field(default_factory=dict)

    def __post_init__(self):
        if (self.classification not in CLASSIFICATIONS or self.population not in POPULATIONS or
                self.comparison not in COMPARISONS or self.evidence_strength not in STRENGTHS or
                self.status not in STATUSES or self.enforcement not in {'REPORT_ONLY', 'ENFORCED'} or
                self.aggregation not in {'rate', 'mean', 'p95', 'max', 'sum'}):
            raise ValueError('Invalid SLO vocabulary')
        if self.target is not None and (not number(self.target) or self.target < 0):
            raise ValueError('Invalid SLO target')
        if type(self.minimum_samples) is not int or self.minimum_samples < 1:
            raise ValueError('minimum_samples must be a positive integer')
        if not all(isinstance(v, str) and v.strip() for v in (self.metric, self.domain, self.window, self.definition, self.denominator, self.rationale)):
            raise ValueError('SLO definitions and window are required')
        if any(not number(v) or v < 0 for v in self.alert_bands.values()):
            raise ValueError('Invalid alert band')


@dataclass(frozen=True)
class RequestEvidence:
    request_id: str
    admitted: bool
    outcome: str | None
    # Only explicitly observed metrics. Missing key means unknown, None means
    # explicitly not applicable (e.g. a policy marked skipped).
    metrics: dict
    recovery_count: int | None = None


@dataclass(frozen=True)
class Measurement:
    value: float | None
    samples: int
    eligible: int
    missing: int
    applicable: bool | None
    numerator: float | None = None
    denominator: int | None = None


@dataclass
class PopulationEvidence:
    name: str
    window: str = 'qualification_batch'
    requests: list[RequestEvidence] = field(default_factory=list)
    observations: dict[str, Measurement] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    admission_census_complete: bool = True

    def __post_init__(self):
        ids = [r.request_id for r in self.requests]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate request IDs must be resolved before scoring')


def denominators(population):
    rows = population.requests
    admitted = [r for r in rows if r.admitted]
    expected = sum(not r.admitted and r.outcome == 'capacity_rejected' for r in rows)
    successes = sum(r.metrics.get('request_success') == 1 for r in admitted)
    failures = sum(r.metrics.get('request_success') == 0 for r in admitted)
    unknown = len(admitted) - successes - failures
    started_known = [r.metrics['started'] for r in admitted if r.metrics.get('started') in (0, 1)]
    completed_known = [r.metrics['completed'] for r in admitted if r.metrics.get('completed') in (0, 1)]
    return dict(offered=len(rows) if population.admission_census_complete else None,
        admission_census_complete=population.admission_census_complete,
        admitted=len(admitted), expected_capacity_rejections=expected,
        other_admission_rejections=sum(not r.admitted and r.outcome != 'capacity_rejected' for r in rows),
        started=sum(started_known) if len(started_known) == len(admitted) else None,
        completed=sum(completed_known) if len(completed_known) == len(admitted) else None,
        started_coverage=len(started_known), completed_coverage=len(completed_known),
        success=successes, unexpected_failure=failures, unassessed_or_pending=unknown,
        execution_failure_rate=failures/len(admitted) if admitted and not unknown else None,
        admission_ratio=len(admitted)/len(rows) if rows and population.admission_census_complete else None,
        capacity_rejection_rate=expected/len(rows) if rows and population.admission_census_complete else None)


def measure(spec, population):
    if spec.population == 'offered' and not population.admission_census_complete:
        return Measurement(None, 0, 1, 1, None)
    if spec.metric in population.observations:
        return population.observations[spec.metric]
    rows = population.requests
    if spec.population != 'offered':
        rows = [r for r in rows if r.admitted]
    selection_unknown = 0
    if spec.population in {'normal', 'recovery'}:
        selection_unknown = sum(r.recovery_count is None for r in rows)
        rows = [r for r in rows if r.recovery_count is not None and
                (r.recovery_count == 0 if spec.population == 'normal' else r.recovery_count > 0)]
    missing = selection_unknown
    values = []
    skipped = 0
    for r in rows:
        if spec.metric not in r.metrics:
            missing += 1
        elif r.metrics[spec.metric] is None:
            skipped += 1
        elif number(r.metrics[spec.metric]):
            v = r.metrics[spec.metric]
            if spec.aggregation == 'rate' and v not in (0, 1):
                missing += 1
            else:
                values.append(v)
        else:
            missing += 1
    eligible = len(values) + missing
    applicable = True if eligible else False if skipped or population.requests else None
    s = statistics(values)
    value = s['mean' if spec.aggregation == 'rate' else spec.aggregation]
    return Measurement(value, len(values), eligible, missing, applicable,
        sum(values) if spec.aggregation == 'rate' and values else None,
        eligible if spec.aggregation == 'rate' else None)


def compare(value, target, comparison):
    return value <= target if comparison == '<=' else value >= target if comparison == '>=' else value == target


def evaluate_spec(spec, population):
    if spec.enforcement != 'REPORT_ONLY':
        raise ValueError('13E.2 evaluator cannot activate ENFORCED specifications')
    m = measure(spec, population)
    observed = None if m.value is None or spec.target is None else compare(m.value, spec.target, spec.comparison)
    if population.window != spec.window:
        result, reason = 'INSUFFICIENT_DATA', 'WINDOW_MISMATCH'
    elif m.applicable is False:
        result, reason = 'NOT_APPLICABLE', 'NO_APPLICABLE_OBSERVATIONS'
    elif spec.classification == 'HARD_INVARIANT' and observed is False:
        result, reason = 'MISS', 'OBSERVED_INVARIANT_VIOLATION'
    elif m.value is None or m.missing or m.samples < spec.minimum_samples:
        result, reason = 'INSUFFICIENT_DATA', 'MISSING_EVIDENCE_OR_SMALL_SAMPLE'
    elif spec.target is None:
        result, reason = 'NOT_APPLICABLE', 'DESCRIPTIVE_METRIC_NO_NUMERIC_OBJECTIVE'
    else:
        result, reason = ('PASS', 'OBSERVED_TARGET_MET') if observed else ('MISS', 'OBSERVED_TARGET_MISSED')
    return dict(metric=spec.metric, classification=spec.classification, population=population.name,
        result=result, reason=reason, target=spec.target, comparison=spec.comparison,
        measurement=asdict(m), observed_comparison=observed, evidence_strength=spec.evidence_strength,
        status=spec.status, enforcement='REPORT_ONLY',
        alert_bands_exceeded=[k for k, v in spec.alert_bands.items() if m.value is not None and m.value > v],
        alert_action='NONE_REPORT_ONLY')


def evaluate_scorecard(specs, populations):
    checks = [evaluate_spec(s, p) for p in populations for s in specs]
    return dict(mode='REPORT_ONLY', enforced=False, ci_exit_code=0, checks=checks,
        denominators={p.name: denominators(p) for p in populations},
        notes={p.name: p.notes for p in populations},
        counts={status: sum(c['result'] == status for c in checks)
                for status in ('PASS', 'MISS', 'INSUFFICIENT_DATA', 'NOT_APPLICABLE')})


def error_budget(objective, requests, failures=0):
    if not number(objective) or not 0 < objective <= 1:
        raise ValueError('Objective must be in (0, 1]')
    if type(requests) is not int or type(failures) is not int or not 0 <= failures <= requests:
        raise ValueError('Nonnegative request/failure counts required')
    allowance = Decimal(requests) * (1-Decimal(str(objective)))
    whole = int(allowance.to_integral_value(rounding=ROUND_FLOOR))
    fraction = failures/requests if requests else None
    return dict(objective=objective, requests=requests, allowed_failures_exact=float(allowance),
        allowed_whole_failures=whole, observed_failures=failures, remaining_whole_failures=whole-failures,
        exhausted=failures > whole, observed_failure_rate=fraction,
        burn_rate=float(Decimal(failures)/allowance) if allowance else (0.0 if requests and not failures else None),
        scope='MATHEMATICAL_EXAMPLE_NOT_SELECTED_AVAILABILITY_COMMITMENT')
