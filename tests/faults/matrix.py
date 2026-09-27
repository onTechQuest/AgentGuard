"""Independent expected classifications for the offline fault contracts."""

from dataclasses import dataclass

from .harness import Fault, Injection, Stage


@dataclass(frozen=True)
class Case:
    name: str
    injection: Injection
    category: str
    escaped_type: str
    sanitized_type: str | None
    component: str | None = None

    @property
    def failure_component(self):
        return self.component or self.injection.stage.value


CASES = (
    Case("router_timeout", Injection(Stage.ROUTER, Fault.TIMEOUT), "TIMEOUT", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("router_rate_limit", Injection(Stage.ROUTER, Fault.RATE_LIMIT), "RATE_LIMIT", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("router_network", Injection(Stage.ROUTER, Fault.NETWORK), "NETWORK_ERROR", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("router_provider", Injection(Stage.ROUTER, Fault.PROVIDER), "PROVIDER_ERROR", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("router_invalid_output", Injection(Stage.ROUTER, Fault.MALFORMED), "INVALID_MODEL_OUTPUT", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("recovery_timeout", Injection(Stage.RECOVERY, Fault.TIMEOUT), "TIMEOUT", "PlanningCompletenessError", "APITimeoutError"),
    Case("recovery_provider", Injection(Stage.RECOVERY, Fault.PROVIDER), "PROVIDER_ERROR", "PlanningCompletenessError", "InternalServerError"),
    Case("recovery_invalid_output", Injection(Stage.RECOVERY, Fault.MALFORMED), "INVALID_MODEL_OUTPUT", "PlanningCompletenessError", "ValidationError"),
    Case("policy_exception", Injection(Stage.POLICY, Fault.EXCEPTION), "UNKNOWN_INTERNAL_FAILURE", "RuntimeError", "RuntimeError"),
    Case("plan_exception", Injection(Stage.PLAN, Fault.EXCEPTION), "UNKNOWN_INTERNAL_FAILURE", "RuntimeError", "RuntimeError"),
    Case("tool_missing", Injection(Stage.TOOL, Fault.MISSING), "TOOL_NOT_FOUND", "ExecutionFailure", None),
    Case("tool_exception", Injection(Stage.TOOL, Fault.EXCEPTION), "TOOL_ERROR", "ExecutionFailure", "RuntimeError"),
    Case("tool_timeout", Injection(Stage.TOOL, Fault.TIMEOUT), "TIMEOUT", "ExecutionFailure", "TimeoutError"),
    Case("tool_malformed_projection", Injection(Stage.TOOL, Fault.MALFORMED), "PROJECTION_ERROR", "ExecutionFailure", "AttributeError", "projection"),
    Case("required_operation_pending", Injection(Stage.CONTRACT, Fault.INCOMPLETE), "REQUIRED_OPERATION_INCOMPLETE", "ExecutionFailure", "ExecutionFailure"),
    Case("prohibited_operation", Injection(Stage.CONTRACT, Fault.PROHIBITED), "PROHIBITED_OPERATION_ATTEMPT", "ExecutionFailure", "ExecutionFailure"),
    Case("synthesis_timeout", Injection(Stage.SYNTHESIS, Fault.TIMEOUT), "TIMEOUT", "APITimeoutError", "APITimeoutError"),
    Case("synthesis_rate_limit", Injection(Stage.SYNTHESIS, Fault.RATE_LIMIT), "RATE_LIMIT", "RateLimitError", "RateLimitError"),
    Case("synthesis_network", Injection(Stage.SYNTHESIS, Fault.NETWORK), "NETWORK_ERROR", "APIConnectionError", "APIConnectionError"),
    Case("synthesis_provider", Injection(Stage.SYNTHESIS, Fault.PROVIDER), "PROVIDER_ERROR", "InternalServerError", "InternalServerError"),
    Case("synthesis_protocol", Injection(Stage.SYNTHESIS, Fault.PROTOCOL), "MODEL_PROTOCOL_ERROR", "MaxTurnsExceeded", "MaxTurnsExceeded"),
    Case("synthesis_malformed_behavior", Injection(Stage.SYNTHESIS, Fault.MALFORMED), "INVALID_MODEL_OUTPUT", "ModelBehaviorError", "ModelBehaviorError"),
    Case("synthesis_prohibited_call", Injection(Stage.SYNTHESIS, Fault.PROHIBITED), "PROHIBITED_OPERATION_ATTEMPT", "ExecutionFailure", "ExecutionFailure"),
    Case("invalid_model_configuration", Injection(Stage.ROUTER, Fault.CONFIGURATION), "CONFIGURATION_ERROR", "CapabilityRoutingError", "CapabilityRoutingError"),
    Case("recovery_then_tool_failure", Injection(Stage.TOOL, Fault.EXCEPTION, after_recovery=True), "TOOL_ERROR", "ExecutionFailure", "RuntimeError"),
    Case("recovery_then_synthesis_timeout", Injection(Stage.SYNTHESIS, Fault.TIMEOUT, after_recovery=True), "TIMEOUT", "APITimeoutError", "APITimeoutError"),
    Case("synthesis_protocol_with_usage", Injection(Stage.SYNTHESIS, Fault.PROTOCOL, failure_usage=True, sdk_requests=3), "MODEL_PROTOCOL_ERROR", "MaxTurnsExceeded", "MaxTurnsExceeded"),
    Case("router_protocol_with_usage", Injection(Stage.ROUTER, Fault.PROTOCOL, failure_usage=True, sdk_requests=2), "MODEL_PROTOCOL_ERROR", "CapabilityRoutingError", "CapabilityRoutingError"),
)
