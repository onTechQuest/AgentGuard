# Milestone 13D.3: opt-in bounded worker runtime

`src.hosting.runtime.WorkerRuntime` implements Architecture B around the existing
`run_support_agent_detailed()` path. Importing it starts nothing. The default
sequential entry point, CLI, CI, models, prompts, tools, datasets, policy values,
retries, and quality gates are unchanged. Qualification is offline/local only.

## Ownership and request execution

Explicit construction creates the configured number of persistent, non-daemon
threads. Each creates and owns one event loop, one OpenAI async client/HTTP
transport and one provider. The factory runs on that worker. A factory returning
a transport already registered to another worker is rejected before admission.
Initialization failures stop admission and join initialized workers; a
`StartupFailure.runtime` reference retains ownership if cleanup needs attention.

All model calls in an admitted request run synchronously and sequentially on its
assigned worker: primary router, optional recovery and synthesis. A ContextVar
scope adds the owned provider at `model_run_config()`. Ownership checks fail
closed for the wrong thread/loop or closed client. The concurrency path cannot
silently fall back to the SDK's shared async client. The ordinary entry point has
no binding and retains its previous settings-only configuration.

Transport instances are reused across requests and model calls. The same
deterministic operation executor and privacy projection execute before synthesis;
the support model still receives no executable tools. No work is replayed.

## Admission, queue and deadline

`submit(message, request_label=...)` returns an immutable `Admission`:

- An idle worker slot is reserved immediately, independently of queue capacity.
- If every worker is occupied, at most `queue_capacity` waiting jobs are kept.
- When both capacities are exhausted, admission returns `accepted=False` with
  `reason="capacity_exhausted"`. Calling its `result()` raises `AdmissionRejected`.
- Admission after shutdown returns `reason="runtime_stopped"`.
- There is no waiting for queue space, silent dropping or automatic resubmission.

A `RequestBudget` is created before the ingress admission lock. It uses the
checked-in production policy's **20,000 ms** duration. The same budget, ID and
absolute deadline reach the existing production runtime; worker startup does not
restart the clock. Queue time consumes that deadline. An expired queued request
enters the observed admission boundary but dispatches no model or tool work.

The mutable hosting counters are synchronized control-plane state, not shared
campaign reports. `status()` exposes offered/admitted/rejected counts, configured
workers, waiting queue depth, active workers, peak activity, starts, completions,
successful completions and worker lifecycle/error state. Reserved slots can exist
briefly before a worker increments the active counter. Queue capacity zero is
supported. Capacity is explicitly configured; no SLO thresholds are introduced.

## Completion and single-writer collection

`Admission.result(timeout=...)` returns a frozen `CompletedRequest`, including
request/worker IDs, outcome, output, absolute deadline, queue/service/end-to-end
durations, and serialized immutable telemetry/execution/dispatch evidence. It
does not return live SDK objects, mutable request contexts or the owned client.
Result timeout is a caller wait bound, not a new runtime deadline or retry.

One caller-owned `CampaignCollector` observes admissions and records completed
snapshots. Every collector operation checks its creating thread; workers never
write campaign reports. Keep one collector for the service campaign and observe
all its admissions. Cross-thread ingress may return tickets to that owner, but
must not call collector methods from worker/ingress threads.

Metrics include:

| Area | Evidence |
| --- | --- |
| Admission/concurrency | Offered, admitted, rejected, queue depth, configured/active workers, peak active requests |
| Throughput | Runtime starts, collected completions/successes, observation duration and completion rate |
| Latency | Per-request queue, service, ingress-to-service-completion, router, recovery, tools and synthesis; average/maximum summaries |
| Reliability | Success, admission/deadline rejection, provider failure, rate limit, cancellation, unknown failure; late-result flag separately |
| Consumption | Known tokens/request, usage completeness, logical calls, HTTP-client dispatch attempts, recovery count, extra retries |
| Isolation | Request-ID collisions, mismatched tool targets, duplicate invoked operations and mismatched telemetry/dispatch IDs; assessed-evidence counts |

The HTTP hook counts actual local client dispatch attempts. It does **not** claim
remote receipt, billing, or provider work completion. Qualification compares hook
counts with local server arrivals. Unknown token consumption is not estimated;
token rates require complete usage and positive observation time. The test
campaigns pass `measure_rates=False` because their clocks are deterministic and
are not throughput measurements. Missing execution/telemetry evidence is exposed
through assessed counts; zero detected violations alone does not prove coverage.

Service time includes runtime work and snapshot serialization. End-to-end time
ends at worker completion, not client network delivery. Stage durations come from
existing telemetry; absent stages contribute zero work time. Counters under
`runtime` are an instantaneous service snapshot, while collected-completion
metrics describe the immutable snapshots already recorded by the collector.

Reports omit final response text, original prompts, raw HTTP headers and tool
payloads; those remain on the explicit completion object where available. The
collector publishes a complete JSON file using a temporary file, fsync and an
atomic same-filesystem hard link to a unique target. Existing files are never
overwritten. Filesystems without atomic hard-link support fail explicitly.

Example API structure (does not run automatically):

```python
from src.hosting.runtime import WorkerRuntime
from src.hosting.collector import CampaignCollector

collector = CampaignCollector()
with WorkerRuntime(max_workers=2, queue_capacity=4) as runtime:
    tickets = [runtime.submit(message) for message in messages]
    for ticket in tickets:
        collector.observe(ticket)
    for ticket in tickets:
        if ticket.accepted:
            collector.record(ticket.result())
collector.publish("reports", runtime.status())
```

The application must handle rejected tickets explicitly. This example introduces
no retry/replay mechanism and does not activate any existing CLI path.

## Cancellation and bounded shutdown

`cancel(request_id)` removes queued work with an immutable cancellation result,
or marks the active request's budget and schedules cancellation on its owning
loop. At most one cancellation callback is queued per worker. It checks the
current request's cancellation signal so a later healthy request is unaffected.
Async cancellation concerns local work only; remote outcome may be unknown.

`shutdown(timeout=..., cancel=False)` stops admission, permits draining for half
the bound, then cancels remaining queued/active work if necessary. `cancel=True`
starts cancellation immediately. Each worker drains owned work, closes its client
on its loop, drains generators, shuts down the executor and closes/clears the
loop. The caller joins workers against one shared wall-clock timeout, not a
per-worker timeout multiplied by worker count. Successful shutdown is idempotent.
Lifecycle errors are explicit and retain sanitized error types in `status()`.

**Python cannot forcibly terminate an arbitrary blocking synchronous function.**
If work or resource cleanup ignores cancellation, shutdown raises
`ShutdownTimeout` within its caller bound and exposes `.runtime`. The runtime
retains its worker ownership; it does not falsely claim closure or discard a live
loop/client. The host must retain that handle, release the blocker where possible,
and call shutdown again. A permanently uncooperative extension requires process
supervision, not a thread kill. The offline test verifies timeout, retained
ownership and eventual clean joining after a blocker releases. This limitation
must be addressed by hosting policy before live concurrency activation.

The pinned SDK's repeated `run_sync()` generator warnings remain visible. Their
13D.2C disposition remains `SDK_WARNING_WITH_PROVEN_CLEAN_RESOURCE_STATE` in the
qualified scope; neither warnings nor private asyncio flags are suppressed or
patched by this runtime.

## Offline qualification

Tests in `tests/hosting/` use the real runtime and Runner with a loopback Responses
server, fixed fake credentials, blocked non-loopback sockets/DNS and no trace
export. A fallback to the SDK shared-client factory raises immediately. Fixtures
hold requests after receiving complete bodies, then release/cancel via events;
there are no arbitrary sleeps or external API calls.

The bounded correctness campaigns are:

| Workers | Queue capacity | Offered | Admitted | Rejected | Successful | HTTP dispatches |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 4 | 10 | 6 | 4 | 6 | 12 |
| 5 | 10 | 25 | 15 | 10 | 15 | 30 |

Separate probes cover one-worker reuse/recovery, queued expiration, queued and
active cancellation, graceful and cancelling shutdown, sequential failures with
worker reuse, and overlapping success/429/500/deadline/cancellation/synthesis
failure. Unit tests cover invalid capacities, initialization/shared-client/thread
creation failures, wrong-loop binding, default configuration restoration,
immutable results, collector ownership, isolation corruption and atomic reports.

Run the complete offline suite, optionally retaining fresh artifacts:

```powershell
$env:AGENTGUARD_HOSTING_REPORT_DIR = 'reports/concurrency_13d3_new_run'
python -m pytest tests -q
```

Use a new report directory for each campaign. No production concurrency is
default-activated. Before live use, the host still needs integration/admission
response handling, shutdown/process-supervision policy, deployment resource
limits and a separately authorized bounded live qualification. These local
campaigns establish correctness, not capacity, provider rate limits or SLOs.

## Validation record

There are 22 new hosting tests and 1,779 tests in the complete offline suite.
One complete run passed all 1,779. The last complete run passed 1,777 and hit
the existing 35-second subprocess bounds in two 13D.2B probes (`same[25]` and
`shared[2]`). Both probes subsequently passed unchanged together with all 22
hosting tests: **24/24 passed**. No timeouts or assertions were relaxed.

An earlier full run also encountered a best-effort `ValueError` from the existing
shared safety-incident recorder; isolated and subsequent full runs passed that
test. Its underlying cause was not established, and its logic was not changed.
These intermittent legacy results remain a qualification caveat to investigate
before live activation, rather than evidence of a clean run every time.

13D.2A and 13D.2C passed in the last full run, and the fault matrix remained
28/28. The new local campaigns consistently recorded 64 offers, 14 capacity
rejections, 50 completed results (36 successes and 14 intentional failures),
88 HTTP dispatches, zero extra retries and zero detected isolation violations.
All tested hosting workers, clients, pools and tasks shut down cleanly. The
uncooperative synchronous-work test separately verified a bounded timeout with
ownership retained, followed by clean shutdown after release.

Final campaign evidence is under `reports/concurrency_13d3_final/`, gitignored.
Earlier campaign directories and temporary recorder diagnostics are retained
under `reports/`; no prior artifacts were overwritten.
