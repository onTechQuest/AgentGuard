"""One SDK invocation with lower-layer retries disabled, no application retries.

The pinned Agents SDK propagates explicit retry.max_retries=0 to its OpenAI
adapter using client.with_options(max_retries=0), and disables its own replay
paths. A settings-only RunConfig input preserves model/provider selection and
the Runner-owned provider lifecycle. No client or SDK global is mutated here.
"""

from agents import ModelRetrySettings, ModelSettings, Runner

from src.agent import telemetry


def model_run_config() -> dict:
    """Fresh per-call configuration; no model, API, timeout or prompt overrides."""
    return {"model_settings": ModelSettings(retry=ModelRetrySettings(max_retries=0))}


def run_model(agent, model_input, *, run=None, **kwargs):
    """Run once. Explicit injected runners retain their existing test contract."""
    if run is not None:
        # This extension point has no verified transport configuration. Do not
        # claim that an arbitrary injected runner suppresses lower-layer retries.
        return run(agent, model_input, **kwargs)
    config = model_run_config()
    telemetry.retry_configuration(lower_layer_retries_configured=False)
    return Runner.run_sync(agent, model_input, run_config=config, **kwargs)
