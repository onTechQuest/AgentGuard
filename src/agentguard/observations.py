"""Allowlisted per-execution observations. Never executes an evaluator or model."""
from src.agentguard.synthesis_qualification import normalize, number, flag, identity

DETERMINISTIC = ("functional_pass", "tool_pass", "argument_pass", "factual_grounding_pass")
EVIDENCE = ("required_tools_satisfied", "tool_suppression_attempt_overridden", "authoritative_tool_used",
    "factual_grounding_passed", "grounded_result_used", "unauthorized_tool_used", "required_tool_missing",
    "attack_scope_supported", "prohibited_operation_attempted", "execution_completed", "fabricated_behavior_rejected", "override_checks_passed")
LABELS = {"resisted", "partially_followed", "followed_injection", "unavailable"}
CLASSIFICATIONS = {"INSUFFICIENT_EVIDENCE", "DETERMINISTIC_CONFIRMED_FAILURE", "SEMANTIC_CONFIRMED", "DETERMINISTIC_OVERRIDE_RESISTED"}


def blank(run, scenario_id, repetition):
    manifest = run.manifest.to_dict()
    scenario = run.scenario_rows[scenario_id]
    return dict(observation_schema_version=1, **run.reference, scenario_id=scenario_id,
        scenario_fingerprint=scenario["scenario_fingerprint"], population=run.populations[scenario_id],
        repetition=repetition, completed=False, completion_state="INCOMPLETE",
        deterministic={key: None for key in DETERMINISTIC},
        safety=dict(final_pass=None, deterministic_evidence={key: None for key in EVIDENCE},
                    component_results={key: None for key in ("tool_policy_pass", "data_protection_pass", "unsupported_action_pass", "factual_grounding_pass")},
                    semantic_classifier_result=None, composite_verdict=None, composite_classification=None),
        semantic={name: dict(score=None, evaluator_available=False, judge_role=role,
                            judge_identity=manifest["judge_models"].get(role, {"resolved": "UNKNOWN"}))
                  for name, role in (("answer_relevancy", "answer_relevancy"), ("correctness", "correctness"), ("faithfulness", "hallucination"))},
        operational={**{k: None for k in ("request_id", "total_latency_ms", "router_latency_ms", "recovery_latency_ms",
                    "required_operation_latency_ms", "synthesis_latency_ms", "production_input_tokens", "production_output_tokens",
                    "production_tokens", "retry_count", "terminal_category")}, "model_identities": [], "usage_completeness": "UNAVAILABLE"},
        safety_judge_linkage={role: manifest["judge_models"].get(role, {"resolved": "UNKNOWN"}) for role in ("prompt_injection", "action_claim")},
        failure_evidence_reference=None)


def update(run, observation, *, scenario=None, record=None, deterministic=None, semantic=None, safety=None, measurement=None, failure=None):
    if deterministic is not None:
        observation["deterministic"].update({k: flag(getattr(deterministic, k, None)) for k in DETERMINISTIC})
    if semantic is not None:
        for name, source in (("answer_relevancy", "answer_relevancy_score"), ("correctness", "correctness_score"), ("faithfulness", "hallucination_score")):
            value = number(getattr(semantic, source, None))
            observation["semantic"][name].update(score=value, evaluator_available=value is not None)
    if safety is not None:
        def categorical(value, allowed):
            return value if isinstance(value, str) and value in allowed else None
        observation["safety"].update(final_pass=flag(getattr(safety, "passed", None)),
            component_results={key: flag(getattr(safety, key, None)) for key in observation["safety"]["component_results"]},
            semantic_classifier_result=categorical(getattr(safety, "prompt_injection_label", None), LABELS),
            composite_verdict=categorical(getattr(safety, "prompt_injection_verdict", None), LABELS),
            composite_classification=categorical(getattr(safety, "prompt_injection_classification", None), CLASSIFICATIONS),
            deterministic_evidence={k: flag(getattr(getattr(safety, "injection_evidence", None), k, None)) for k in EVIDENCE})
        observation["deterministic"]["factual_grounding_pass"] = flag(getattr(safety, "factual_grounding_pass", None))
    if record is not None or measurement is not None or failure is not None:
        raw = getattr(record, "production_telemetry", None) if record is not None else None
        row = dict(measurement or {})
        if record is not None:
            row.update(production_telemetry=raw, latency_ms=getattr(record, "latency_ms", None),
                       production_usage={k: getattr(record, k, None) for k in ("input_tokens", "output_tokens", "total_tokens")})
        if failure is not None:
            row["failure_evidence"] = failure
        row["scenario_id"] = observation["scenario_id"]
        projected = normalize(row)
        from src.agentguard.safety_incidents import Redactor
        redactor = Redactor([raw, row, scenario or {}])
        models = [dict(model=model, immutable_revision="UNKNOWN") for model in projected["model_identities"] if redactor.clean(model) == model]
        values = dict(
            request_id=projected["request_id"] if redactor.clean(projected["request_id"]) == projected["request_id"] else None,
            total_latency_ms=projected["total_request_ms"], router_latency_ms=projected["component_latency_ms"]["primary_router"],
            recovery_latency_ms=projected["component_latency_ms"]["recovery_planner"],
            required_operation_latency_ms=projected["component_latency_ms"]["required_operations"],
            synthesis_latency_ms=projected["component_latency_ms"]["synthesis"],
            production_input_tokens=projected["production_tokens"]["input_tokens"],
            production_output_tokens=projected["production_tokens"]["output_tokens"], production_tokens=projected["production_tokens"]["total_tokens"],
            retry_count=projected["retry_attempts_total"], terminal_category=projected["failure_category"],
            usage_completeness=projected["usage_completeness"] or "UNAVAILABLE", model_identities=models)
        # Later measurement projections may contain less evidence for this same
        # execution. Missing fields must not erase its earlier known snapshot.
        observation["operational"].update({key: value for key, value in values.items()
            if value is not None and value != [] and value != "UNAVAILABLE"})
        for model in models:
            linked = dict(**model, request_id=observation["operational"]["request_id"], scenario_id=observation["scenario_id"], repetition=observation["repetition"])
            if linked not in run.observed_models:
                run.observed_models.append(linked)
    if failure is not None:
        observation["failure_evidence_reference"] = dict(**run.reference, scenario_id=observation["scenario_id"], repetition=observation["repetition"])
