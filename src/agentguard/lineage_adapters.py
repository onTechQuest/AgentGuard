"""Allowlisted local identity adapters. No evaluator initialization or network I/O."""
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from importlib import metadata
from pathlib import Path
import platform
import re
from uuid import uuid4

from src.agentguard.lineage import (UNKNOWN, ABSENT, DISABLED, EvaluationRunManifest,
    dataset_identity, fingerprint, read_document, scenario_index, source_identity)

PACKAGES = ("openai", "openai-agents", "deepeval", "pydantic", "PyYAML", "httpx", "httpcore", "httpx2", "httpcore2", "anyio")
JUDGES = ("answer_relevancy", "correctness", "hallucination", "prompt_injection", "action_claim")
CONFIGURATION_OPTIONS = frozenset({"qualify", "qualification", "execute_retries", "enforce_candidate_budget"})
PRIVATE_CONFIGURATION_KEY = re.compile(r"api.?key|secret|credential|password|headers?|endpoint|authorization|account|customer|^env(?:ironment)?$", re.I)


def approved_configuration(options):
    """Restrict caller options before hashing; never hash a credential value."""
    def project(value):
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items() if not PRIVATE_CONFIGURATION_KEY.search(k)}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value
    return project({k: v for k, v in (options or {}).items() if k in CONFIGURATION_OPTIONS})


SOURCE_GROUPS = {
    "measurement_definition": ("src/agent/telemetry.py", "src/agentguard/evaluation_record.py",
        "src/agentguard/observations.py", "src/agentguard/metric_registry.py",
        "src/agentguard/synthesis_qualification.py", "src/agentguard/reliability.py"),
    "prompt_bundle": ("src/agent/capability_router.py", "src/agent/planning_completeness.py", "src/agent/support_agent.py"),
    "evaluator_prompts": ("src/agentguard/semantic_evaluator.py", "src/agentguard/action_claims.py", "src/agentguard/safety_evaluator.py"),
    "tool_contract": ("src/agentguard/tool_policy.py", "src/agent/request_policy.py", "src/agent/data_policy.py", "src/agent/execution_plan.py"),
    "runtime_policy": ("src/agent/runtime_reliability.py", "src/agent/retry_policy.py", "src/agent/request_budget.py",
                       "src/agent/request_execution.py", "src/agent/model_execution.py", "src/agent/owned_transport.py"),
    "safety_policy": ("src/agent/data_policy.py", "src/agent/request_policy.py", "src/agentguard/tool_policy.py",
                      "src/agentguard/safety_evaluator.py", "src/agentguard/action_claims.py",
                      "src/agentguard/grounding_normalization.py", "src/agentguard/injection_adjudication.py"),
    "deterministic_evaluator": ("src/agentguard/scoring.py", "src/agentguard/behavior_predicates.py"),
    "semantic_evaluator": ("src/agentguard/semantic_evaluator.py",),
    "safety_evaluator": ("src/agentguard/safety_evaluator.py", "src/agentguard/action_claims.py",
                         "src/agentguard/injection_adjudication.py", "src/agentguard/grounding_normalization.py"),
    "evaluation_config": ("src/agentguard/scorecard.py", "src/agentguard/performance.py", "src/agentguard/quality_gate.py"),
}


def plain(value):
    if isinstance(value, Enum):
        return plain(value.value)
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict) or hasattr(value, "items"):
        return {(k.value if isinstance(k, Enum) else str(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def library_versions():
    versions = {"python": platform.python_version()}
    for package in PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = UNKNOWN
    return versions


def safe_model_name(value):
    # Do not serialize clients, endpoints, account identifiers or arbitrary reprs.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) else UNKNOWN


def model_identity(configured):
    resolved, provenance = UNKNOWN, UNKNOWN
    if isinstance(configured, str):
        resolved, provenance = safe_model_name(configured), "configured_name"
    elif configured is None:
        try:
            from agents.models.default_models import get_default_model
            resolved, provenance = safe_model_name(get_default_model()), "agents_sdk_default_resolution"
        except Exception:
            pass
    return dict(configured=ABSENT if configured is None else safe_model_name(configured), resolved=resolved,
                provider="openai" if configured is None or isinstance(configured, str) else UNKNOWN,
                resolution_provenance=provenance, immutable_revision=UNKNOWN)


def runtime_contracts():
    """Observe instantiated local contracts without executing any runner/judge."""
    from agents.agent_output import AgentOutputSchema
    from src.agent.support_agent import support_agent, BUSINESS_TOOLS
    from src.agent.capability_router import SemanticCapabilityRouter
    from src.agent.planning_completeness import SemanticRecoveryPlanner, ActionabilityRecoveryPlan
    from src.agentguard.tool_policy import CAPABILITIES, TOOL_REGISTRY, ACTION_REGISTRY
    from src.agent.runtime_reliability import default_runtime_policy
    from src.agent.tools.orders import RETURN_EVALUATION_DATE, RETURN_WINDOW_DAYS
    router = SemanticCapabilityRouter(model=support_agent.model).agent
    recovery = SemanticRecoveryPlanner(model=support_agent.model)
    def agent_contract(agent):
        return dict(instructions=agent.instructions, output_schema=AgentOutputSchema(agent.output_type).json_schema()
                    if agent.output_type else ABSENT)
    prompts = dict(router=agent_contract(router), omitted_work_recovery=agent_contract(recovery.agent),
                   actionability_recovery=dict(instructions=recovery._actionability_instructions,
                       output_schema=AgentOutputSchema(ActionabilityRecoveryPlan).json_schema()),
                   synthesis=agent_contract(support_agent))
    # Conditional fragments/rendering logic are covered by support_agent.py's
    # conservative source identity, avoiding duplicated behavioral constants.
    tools = dict(tools=[dict(name=t.name, description=t.description, arguments=t.params_json_schema)
                       for _, t in sorted(BUSINESS_TOOLS.items())],
                 capabilities=plain(CAPABILITIES), registry=plain(TOOL_REGISTRY), actions=plain(ACTION_REGISTRY))
    models = {role: model_identity(support_agent.model) for role in ("router", "recovery", "synthesis")}
    settings = {k: v for k, v in plain(support_agent.model_settings).items()
                if k not in {"metadata", "extra_query", "extra_body", "extra_headers", "extra_args"}}
    return dict(prompts=prompts, tools=tools, models=models, model_settings=settings,
                runtime_policy=default_runtime_policy().snapshot(),
                business_constants=dict(evaluation_date=RETURN_EVALUATION_DATE.isoformat(), return_window_days=RETURN_WINDOW_DAYS))


def build_manifest(root, *, suite, functional, safety, execution_mode="live", evaluation_config=None,
                   hosting=None, repetitions=1, effective_runtime_policy=None, execution_profile=None):
    root = Path(root)
    evaluation_config = approved_configuration(evaluation_config)
    unavailable = []
    def sources(names):
        result = {}
        for name in names:
            path = root / name
            if path.exists():
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[name] = UNKNOWN
                unavailable.append(name)
        return result
    def document(name):
        path = root / name
        if path.exists():
            return read_document(path)
        unavailable.append(name)
        return UNKNOWN
    libraries = library_versions()
    unavailable.extend("libraries." + k for k, v in libraries.items() if v == UNKNOWN)
    try:
        observed = runtime_contracts()
    except Exception:
        observed = {k: UNKNOWN for k in ("prompts", "tools", "models", "model_settings", "runtime_policy", "business_constants")}
        unavailable.append("runtime_contracts")
    bundles = {kind: {"sources": sources(names)} for kind, names in SOURCE_GROUPS.items()}
    bundles["prompt_bundle"]["effective_templates"] = observed["prompts"]
    bundles["production_model_settings"] = observed["model_settings"]
    bundles["tool_contract"]["effective_contract"] = observed["tools"]
    bundles["runtime_policy"]["effective_policy"] = effective_runtime_policy if effective_runtime_policy is not None else observed["runtime_policy"]
    bundles["evaluator_prompts"]["libraries"] = {k: libraries[k] for k in ("deepeval", "openai-agents", "pydantic")}
    bundles["semantic_evaluator"]["deepeval"] = libraries["deepeval"]
    bundles["safety_evaluator"]["libraries"] = bundles["evaluator_prompts"]["libraries"]
    bundles["evaluation_config"]["effective"] = dict(suite=suite, repetitions=repetitions,
        sampling="sequential_dataset_order", options=evaluation_config or {})
    bundles["aggregation"] = {"sources": sources(("src/agentguard/scorecard.py", "src/agentguard/performance.py"))}
    bundles["release_gates"] = {name: document("config/" + name) for name in ("quality-gates.yaml", "release-gates.yaml")}
    bundles["slo_spec"] = document("config/slo-report-only.yaml")
    bundles["business_fixtures"] = dict(orders=document("data/orders.json"), constants=observed["business_constants"],
                                        implementation=sources(("src/agent/tools/orders.py",)))
    datasets = {}
    for kind, selected in (("functional", functional), ("safety", safety)):
        full = document(f"evals/datasets/{kind}.json")
        datasets[kind] = dict(full=dataset_identity(kind, full) if isinstance(full, list) else UNKNOWN,
                             selected=dataset_identity(kind, selected), schema_version=1)
    rows = scenario_index([*functional, *safety])
    host = hosting or dict(mode="sequential", max_workers=DISABLED, queue_capacity=DISABLED)
    host = {k: host.get(k, UNKNOWN) for k in ("mode", "max_workers", "queue_capacity")}
    host["configuration_fingerprint"] = fingerprint("hosting", host)
    source = source_identity(root)
    unavailable.extend("source." + k for k, v in source.items() if v == UNKNOWN)
    judges_active = suite in {"smoke", "full"}
    # DeepEval construction may initialize clients/defaults. Do not do that just
    # for provenance. Unknown is explicit, never an equivalence assertion.
    judges = {role: dict(resolved=UNKNOWN if judges_active else DISABLED, provider=UNKNOWN if judges_active else DISABLED,
                         resolution_provenance=UNKNOWN if judges_active else "not_used") for role in JUDGES}
    if judges_active:
        unavailable.extend("judge_models." + role for role in JUDGES)
    if isinstance(observed["models"], dict):
        unavailable.extend("production_models." + role + ".immutable_revision" for role in observed["models"])
    manifest = EvaluationRunManifest.create(dict(manifest_schema_version=1, canonicalization_version=1,
        run_id=uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(), suite=suite, execution_mode=execution_mode,
        source=source, production_models=observed["models"], judge_models=judges, datasets=datasets,
        protocol=dict(protocol_version=1, suite=suite, repetitions=repetitions,
            **({"execution_profile": execution_profile} if execution_profile is not None else {}),
            execution_ordering="sequential_dataset_order" if host["mode"] == "sequential" else "bounded_dispatch_order",
            execution_mode=execution_mode, hosting_mode=host["mode"],
            scenario_populations={**{s["id"]: "functional" for s in functional}, **{s["id"]: "safety" for s in safety}}),
        scenario_count=len(rows), planned_execution_count=len(rows) * repetitions,
        scenario_set_fingerprint=fingerprint("scenario-set", sorted(rows, key=lambda r: r["scenario_id"])),
        execution_order_fingerprint=fingerprint("execution-order", [r["scenario_id"] for r in rows] * repetitions),
        fingerprints={k: fingerprint(k, plain(v)) for k, v in bundles.items()}, libraries=libraries, hosting=host,
        lineage_status="PARTIAL_LINEAGE" if unavailable else "FULL_LINEAGE", unavailable_fields=sorted(set(unavailable))))
    return manifest, rows
