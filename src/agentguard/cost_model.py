"""Offline consumption arithmetic. No provider imports, runtime limits or gates."""
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import math


@dataclass(frozen=True)
class Tokens:
    input_tokens: int | float | None = None
    output_tokens: int | float | None = None
    total_tokens: int | float | None = None
    cached_input_tokens: int | float | None = None

    def __post_init__(self):
        for value in asdict(self).values():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("Token counters must be finite, nonnegative numbers or unavailable")
        if self.input_tokens is not None and self.output_tokens is not None and self.total_tokens is not None:
            if not math.isclose(self.input_tokens + self.output_tokens, self.total_tokens):
                raise ValueError("Input/output tokens do not reconcile with total")
        if self.cached_input_tokens is not None and self.input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input exceeds input tokens")


@dataclass(frozen=True)
class ComponentUsage:
    name: str
    calls: int | None
    tokens: Tokens
    model: str | None = None

    def __post_init__(self):
        if self.calls is not None and (type(self.calls) is not int or self.calls < 0):
            raise ValueError("Component calls must be nonnegative integers or unavailable")


@dataclass(frozen=True)
class ProductionObservation:
    identity: str
    source: str
    population: str
    outcome: str
    usage_completeness: str
    tokens: Tokens
    logical_calls: int | None
    provider_dispatches: int | None
    components: tuple[ComponentUsage, ...] = ()
    recovery_count: int | None = None
    model: str | None = None
    scenario_id: str | None = None
    admitted: bool = True
    zero_work_proven: bool = False
    expired_before_service: bool = False
    deadline_after_dispatch: bool = False
    business_pass: bool | None = None

    def __post_init__(self):
        for value in (self.logical_calls, self.provider_dispatches, self.recovery_count):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Call counters must be nonnegative integers or unavailable")
        if self.zero_work_proven and (self.logical_calls != 0 or self.provider_dispatches != 0 or self.tokens.total_tokens != 0):
            raise ValueError("Zero-work evidence contradicts consumption")

    @property
    def archetype(self):
        if not self.admitted:
            return "ADMISSION_REJECTED" if self.zero_work_proven else "UNCLASSIFIED"
        if self.expired_before_service and self.zero_work_proven:
            return "DEADLINE_BEFORE_SERVICE"
        if self.deadline_after_dispatch:
            return "DEADLINE_AFTER_DISPATCH"
        counts = Counter()
        for c in self.components:
            if c.calls is not None:
                counts[c.name] += c.calls
        if self.outcome in {"success", "completed"}:
            if self.logical_calls == 2 and counts == {"router": 1, "synthesis": 1}:
                return "NORMAL_2_CALL"
            if self.logical_calls == 3 and counts == {"router": 1, "recovery": 1, "synthesis": 1}:
                return "RECOVERY_3_CALL"
        if self.outcome not in {"success", "completed"} and self.components and counts.get("synthesis", 0) == 0:
            return "EARLY_FAILURE"
        return "OTHER_OBSERVED"


def statistics(values):
    """Available observations only; linear interpolation, no imputed zeros."""
    values = sorted(v for v in values if v is not None)
    def percentile(p):
        x = (len(values) - 1) * p
        lo, hi = math.floor(x), math.ceil(x)
        return values[lo] + (values[hi] - values[lo]) * (x - lo)
    return dict(count=len(values), sum=sum(values) if values else None,
                mean=sum(values)/len(values) if values else None,
                **{key: percentile(p) if values else None for key, p in (("p50", .5), ("p90", .9), ("p95", .95))},
                max=max(values) if values else None)


def aggregate(observations):
    rows = list(observations)
    complete = [r for r in rows if r.usage_completeness == "COMPLETE"]
    return dict(sample_count=len(rows), usage_completeness=dict(Counter(r.usage_completeness for r in rows)),
                logical_calls=statistics(r.logical_calls for r in rows),
                provider_dispatches=statistics(r.provider_dispatches for r in rows),
                complete_tokens={k: statistics(getattr(r.tokens, k) for r in complete)
                                 for k in ("input_tokens", "output_tokens", "total_tokens")},
                partial_known_tokens={k: statistics(getattr(r.tokens, k) for r in rows if r not in complete)
                                      for k in ("input_tokens", "output_tokens", "total_tokens")})


def component_summary(observations):
    rows = list(observations)
    result = {}
    for name in ("router", "recovery", "synthesis"):
        selected = [c for r in rows if r.usage_completeness == "COMPLETE" for c in r.components if c.name == name and c.calls]
        result[name] = dict(request_observations=len(selected), calls=sum(c.calls for c in selected),
            tokens_per_component_per_request={k: statistics(getattr(c.tokens, k) for c in selected)
                                             for k in ("input_tokens", "output_tokens", "total_tokens")})
    return result


@dataclass(frozen=True)
class Pricing:
    model: str
    effective_date: str
    source: str
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    cached_input_usd_per_million: float | None = None
    verified: bool = False

    def __post_init__(self):
        if not self.model.strip() or not self.source.strip():
            raise ValueError("Pricing needs a model and source/reference")
        date.fromisoformat(self.effective_date)
        if type(self.verified) is not bool:
            raise ValueError("verified must be boolean")
        for value in (self.input_usd_per_million, self.output_usd_per_million, self.cached_input_usd_per_million):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or value < 0):
                raise ValueError("Prices must be nonnegative finite values or unavailable")

    @property
    def mode(self):
        return "CONFIGURED_PRICING" if self.verified and self.input_usd_per_million is not None and self.output_usd_per_million is not None else "TOKEN_ONLY"


def cost(tokens, model, pricing=None, *, complete=True):
    """No estimates from total-only usage; cached discounts need measured cache usage."""
    mode = pricing.mode if pricing else "TOKEN_ONLY"
    reason = ("PRICING_UNAVAILABLE_OR_UNVERIFIED" if mode == "TOKEN_ONLY" else
              "MODEL_MISMATCH_OR_UNKNOWN" if model != pricing.model else
              "INCOMPLETE_USAGE" if not complete else
              "INPUT_OUTPUT_UNAVAILABLE" if tokens.input_tokens is None or tokens.output_tokens is None else
              "CACHED_INPUT_UNAVAILABLE" if pricing.cached_input_usd_per_million is not None and tokens.cached_input_tokens is None else None)
    if reason:
        return dict(mode=mode, usd=None, reason=reason)
    d = lambda v: Decimal(str(v))
    incoming = d(tokens.input_tokens) * d(pricing.input_usd_per_million)
    if pricing.cached_input_usd_per_million is not None:
        incoming += d(tokens.cached_input_tokens) * (d(pricing.cached_input_usd_per_million) - d(pricing.input_usd_per_million))
    return dict(mode=mode, usd=float((incoming + d(tokens.output_tokens)*d(pricing.output_usd_per_million))/Decimal(1000000)), reason=None)


def mean_tokens(rows):
    rows = list(rows)
    if not rows or any(r.usage_completeness != "COMPLETE" for r in rows):
        raise ValueError("Projection requires complete observed usage")
    values = {}
    for key in Tokens.__dataclass_fields__:
        counters = [getattr(r.tokens, key) for r in rows]
        values[key] = sum(counters)/len(counters) if all(v is not None for v in counters) else None
    return Tokens(**values)


def projection(normal, recovery, rate, requests=1, pricing=None):
    """Mixture of observed archetypes, not a forecast or causal recovery estimate."""
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("Recovery rate must be between zero and one")
    if type(requests) is not int or requests < 1:
        raise ValueError("Positive integer request count required")
    normal, recovery = list(normal), list(recovery)
    if any(r.archetype != "NORMAL_2_CALL" for r in normal) or any(r.archetype != "RECOVERY_3_CALL" for r in recovery):
        raise ValueError("Projection needs normal and recovery archetypes")
    n = mean_tokens(normal)
    r = mean_tokens(recovery) if rate else None
    totals = {}
    for key in Tokens.__dataclass_fields__:
        a, b = getattr(n, key), getattr(r, key) if r else None
        totals[key] = (a * requests if rate == 0 and a is not None else
                       b * requests if rate == 1 and b is not None else
                       ((1-rate)*a + rate*b)*requests if a is not None and b is not None else None)
    tokens = Tokens(**totals)
    models = {row.model for row in normal + (recovery if rate else [])}
    model = next(iter(models)) if len(models) == 1 else None
    return dict(kind="MODELED_PROJECTION", requests=requests, recovery_rate=rate,
                logical_model_calls=requests*(2+rate), recovery_calls=requests*rate,
                tokens=asdict(tokens), cost=cost(tokens, model, pricing),
                normal_samples=len(normal), recovery_samples=len(recovery) if rate else 0)


def velocity(requests, calls, tokens, seconds):
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("A positive observed duration is required")
    return dict(requests_per_second=requests/seconds, logical_calls_per_second=calls/seconds,
                tokens_per_second=tokens/seconds if tokens is not None else None, observation_seconds=seconds)


def avoided_work(rejected, admitted):
    rejected, admitted = list(rejected), list(admitted)
    if not rejected or any(r.archetype != "ADMISSION_REJECTED" for r in rejected):
        raise ValueError("Explicit zero-work capacity rejections required")
    tokens = mean_tokens(admitted)
    calls = [r.logical_calls for r in admitted]
    return dict(kind="HYPOTHETICAL_AVOIDED_WORK_NOT_ACTUAL_SAVINGS", rejected=len(rejected),
                observed_rejected_model_calls=0, observed_rejected_tool_calls=0, observed_rejected_tokens=0,
                estimated_model_calls=len(rejected)*sum(calls)/len(calls) if all(c is not None for c in calls) else None,
                estimated_tokens=len(rejected)*tokens.total_tokens if tokens.total_tokens is not None else None)
