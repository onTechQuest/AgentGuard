"""Model-attempt boundary; lower retries disabled and application retries opt-in.

The pinned Agents SDK propagates explicit retry.max_retries=0 to its OpenAI
adapter using client.with_options(max_retries=0), and disables its own replay
paths. The default settings-only RunConfig preserves provider selection. An
explicit hosting scope supplies its worker-owned provider without mutating an
SDK global or changing the model, prompt, deadline or retry policy.
"""

from agents import ModelRetrySettings, ModelSettings, Runner
from agents.usage import Usage

from src.agent import telemetry
from src.agent.owned_transport import owned_provider
from src.agent.request_execution import retry_admission
from src.agent.retry_policy import RetryDecision, classify_failure, current_retry_state, error_chain


def model_run_config() -> dict:
    """Fresh per-call configuration; no model, API, timeout or prompt overrides."""
    config = {"model_settings": ModelSettings(retry=ModelRetrySettings(max_retries=0))}
    provider = owned_provider()
    if provider is not None:
        config["model_provider"] = provider
    return config


def run_model(agent, model_input, *, component=None, run=None, **kwargs):
    """Run one logical call, with only explicitly enabled model-attempt retries.

    Injected runners retain their existing, unverified extension contract.
    """
    if run is not None:
        # This extension point has no verified transport configuration. Do not
        # claim that an arbitrary injected runner suppresses lower-layer retries.
        return run(agent, model_input, **kwargs)
    config = model_run_config()
    telemetry.retry_configuration(lower_layer_retries_configured=False)
    state = current_retry_state()
    if state is None or not state.policy.enabled:
        return Runner.run_sync(agent, model_input, run_config=config, **kwargs)
    if component is None:
        raise ValueError("A component is required for controlled model retries")
    number = 1
    known_failed_usage = Usage()
    while True:
        try:
            result = Runner.run_sync(agent, model_input, run_config=config, **kwargs)
        except Exception as error:
            returned = _failure_usage(error)
            if returned is not None:
                telemetry.usage(returned)
                known_failed_usage.add(returned)
            evidence = classify_failure(error, component, wall_clock=state.wall_clock)
            telemetry.attempt_failure(error, evidence)
            decision = state.decide(evidence, number)
            if decision.admitted:
                decision = _budget_decision(state, component, evidence, decision, decision.delay_ms)
            telemetry.retry_decision(decision)
            if not decision.admitted:
                raise
            retry_error = error
        else:
            # Copy this attempt before adding earlier returned usage to the SDK
            # aggregate. Caller observations must not count it twice.
            telemetry.usage(result.context_wrapper.usage)
            telemetry.attempt_success()
            result.context_wrapper.usage.add(known_failed_usage)
            return result

        if decision.delay_ms:
            try:
                state.sleeper(decision.delay_ms / 1000)
            except BaseException:
                telemetry.retry_decision(RetryDecision(True, False, "BACKOFF_INTERRUPTED", decision.delay_ms))
                raise
        after_wait = _budget_decision(state, component, evidence, decision, 0)
        if not after_wait.admitted:
            telemetry.retry_decision(after_wait)
            raise retry_error
        # Consume allowance only when another model attempt is dispatched.
        state.consumed += 1
        telemetry.retry_decision(decision, performed=True)
        telemetry.next_attempt()
        telemetry.retry_configuration(lower_layer_retries_configured=False)
        number += 1


def _budget_decision(state, component, evidence, decision, delay_ms):
    admission = retry_admission(component, delay_ms=delay_ms,
                                minimum_ms=state.policy.minimum_attempt_budget_ms,
                                reserve_ms=state.policy.downstream_reserve_ms)
    if admission.admitted:
        return decision
    if admission.denial_reason in {"CANCELLED", "CANCELLATION_REQUESTED"}:
        reason = admission.denial_reason.value
    else:
        reason = "RETRY_AFTER_EXCEEDS_BUDGET" if evidence.retry_after_ms is not None else "INSUFFICIENT_RETRY_BUDGET"
    return RetryDecision(decision.eligible, False, reason, decision.delay_ms)


def _failure_usage(error):
    """Only public returned SDK usage; do not estimate failed provider consumption."""
    for candidate in error_chain(error):
        data = getattr(candidate, "run_data", None)
        wrapper = getattr(data, "context_wrapper", None)
        usage = getattr(wrapper, "usage", None)
        if isinstance(usage, Usage):
            return usage
        usage = getattr(candidate, "usage", None)
        if isinstance(usage, Usage):
            return usage
    return None
