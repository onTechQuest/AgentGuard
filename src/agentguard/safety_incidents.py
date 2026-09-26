"""Sanitized retention of captured safety evidence; never execute or rescore.

Unknown fields are omitted. Free text is retained after credential/PII redaction;
this is diagnostic evidence, not a lossless copy of a sensitive response.
"""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock
from uuid import UUID, uuid4

from pydantic import BaseModel

from src.agent.data_policy import project_tool_result
from src.agent.domain_entities import extract_entities
from src.agent.execution_plan import ExecutionTrace
from src.agent.planning_completeness import PlanningResult
from src.agent.telemetry import snapshot
from src.agentguard.reliability import git_commit, number, sanitize_measurement
from src.agentguard.tool_policy import CAPABILITIES, TOOL_REGISTRY


def keys(text):
    return dict.fromkeys(text.split())


USAGE = keys("requests sdk_visible_requests input_tokens output_tokens total_tokens")
BINDING = keys("capability order_id needs_clarification")
PLAN = {**keys("ambiguity confidence entity_scope"), "business_capabilities": [None],
        "control_signals": [None], "extracted_entities": {"order_ids": [None]},
        "capability_requests": [BINDING], "denied_disclosures": [keys("kind target")]}
PLANNING = {**keys("completeness_review_triggered completeness_reason recovery_attempted plan_source recovery_count recovery_error"),
            "primary_plan": PLAN, "final_plan": PLAN, "recovery_plan": {"capability_requests": [BINDING]},
            "recovery_usage": USAGE,
            "evidence": {"extracted_order_ids": [None], "control_signals": [None],
                         "supported_primary_capabilities": [None], "registered_capability_references": [None],
                         "registered_tool_references": [{"name": None, "authoritative_for": [None]}]}}
GRANT = {**keys("tool order_id"), "capabilities": [None]}
OPERATION = {**keys("tool mode"), "capabilities": [None], "arguments": {"order_id": None}}
INJECTION = {**keys("required_tools_satisfied tool_suppression_attempt_overridden authoritative_tool_used factual_grounding_passed grounded_result_used unauthorized_tool_used required_tool_missing"),
             "authoritative_sources": [[None]], "grounded_sources": [[None]]}
SCORE = {**keys("scenario_id passed prompt_injection_pass unsupported_action_pass data_protection_pass tool_policy_pass prompt_injection_label prompt_injection_reason factual_grounding_pass legacy_forbidden_pass unsupported_action_reason prompt_injection_verdict prompt_injection_disagreement prompt_injection_diagnostic"),
         "failures": [None], "injection_evidence": INJECTION,
         "action_claims": [keys("action actor state confidence reason")],
         "evaluation_usage": [{**USAGE, **keys("component http_retry_count")}]}
POLICY = {**keys("name request_deadline_ms router_allowance_ms recovery_reserve_ms synthesis_allowance_ms"),
          "model_retry_policy": {**keys("enabled max_attempts shared_extra_attempts_per_request maximum_retry_delay_ms minimum_attempt_budget_ms downstream_reserve_ms retry_after_policy"),
                                 "eligible_failure_categories": [None], "transient_http_statuses": [None],
                                 "backoff": keys("initial_delay_ms multiplier jitter")},
          "lower_layer_retry_policy": keys("agents_sdk_max_retries openai_client_max_retries scope")}
SCENARIO = {**keys("id category tier risk test_intent input expected_behavior expected_injection_label legacy_compatibility"),
            **{k: [None] for k in ("coverage_tags", "required_tools", "allowed_tools", "prohibited_actions", "allowed_fields")}}
ROOT_TELEMETRY = keys("request_id started_at_utc total_latency_ms terminal_status business_outcome terminal_failure_component terminal_failure_category terminal_exception_type deadline_budget_ms deadline_exhausted cancellation_requested cancellation_observed cancellation_evidence result_abandoned remote_outcome_unknown retry_policy_enabled retry_allowance_initial retry_allowance_consumed retry_allowance_remaining retry_attempts_total retry_exhausted")


def mapping(value):
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in fields(value)}
    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in type(value).model_fields}
    return {}


def project(value, schema):
    if value is None:
        return None
    if isinstance(schema, dict):
        source = mapping(value)
        return {k: project(source[k], child) for k, child in schema.items() if k in source}
    if isinstance(schema, list):
        return [project(v, schema[0]) for v in value] if isinstance(value, (list, tuple)) else None
    return value if isinstance(value, (str, bool, int)) or type(value) is float and isfinite(value) else None


_PRIVATE_KEY = re.compile(r"api.?key|secret|password|authorization|cookie|headers?|provider.?bod|raw.?bod|raw.?response|environment|customer|email|phone|address|payment|card|ssn|^(?:access_token|refresh_token|token|credentials?|env|body)$", re.I)
_TEXT_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)"),
    re.compile(r"\bCUST-[A-Za-z0-9_-]+\b", re.I),
    re.compile(r'''(?i)["']?(?:[a-z0-9_]*(?:api_key|password|secret|access_token|refresh_token)|authorization)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)'''),
    re.compile(r"(?im)\b(?:email|phone|customer(?:[ _]id)?|address|shipping address|billing address)\s*[:=]\s*[^\n;]+"),
)


class Redactor:
    def __init__(self, sources):
        self.values = set()
        self.redactions = 0
        for source in sources:
            self.collect(source)

    def collect(self, value, private=False):
        if isinstance(value, dict):
            for key, child in value.items():
                self.collect(child, private or bool(_PRIVATE_KEY.search(str(key))))
        elif isinstance(value, (list, tuple)):
            for child in value:
                self.collect(child, private)
        elif private and isinstance(value, str) and len(value) >= 3:
            self.values.add(value)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                self.collect(json.loads(value))
            except ValueError:
                pass

    def clean(self, value):
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(self.values, key=len, reverse=True):
            self.redactions += value.count(secret)
            value = value.replace(secret, "[REDACTED]")
        for pattern in _TEXT_PATTERNS:
            value, count = pattern.subn("[REDACTED]", value)
            self.redactions += count
        return value


def arguments(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"unavailable": "unstructured_arguments_omitted"}
    if isinstance(value, (list, tuple)):
        try:
            value = dict(value)
        except (ValueError, TypeError):
            return {"unavailable": "invalid_arguments_omitted"}
    return project(value, {"order_id": None})


def facts(value, tool, targets, redactor):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"unavailable": "unstructured_output_omitted"}
    if not isinstance(value, dict):
        return None
    # The registry's existing disclosure contract is the ceiling, even if an
    # injected/malformed record contains additional private fields.
    capabilities = next((tuple(t.supports & CAPABILITIES.keys()) for t in TOOL_REGISTRY if t.name == tool), ())
    order = value.get("order", value)
    if isinstance(order, dict) and order.get("order_id") is not None and str(order["order_id"]).upper() not in targets:
        redactor.collect(value, private=True)
        return {"unavailable": "unrelated_record_omitted"}
    return project_tool_result(value, capabilities) if capabilities else {"unavailable": "unknown_tool_output_omitted"}


def execution_snapshot(raw, targets, redactor):
    if not isinstance(raw, dict):
        return None
    result = project(raw, {key: [None] for key in ("required_operations", "completed_required_operations",
                     "missing_required_operations", "optional_operations")})
    plan = raw.get("plan", {})
    result["plan"] = project(plan, {"authorized_bindings": [GRANT], "prohibited_actions": [None]})
    result["plan"]["operations"] = []
    for op in plan.get("operations", []):
        result["plan"]["operations"].append({**project(op, OPERATION), "arguments": arguments(op.get("arguments"))})
    result["operations"] = []
    for item in raw.get("operations", []):
        op = item.get("operation", {})
        result["operations"].append({**project(item, keys("call_id status invoked error error_type latency_ms")),
            "operation": {**project(op, OPERATION), "arguments": arguments(op.get("arguments"))},
            "output": facts(item.get("output"), op.get("tool"), targets, redactor)})
    result["prohibited_operation_attempts"] = [
        {**project(a, keys("name reason")), "arguments": arguments(a.get("arguments"))}
        for a in raw.get("prohibited_operation_attempts", [])]
    return result


class SafetyIncidentRecorder:
    def __init__(self, project_root, *, retain_all=False):
        self.root = Path(project_root).resolve()
        self.run_id = uuid4().hex
        self.retain_all = retain_all
        self._metadata = None
        self._metadata_lock = Lock()

    def _directory(self):
        # Validate before creating anything; IDs never supply path components.
        if UUID(self.run_id).hex != self.run_id:
            raise ValueError("Incident run ID must be a canonical UUID hex string")
        parent = self.root
        for name in ("reports", "safety_incidents", self.run_id):
            child = parent / name
            child.mkdir(parents=True, exist_ok=True)
            resolved = child.resolve(strict=True)
            # Compare existing filesystem identities. Non-strict resolution of
            # missing paths can change Windows namespace spelling while another
            # writer creates an ancestor (C:\\... versus \\\\?\\C:\\...).
            if not resolved.parent.samefile(parent) or Path(resolved.name) != Path(name):
                raise ValueError("Incident destination escapes reports")
            parent = child
        return parent

    def _get_metadata(self):
        # Only initialization on this recorder is serialized. Other recorders,
        # requests, redactors and artifact writers remain independent.
        with self._metadata_lock:
            if self._metadata is None:
                versions = {}
                for package in ("openai-agents", "openai", "deepeval"):
                    try:
                        versions[package] = version(package)
                    except PackageNotFoundError:
                        versions[package] = None
                self._metadata = {"git_commit": git_commit(self.root), "sdk_versions": versions}
            return self._metadata

    def retain(self, scenario, record=None, score=None, *, error=None, stage=None):
        """Retain selected evidence; skip ordinary passes, raise on write failure."""
        disagreement = score is not None and (score.prompt_injection_disagreement or
            score.prompt_injection_label is not None and score.prompt_injection_verdict is not None
            and score.prompt_injection_label != score.prompt_injection_verdict)
        execution_error = record is not None and record.execution_error is not None
        if not (self.retain_all or error is not None or execution_error or score is not None and (not score.passed or disagreement)):
            return None
        try:
            artifact = self._build(scenario, record, score, error, stage, disagreement)
            directory = self._directory()
            # UUID filenames prevent collisions and path traversal; the exact
            # sanitized scenario ID is inside the record, not used as a path.
            path = directory / (uuid4().hex + ".json")
            payload = json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            temporary = None
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                        prefix=".incident-", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                # Same-filesystem, atomic, exclusive publication: readers never
                # see partial JSON, and UUID collisions cannot overwrite data.
                os.link(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            print(f"Safety diagnostic: {path.relative_to(self.root).as_posix()}")
            return path
        except Exception as failure:
            # No raw error message/provider response or credentials in logs.
            print(f"Safety diagnostic retention unavailable ({type(failure).__name__}).")
            raise

    def _build(self, scenario, record, score, error, stage, disagreement):
        raw = mapping(record)
        observed = raw.get("production_telemetry")
        if observed is None and error is not None:
            observed = snapshot(getattr(error, "production_telemetry", None))
        observed = observed or {}
        planning, execution = raw.get("planning"), raw.get("execution")
        if error is not None:
            if planning is None and isinstance(getattr(error, "planning", None), PlanningResult):
                planning = error.planning.snapshot()
            if execution is None and isinstance(getattr(error, "trace", None), ExecutionTrace):
                execution = error.trace.snapshot()
                if planning is None and isinstance(getattr(error.trace, "planning", None), PlanningResult):
                    planning = error.trace.planning.snapshot()
        redactor = Redactor([scenario, raw, observed, planning, execution, mapping(score)])
        targets = set(extract_entities(scenario.get("input", "")).order_ids)
        calls = [{**project(c, keys("name call_id")), "arguments": arguments(c.get("arguments"))}
                 for c in raw.get("tool_calls", [])]
        outputs = [{**project(o, keys("name call_id")), "output": facts(o.get("output"), o.get("name"), targets, redactor)}
                   for o in raw.get("tool_outputs", [])]
        execution = execution_snapshot(execution, targets, redactor)
        if record is None:
            calls = ([{"name": item["operation"].get("tool"), "call_id": item.get("call_id"),
                       "arguments": item["operation"].get("arguments")}
                      for item in execution["operations"] if item.get("invoked")] if execution else None)
            outputs = ([{"name": item["operation"].get("tool"), "call_id": item.get("call_id"), "output": item["output"]}
                        for item in execution["operations"] if item.get("status") == "completed"] if execution else None)
        metadata = self._get_metadata()
        policy = project(observed.get("effective_runtime_policy"), POLICY)
        measurement = sanitize_measurement(observed, status=project(observed.get("terminal_status"), None),
                                           elapsed=number(raw.get("latency_ms"))) if observed else None
        result = {
            "schema_version": 1, "run_id": self.run_id, "request_id": project(observed.get("request_id"), None),
            "scenario_id": project(scenario.get("id"), None), "timestamp": datetime.now(timezone.utc).isoformat(),
            **metadata,
            "retention_reasons": [reason for condition, reason in (
                (error is not None, "exception"), (raw.get("execution_error") is not None, "execution_failure"),
                (score is not None and not score.passed, "safety_failure"), (disagreement, "classifier_disagreement"),
                (self.retain_all, "explicit_diagnostic")) if condition],
            "scenario": project(scenario, SCENARIO),
            "planning": project(planning, PLANNING), "execution": execution,
            "request_policy": {"source": "captured_execution_plan_and_telemetry",
                               "authorized_bindings": execution.get("plan", {}).get("authorized_bindings") if execution else None,
                               "business_outcome": project(observed.get("business_outcome"), None)},
            "tool_calls": calls, "projected_authoritative_facts": outputs,
            "final_response": project(raw.get("final_output"), None),
            "models": [{**project(s, keys("component configured_model resolved_model"))}
                       for s in observed.get("component_spans", []) if s.get("logical_model_calls")],
            "runtime_reliability_policy": policy,
            "production_telemetry": {**project(observed, ROOT_TELEMETRY), "measurements": measurement} if observed else None,
            "safety_score": project(score, SCORE),
            "exception": {"type": type(error).__name__, "stage": stage} if error is not None else None,
            "availability": {"evaluation_record": record is not None, "safety_score": score is not None,
                             "planning": planning is not None, "execution": execution is not None,
                             "production_telemetry": bool(observed)},
        }
        expected = scenario.get("expected_authoritative_facts")
        if expected is not None:
            expectations = expected if isinstance(expected, list) else [expected]
            result["scenario"]["expected_authoritative_facts"] = [
                {"tool": project(item.get("tool"), None),
                 "facts": facts(item, item.get("tool"), targets, redactor)}
                for item in expectations if isinstance(item, dict)]
        result = redactor.clean(result)
        result["sanitization"] = {"version": 1, "redaction_count": redactor.redactions,
                                  "unknown_fields_omitted": True, "unstructured_tool_payloads_omitted": True}
        return result
