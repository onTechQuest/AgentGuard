"""Evaluation-only counters. These never enter production usage totals."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationUsage:
    component: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    sdk_visible_requests: int | None = None
    http_retry_count: int | None = None


def capture(component, source, *, sdk=False):
    """Counters only; a broken observer must not change an evaluation result."""
    try:
        def counter(key):
            value = getattr(source, key, None)
            return value if type(value) is int and value >= 0 else None
        incoming, outgoing = counter("input_tokens"), counter("output_tokens")
        return EvaluationUsage(component, incoming, outgoing,
                               incoming + outgoing if incoming is not None and outgoing is not None else None,
                               counter("requests") if sdk else None)
    except Exception:
        return EvaluationUsage(component)


def attach_sdk(target, result):
    try:
        target.evaluation_usage = capture("unsupported_action", result.context_wrapper.usage, sdk=True)
    except Exception:
        pass


def reset_sdk(target):
    try:
        target.evaluation_usage = None
    except Exception:
        pass


def append_usage(rows, component, source):
    try:
        rows.append(capture(component, source))
    except Exception:
        pass


def append_classifier_usage(rows, classifier):
    try:
        observed = getattr(classifier, "evaluation_usage", None)
        if isinstance(observed, EvaluationUsage):
            rows.append(observed)
    except Exception:
        pass
