# Milestone 13D.2C: production-path transport and Runner lifecycle

## Decision and scope

**B_PRODUCTION_DIRECTION.** Architecture B is **QUALIFIED within this bounded
offline qualification**, with the explicit warning disposition below. This is
not approval to activate production concurrency or a live capacity qualification.
Architecture C remains qualified as a transport control, but its production
orchestration has not been migrated or qualified.

The unchanged warning-based classification rule in the 13D.2B regression summary
still prints B as conditional. This 13D.2C decision adds the generator/resource
evidence and explicit warning disposition that the earlier summary lacked.

The new probes use actual `run_support_agent_detailed()`, actual `Runner.run_sync()`,
the installed HTTP client/pool, and a loopback HTTP/1.1 Responses fixture. Primary
routing, completeness recovery, policy resolution, execution plans, deterministic
business implementations, privacy projection, synthesis hooks, budget enforcement,
retry configuration, and telemetry all run. The only injected boundaries are the
test provider configuration, request clocks, and a transparent business-tool
observer that calls the original implementation. No production files changed.

The response fixture controls model outputs; these results qualify orchestration
and lifecycle, not model quality. Socket/DNS audit guards prohibit non-loopback
traffic; request hooks permit only the allocated fixture port and fake credential.
Tracing uses a local public `TracingProcessor`, with no exporting processor.

## Warning root cause and disposition

Installed versions: Python 3.11.5, Agents SDK 0.22.2, OpenAI 3.11.0,
httpx2/httpcore2 2.12.0. The inspected local sources are:

- `agents/run.py`, `AgentRunner._run_sync_impl`: its `finally` calls
  `default_loop.shutdown_asyncgens()` after each model call. The accompanying
  SDK comment explicitly keeps the loop open for subsequent runs.
- Python `asyncio/base_events.py`, `_asyncgen_firstiter_hook`: once shutdown has
  been called, each newly iterated generator emits a `ResourceWarning`. The hook
  **still registers that generator** in the loop's generator set.
- `shutdown_asyncgens()` sets the flag, closes registered generators with
  `aclose()`, and reports close failures to the loop exception handler. Its flag
  stays set on this intentionally reusable loop.

Thus subsequent model calls trigger a lifecycle advisory even though they use
the same owning loop and client correctly. Client-close order does not cause it.
Tracing does not cause it. The direct async client control does not cause it.
The test harness neither creates this SDK lifecycle decision nor resets its flag.

There is one distinct warning class/message template, with these generator
origins. **Each** receives `SDK_WARNING_WITH_PROVEN_CLEAN_RESOURCE_STATE` in the
measured scope:

| Generator | Origin |
| --- | --- |
| `Auth.async_auth_flow` | HTTP authentication flow |
| `safe_async_iterate` (three instances per normal response) | HTTP iterator wrappers |
| `ByteStream.__aiter__` | Request byte stream |
| `Response.aiter_bytes` | Response decoding |
| `Response.aiter_raw` | Raw response iteration |
| `BoundAsyncStream.__aiter__` | Client-bound response stream |
| `AsyncResponseStream.__aiter__` | Transport adapter |
| `PoolByteStream.__aiter__` | Connection-pool response stream |
| `HTTP11ConnectionByteStream.__aiter__` | HTTP/1.1 stream |
| `AsyncHTTP11Connection._receive_response_body` | HTTP/1.1 body reader |

The observer records warning category, source location, generator name, request,
and component. It weakly references the actual generator from the warning stack:
after cleanup, every observed generator is either gone or has `ag_frame=None`.
No forced garbage collection is used. Task sets, registered generators, clients,
pool connections, trace/span membership, loop exceptions and worker/server joins
are checked separately. Unknown warnings fail qualification, not silently pass.
The weak-reference check proves closure of warned generators, not arbitrary
process-wide memory-leak absence.

`-W always` enables warnings normally hidden by Python. The observer forwards
every warning to the original stderr handler; there is no warning suppression,
stderr redirection, private-flag reset, SDK patch, or workaround in production.
Warnings also remain in the generated evidence. Their known disposition is based
on measured resource state **and** the installed SDK's deliberate public
`run_sync()` loop-reuse implementation; it must be requalified on dependency
changes. This is not a promise about untested streaming, sessions, other Python
versions, or long-duration hosting.

| Control | HTTP calls | Warnings |
| --- | ---: | ---: |
| Direct client, 1 / 3 / 5 calls | 1 / 3 / 5 | 0 / 0 / 0 |
| Runner sync, 1 / 3 / 5 calls | 1 / 3 / 5 | 0 / 24 / 48 |
| Runner sync, 3 calls, local tracing enabled | 3 | 24 |

Normal responses produce 12 warnings per call after the first call on a loop.
That explains 13D.2B's 84 warnings: seven workers each made one subsequent call.
The warnings scale with calls, not leaked pools or unfinished tasks. Production
path warnings occur in router, recovery, and synthesis phases, including ordinary
success and injected failures; none originates from final client/loop teardown.

## Persistent-worker production-path results

Each worker handles normal success, recovery-triggered success, and another
normal success sequentially. A server barrier overlaps the initial router calls
of multiple workers. Requests alternate order targets. The real business tool
returns shipped/processing facts; the fixture synthesis uses the projected
function result received on the wire. Support synthesis has `tools=[]`.

| Persistent workers | Requests completed | HTTP dispatches | Tools executed | Pools / TCP connections | Warnings |
| --- | ---: | ---: | ---: | --- | ---: |
| 1 | 3/3 | 7 | 3 | 1 / 1 | 72 |
| 2 | 6/6 | 14 | 6 | 2 / 2 | 144 |
| 5 | 15/15 | 35 | 15 | 5 / 5 | 360 |

These are separate experiments, not throughput measurements. Request hooks assert
every dispatch runs on the client owner's thread and loop. Each worker reuses one
TCP connection across all three requests and all seven model calls. Request IDs,
logical-call IDs, operation IDs, labels, tool targets, projected results, and
telemetry are isolated. Required reads execute exactly once before synthesis.
Both primary and recovered planning use the same actual production orchestration.

## Failure and cancellation recovery

A further single persistent worker handles 13 requests: eight successes and five
intentional failures, totaling 23 HTTP dispatches and nine deterministic reads
(the synthesis-failure request has already completed its read). Each fault is
immediately followed by a successful request on the same loop and client:

| Fault | Failing-request HTTP calls | Observed outcome |
| --- | ---: | --- |
| Router 429 | 1 | `CapabilityRoutingError` wrapping `RateLimitError` |
| Router 500 | 1 | `CapabilityRoutingError` wrapping `InternalServerError` |
| Held router request cancelled locally | 1 | `CancelledError`; Runner drains its task |
| Synthesis 500 | 2 | `InternalServerError`; required read already completed |
| Router result arrives after budget | 1 | `RequestDeadlineExceeded`; returned result abandoned |

Cancellation is injected only after the server receives the request, and the
server waits for local task drainage before releasing its response. There is no
claim of remote cancellation. The cancelled connection is replaced; the same
owned pool remains usable (two TCP connections over this fault experiment).
The delayed-result case advances that request's clock past the unchanged 20s
production deadline. It does not add sleeps or alter policy allowances. Absolute
deadlines are unchanged across stages, and later requests get fresh budgets.

All production probes assert one HTTP dispatch per logical component, retry
headers of zero, no consumed application retries, cleared request contextvars,
and zero pending tasks between requests. Lower-layer/application retry policy
remains disabled. Error-wrapper handling preserves the underlying failure type.

## Shutdown contract and resources

1. Stop accepting new work (the test's finite request roster is exhausted).
2. Finish/cancel and await owned work. An interrupted `run_sync()` already drains
   its task; the owner also checks/drains the remaining task set.
3. Await client close **on its owning loop**, before closing the loop.
4. Drain async generators and report any close errors.
5. Await default-executor shutdown.
6. Confirm no pending tasks, open generators or pooled connections, then close
   the loop and clear its thread binding.
7. Join/terminate the worker; then stop the local fixture server and join handlers.

There are zero unintended transport/loop-affinity errors, loop exception-handler
events, unfinished tasks, open clients, retained pool connections, open warned
generators, unbalanced trace/span items, or surviving workers/server threads in
the qualified probes. This is a tested teardown sequence, not a new production
admission queue or preemptive deadline implementation.

## Architecture C control and B-versus-C decision

For each tracing mode, one loop/client executes one intentionally cancelled
request followed by two waves of three concurrent Runner tasks: seven HTTP
dispatches, six completions, one cancellation, zero warnings and clean shutdown.
These are real async Runner calls, **not** an async rewrite of AgentGuard.

| Consideration | B: persistent synchronous workers | C: async-first owner |
| --- | --- | --- |
| Preserve current runtime | Actual full path qualified here | Async decorators/orchestration still need migration |
| Lifecycle | Clean measured state; known visible SDK advisory | Clean and warning-free control |
| Cancellation | Owner-loop task cancellation and drainage tested | Task cancellation and drainage tested |
| Resources | One thread, loop and pool per worker | One loop/pool across concurrent tasks |
| Reuse | Measured across router/recovery/synthesis and requests | Measured across task waves |
| Observability | Existing production telemetry preserved | Await-aware telemetry boundaries need qualification |
| Implementation risk | Explicit ownership/lifecycle addition | Wider control-flow and cancellation migration |
| Hosting | Dedicated persistent worker owner required | Async application lifespan owner required |

Recommend B because its actual production integration, cleanup, failure recovery,
and resource-backed warning disposition are established without changing the
qualified synchronous orchestration. C's efficiency advantage is architectural,
not a measured capacity claim, and does not by itself justify migration.

A future `WorkerRuntimeOwner` should own thread, loop, client/provider, request
admission and ordered teardown; a scoped provider binding should feed the model
boundary. No mutable process-global async client may span worker loops. Keep
budgets/telemetry/operations request-local. This abstraction is **not implemented**
here. If C is selected later, router/recovery/model boundaries, decorators,
required-tool dispatch, cancellation, and telemetry lifetimes must become
await-aware and independently qualified.

## Reproduction and regression evidence

New tests are in `tests/production_transport/`. The first full regression run
passed 1,755 tests and exposed one 13D.2B fixture error: intentionally unsafe
cross-loop ownership closed a request after HTTP headers, and JSON parsing of the
truncated body raised a server exception. The fixture now records expected and
received byte counts as `truncated_http_evidence`, counts the event as a transport
defect in the architecture scorecard, and requires zero truncations for supported
ownership probes. A deterministic partial-body socket test covers this distinction.
No successful dispatch or harmless warning is claimed for a truncated request.
13D.2A is unchanged; prior report artifacts are untouched.

To retain a fresh, exclusive-create artifact:

```powershell
$env:AGENTGUARD_PRODUCTION_TRANSPORT_REPORT = 'reports/transport_13d2c_new_run.json'
python -m pytest tests -q
```

The final qualification artifact is `reports/transport_13d2c_qualification_final.json`;
the accompanying 13D.2B regression artifact is
`reports/transport_13d2c_regression_13d2b.json` (both gitignored). The first run's
`reports/transport_13d2c_qualification.json` is retained separately. Prior reports
are not overwritten. The test subprocesses inherit
stderr, and standard pytest failure capture remains available. No live calls or
production concurrency are run.

Final validation: `python -m pytest tests -q` using the project virtual environment
passed **1,757 tests in 176.99s**, including 16 new tests. All 13D.2A regression
tests passed (75 controlled requests, zero isolation violations); all 19 13D.2B
tests passed; the fault matrix remained **28/28** with telemetry preserved and no
fabrication or authorization violations.
