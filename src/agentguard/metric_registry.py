"""Versioned descriptive measurements, never release or drift thresholds."""
from statistics import fmean

REGISTRY_VERSION = 1


def definition(metric_id, family, path, population, unit="ratio", direction="HIGHER_IS_BETTER"):
    requirements = ["scenario_cohort", "business_fixtures", "aggregation"]
    requirements += {
        "DETERMINISTIC": ["deterministic_evaluator", "tool_contract"],
        "SEMANTIC": ["semantic_evaluator", "evaluator_prompts", "judge_identity_known_equal"],
        "SAFETY": ["safety_evaluator", "safety_policy", "judge_identity_known_equal"],
        "OPERATIONAL": ["protocol", "hosting", "runtime_policy", "libraries", "measurement_definition"],
    }[family]
    return dict(metric_id=metric_id, version=1, family=family, unit=unit, aggregation="mean",
                population=population, required_lineage=requirements, direction=direction, observation_path=path)


METRICS = tuple([
    definition(name, "DETERMINISTIC", "deterministic." + field, "functional")
    for name, field in (("functional_accuracy", "functional_pass"), ("tool_accuracy", "tool_pass"),
                        ("argument_accuracy", "argument_pass"), ("factual_grounding", "factual_grounding_pass"))
] + [definition(name, "SEMANTIC", "semantic." + name + ".score", "functional")
     for name in ("answer_relevancy", "correctness", "faithfulness")]
  + [definition("safety_pass_rate", "SAFETY", "safety.final_pass", "safety")]
  + [definition(name, "OPERATIONAL", "operational." + name, "each_population", unit, direction)
     for name, unit, direction in (
         ("router_latency_ms", "ms", "LOWER_IS_BETTER"), ("recovery_latency_ms", "ms", "LOWER_IS_BETTER"),
         ("required_operation_latency_ms", "ms", "LOWER_IS_BETTER"), ("synthesis_latency_ms", "ms", "LOWER_IS_BETTER"),
         ("total_latency_ms", "ms", "LOWER_IS_BETTER"), ("production_input_tokens", "tokens", "INFORMATIONAL"),
         ("production_output_tokens", "tokens", "INFORMATIONAL"), ("production_tokens", "tokens", "INFORMATIONAL"),
         ("retry_count", "attempts", "ZERO_IS_REQUIRED"))])


def aggregate(observations, *, population_complete):
    """Known-value denominators; null is never silently a zero or failed check."""
    results = []
    for metric in METRICS:
        populations = ("functional", "safety") if metric["population"] == "each_population" else (metric["population"],)
        for population in populations:
            rows = [r for r in observations if r["population"] == population]
            values = []
            for row in rows:
                value = row
                for part in metric["observation_path"].split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if type(value) in (bool, int, float):
                    values.append(value)
            results.append(dict(metric_id=metric["metric_id"], metric_version=metric["version"], population=population,
                aggregation=metric["aggregation"], unit=metric["unit"], value=fmean(values) if values else None,
                sample_count=len(values), denominator=len(values), observed_population_count=len(rows),
                missing_count=len(rows)-len(values), population_complete=population_complete,
                authority="DESCRIPTIVE_ONLY"))
    return results
