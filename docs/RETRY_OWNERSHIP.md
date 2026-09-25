# Milestone 13C.3A: single retry ownership and transport verification

AgentGuard explicitly disables lower-layer model retries. Default requests still
make one SDK invocation per logical model call and surface transient failures
immediately, for both finite-budget and unlimited requests. Milestone 13C.3B adds
an explicit, disabled-by-default [AgentGuard retry policy](MODEL_RETRY_POLICY.md).
This document records the lower-layer suppression and transport proof established
in 13C.3A; that suppression remains active on every controlled retry attempt.

## Supported configuration and scope

`src/agent/model_execution.py` supplies a fresh settings-only RunConfig input:

```python
{"model_settings": ModelSettings(retry=ModelRetrySettings(max_retries=0))}
```

Routing, completeness recovery, and synthesis call its shared `run_model()`.
This uses public APIs in the pinned `openai-agents==0.22.2` and
`openai==3.11.0`. No library is patched, no global client/settings are mutated,
and no separate application-owned client is necessary. The dict input omits
`model_provider`, preserving the Runner-owned default-provider cleanup behavior.

The override sets only retry configuration. It leaves the agent's model,
SDK/environment model resolution, reasoning settings, API/transport selection,
prompts, output schemas, tools, and `max_turns=1` unchanged. In particular,
AgentGuard does not hard-code a model in order to configure retry ownership.
Explicit injected router/recovery runners keep their existing extension contract;
their transport configuration is not asserted or reported as verified.

## Audited retry layers

The installed source establishes these mechanisms:

| Layer | Before 13C.3A | Current configuration |
| --- | --- | --- |
| OpenAI Python client (`openai._constants`, `_base_client`) | Default `max_retries=2`; eligible connection/timeout/status failures could reach three attempts. | The Agents SDK's scoped OpenAI adapter calls `client.with_options(max_retries=0)`. The original client is unchanged. |
| Agents model retry engine (`agents.run_internal.model_retry`) | General runner retries require an opt-in policy; a separate conversation-lock compatibility path could replay three times. | Explicit `ModelRetrySettings(max_retries=0)` disables runner retries and compatibility replay. No retry policy is installed. |
| Responses adapter (`agents.models.openai_responses`) | Uses the client's ordinary retry configuration unless scoped suppression is requested. | Honors the SDK's per-call suppression via `with_options`. Chat Completions has the same scoped adapter hook. |
| WebSocket pre-event disconnect replay | A distinct built-in compatibility replay exists for WebSocket responses. | Explicit zero also disables its pre-event retry flag. AgentGuard's current non-streaming HTTP path does not enter it. |
| Default HTTP transport (`httpx2.AsyncHTTPTransport`) | Connection retries default to zero. | Unchanged; the default transport adds no connection retry loop. |

The conversation-lock compatibility condition checks `BadRequestError.code ==
"conversation_locked"`; absence of a conversation ID **alone does not disable
that legacy branch**. Explicit zero is necessary. AgentGuard still passes no
conversation ID, previous-response ID, session, or automatic response chaining.

There is no remaining enabled SDK/client replay path in the tested first-party
HTTP configuration. We do not claim control over provider-internal generation,
upstream proxy retries, custom model/provider implementations, or caller-installed
transports that implement their own retries. Switching transport/provider requires
new verification. SDK tracing exporters are separate from model inference and
their configuration is not changed by this work. No live provider behavior was
measured.

## Transport proof, not counter inference

`tests/test_model_execution_transport.py` executes the real production path,
real Agents Runner, real structured-output validation, and real OpenAI client.
Only HTTP I/O is replaced with `httpx2.MockTransport`; test clients have fake
credentials and tracing is disabled. Counts increment on transport handler entry,
independently of returned token usage and `Usage.requests`.

The client starts with its real default `max_retries=2`. Fault responses are
followed by a would-be success on replay, making hidden retries observable.
Test-only sleep doubles prevent real backoff waits; suppressed paths record none.

| Case | Observed requests for the affected logical call |
| --- | --- |
| Router, recovery, synthesis success | 1 each |
| Connection error, timeout, HTTP 429, 500, 401, 403 in each component | 1 each; failure propagates |
| Invalid router/recovery structured output | 1; no replay |
| Conversation-lock error without or with a conversation ID | 1; failure propagates |
| Exhausted budget before routing | 0 |
| Late router/recovery/synthesis success | 1; returned usage retained, result rejected |

The conversation-lock regression control removes suppression and returns a
non-client-retryable 400. It observes two transport entries, both with the OpenAI
client retry header at zero, proving the separate SDK compatibility replay is
what explicit zero prevents. Another control calls the original client outside
the SDK scope and verifies its default retry still operates. Concurrent request
tests use isolated wire counters and clients; one request's failure cannot replay
or change the other request.

Successful requests are compared against the former settings at the serialized
request-body boundary, ignoring only opaque runtime call IDs. Model identity,
instructions, schemas, inputs, tools, projection, output, and aggregate tokens
are unchanged. Tests also cover the SDK environment-selected model override.

## Telemetry and failure behavior

`ComponentSpan` and `AttemptTelemetry` now separate:

- `logical_model_calls` / logical call identity: application invocations.
- `sdk_visible_requests`: only SDK-returned evidence.
- `transport_attempts_observed`: `None` in production, which has no transport
  observer. The test harness owns its own actual dispatch counts.
- `lower_layer_retries_configured=False`: configuration evidence on the normal
  SDK execution path; `None` for unverified injected runners.
- `http_retry_count=None`: suppression configuration does not prove that a
  request actually reached the transport, so zero is not manufactured.

Configuration evidence is recorded before calling the SDK and survives provider
failures. Timeout, rate-limit, network, provider, authentication, authorization,
and invalid-output categories retain their meaning. No fallback answer or broader
authorization is introduced. A synthesis failure leaves completed required tools
recorded and does not execute them again.

Recovery remains a separate logical call, with a distinct ID and attempt 1.
A failed router does not trigger recovery; only the existing planning-completeness
conditions do. A primary routing success followed by recovery makes two model
requests before synthesis, not a routing retry.

## Budgets, validation, and next phase

13C.2 admission and acceptance checks remain authoritative: no dispatch after
pre-dispatch exhaustion; late results preserve completed work/usage and are
abandoned. Disabling client retries does not add cancellation or a component
timeout. No retry admission or attempt budget is configured by default. Explicit
13C.3B policies must additionally admit each retry within the original budget.

Run `python -m pytest tests -q`, including the 13B fault matrix and all 13C budget
tests. No live evaluation is needed. Existing test-only tracing wrappers preserve
the new run configuration instead of replacing it.

13C.3B implements explicit AgentGuard eligibility, attempt limits, shared allowance,
budget/reserve admission, backoff and failed-attempt accounting. Tools remain
single-attempt. Application retry behavior remains disabled by default; see
[Model retry policy](MODEL_RETRY_POLICY.md) for controlled use and its tests.
