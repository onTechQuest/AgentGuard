# Milestone 13D.2B: transport and event-loop ownership

This is bounded **offline loopback transport qualification**, not load testing or
permission to activate production concurrency. Production code, models, prompts,
tools, datasets, reliability/retry policy, quality gates, and CI are unchanged.

## Actual stack and fixture differences

The probes use installed `Runner.run()` / `Runner.run_sync()`, `AsyncOpenAI`,
`DefaultAsyncHttpx2Client`, and the actual httpcore2 TCP connection pool. The
remote Responses service is replaced by a deterministic HTTP/1.1 server bound
to `127.0.0.1` on an OS-selected free port. No `MockTransport` or fake Runner is
used. Requests carry unique tags; server-side counters record every received
request and its accepted TCP connection. Responses echo the tag through the
normal Responses JSON schema and SDK result parser.

The audited installation is Python 3.11.5, Agents SDK 0.22.2, OpenAI 3.11.0,
httpx2 2.12.0, AnyIO 4.15.1. Artifacts include installed versions, including
httpcore2, rather than claiming compatibility with other versions or platforms.

Test-only differences from production are explicit:

- Fake credentials and a loopback base URL; tracing disabled.
- HTTP instead of external HTTPS: DNS, TLS, proxies, and provider behavior are
  outside this qualification.
- Four-second fixture transport/synchronization bounds, not production deadlines.
- Proxy environment inheritance disabled.
- Default client pool limits retained: 1,000 connections and 100 keepalive slots.
- SDK model retry settings and base OpenAI clients both use zero retries.
- Simple tool-free test agents instead of the complete AgentGuard orchestration.

13D.2A separately qualifies request-local AgentGuard orchestration; the two sets
of evidence must not be described as an already-qualified production integration.

## Fail-closed execution and hang containment

Each experiment runs in a separate subprocess with a 35-second outer deadline.
Server gates, barriers, socket waits, and worker joins are bounded. The server
is shut down and its handler connections/threads are closed/joined. Unsupported
cross-loop workers may fail their join bound; the report records that failure
instead of repairing ownership. Process termination then contains remaining
resources. This is not successful client cleanup.

Every HTTP client has a pre-dispatch hook requiring the fixture's exact loopback
host/port and fake Authorization value. A Python audit hook independently denies
external socket destinations and external DNS resolution. The HTTP hook covers
Windows Proactor/ConnectEx paths without relying only on socket audit events.
An intentionally misconfigured non-loopback request verifies rejection before
connection or server dispatch. No real credentials or real OpenAI endpoint are
used. Normal runs do not start SDK tracing exporters.

An unexpected synchronization/server assertion fails pytest. A timeout in the
deliberately unsafe shared-client experiment is accepted as a *qualification
failure* only if it produced a partial diagnostic checkpoint. Missing evidence
or a timeout in the proposed supported alternatives fails the test. Partial
observations are marked; post-termination cleanup is unknown. Terminal output
and the artifact preserve failures even when the regression test itself passes.

## Experiments and findings

| Experiment | Evidence and interpretation |
| --- | --- |
| One loop, one client, 2/10/25 tasks | Concurrent first wave, sequential reuse wave, correct response tags, exact dispatch counts, and clean client shutdown. One TCP connection per overlapping first-wave request; subsequent calls reuse those connections. |
| Shared SDK client, 2/5 thread-owned loops | Real Runner calls, two rounds per worker, shared default SDK HTTP pool. Reuse exposes different-event-loop / closed-loop errors, transport failures, and shutdown-bound failures. Successful requests do not qualify this ownership model. |
| SDK concurrent first-use factory | A barrier around real client construction forces five callers past the actual unsynchronized None check. Five clients/pools are created; one is the final global client, four remain open outside that global reference until test cleanup. This proves ambiguous ownership, not a measured initialization-time HTTP failure. |
| Worker exits before client closes | A completed request leaves a pooled connection. Closing its owner loop and using the client elsewhere exposes a loop-affinity failure. |
| One client per worker/loop, 2/5 workers | Two requests per worker retain thread/loop identity, reuse that worker's connection, then close the client before closing the loop. No cross-worker transport sharing. |
| Runner loop lifecycle | Actual run_sync creates a loop when none exists, reuses it across success/429/success, leaves it open, and leaves no pending tasks after these calls. Nested run_sync in an active loop is rejected without dispatch. |
| Cancellation through Runner and directly through AsyncOpenAI | Cancel after the local server has received a held request, then complete an unrelated request. Cancellation is local; server-side work was already received. |
| Shutdown with a request in flight | Cancel/drain the request task, close its client on the owning loop, shut down the executor, then close the loop. |
| Delayed response without cancellation | A peer completes before the held response is released. The held request then completes normally. This does not introduce or test a new production deadline policy. |

The installed Runner invokes `shutdown_asyncgens()` after each synchronous run
but retains the loop for reuse. Subsequent runs produce ResourceWarning messages
about async generators scheduled after that call. These are preserved separately
from unclosed socket/transport warnings. The worker-owned experiments completed
and closed resources, but their repeated-run warning prevents an unconditional
clean-lifecycle classification. No private loop flag was reset and no SDK warning
was suppressed as a fix.

Connection-disconnect observations after cancellation are recorded separately
from fixture assertion errors. A successful local socket write does not prove
that a cancelled client accepted the response. At cancellation, the server has
received the request but the eventual remote outcome is unknown to the caller.

## Scorecard interpretation

Each probe records attempts, completions, mapping errors, exceptions and their
chains, loop-affinity errors, initialization races, cleanup failures, worker join
timeouts, pending tasks, unclosed clients/pool entries, warnings, HTTP requests,
client/pool counts, connection IDs, and lifecycle evidence. Client counts mean
HTTP transport instances, not transient OpenAI wrapper copies.

Hidden retries count duplicate HTTP arrivals for one logical request tag. Clean
and intentionally cancelled attempts must each have exactly one server request.
Cross-loop failures can happen before the server receives anything: a missing
HTTP arrival is not called a retry, and unknown/unfinished observations are not
invented as successes. The 429 control proves one server request for its failing
attempt and an independently successful subsequent request.

Architecture totals sum separate experiments/processes; they are not simultaneous
pool sizes, throughput estimates, or failure probabilities. Expected cancellation
is separate from transport failure. The intentional 429 and outbound-denial
controls are reported separately from architecture success/failure totals.

| Architecture | Classification | Decision |
| --- | --- | --- |
| A: shared async client across thread-owned loops | NOT_QUALIFIED | Do not activate. Loop affinity, lifecycle failures, and lazy initialization races are demonstrated. |
| B: worker/loop-owned client, synchronous entry | CONDITIONALLY_QUALIFIED | Local transport reuse/isolation/closure pass; the Runner's repeated-loop lifecycle warning still requires explicit resolution or documented SDK-supported disposition. |
| C: one loop, shared async client | QUALIFIED within bounded offline scope | Task correlation, reuse, cancellation, and cleanup pass. This does not qualify a production async AgentGuard migration. |

## Architecture-first review and next direction

**A defect type:** transport/event-loop ownership mismatch, plus unsynchronized
shared-client initialization. **Root cause:** loop-associated async connections
are reused or closed from foreign loops; the global factory has no single owner.
**Correct layer:** client/provider lifecycle and hosting ownership, not business
policy, prompts, tools, deadlines, or request telemetry.

**Tactical patch risk:** a global lock serializes calls but does not transfer loop
affinity. Extra retries obscure the defect; fresh clients for every call discard
reuse and complicate resources. Neither approach is qualified here.

**Generic solution:** one explicit owner loop for each transport, a corresponding
startup/shutdown lifecycle, and no global cross-loop pool. Regression must cover
reuse, overlapping requests, initialization, cancellation, failure, and teardown
against the actual pool, retaining request-level isolation tests.

**B caveat:** repeated run_sync lifecycle warnings belong to SDK Runner integration.
Do not suppress warnings or reset private asyncio state to manufacture a clean
qualification. Keep this issue distinct from A's broken transport ownership.

**Recommended next production direction:** retain the synchronous AgentGuard
entry point and introduce explicitly worker/loop-owned transport, subject to the
Runner lifecycle condition. This has measured connection reuse and request
isolation and requires less change to the qualified synchronous budget, retry,
and obligation orchestration than an async-first migration. C is a sound transport
alternative, but converting decorators, cancellation, tools, and orchestration
would require separate qualification; transport success alone does not justify it.

The next milestone should define the worker/client ownership API, lifecycle,
and supported handling of the Runner warning, then test integration through the
unchanged production request path offline. Keep live concurrency disabled until
that integration is qualified and a bounded live campaign is separately authorized.

## Reproduction and evidence

The implementation qualification is retained in the gitignored
`reports/transport_13d2b_qualification.json`. Its full offline run passed 1,741
tests (19 new transport tests), including the 75-request 13D.2A isolation suite
with zero violations and the 28/28 fault matrix.

| Observed metric | A: cross-loop shared | B: loop-owned workers | C: one-loop async |
| --- | ---: | ---: | ---: |
| Logical attempts | 16 | 14 | 81 |
| Completed responses | 9 | 14 | 78 |
| Intentional local cancellations | 0 | 0 | 3 |
| Server HTTP arrivals | 14 | 14 | 81 |
| Mapping errors / hidden retries | 0 / 0 | 0 / 0 | 0 / 0 |
| Transport exceptions / loop-affinity errors | 7 / 7 | 0 / 0 | 0 / 0 |
| Extra initialization allocations | 4 | 0 | 0 |
| Cleanup failures (worker join timeouts) | 4 | 0 | 0 |
| Clients still open in observation | 2 | 0 | 0 |
| ResourceWarning messages | 35 | 84 | 0 |
| Unclosed-resource warnings (subset) | 5 | 0 | 0 |
| HTTP clients / pools across probes | 8 / 8 | 7 / 7 | 7 / 7 |

A includes the closed-owner lifecycle and initialization probes. Two A experiments
have incomplete teardown observations because workers missed the join bound;
zero pending tasks in recorded checkpoints is not a claim that those experiments
had clean shutdown. B's warnings are the repeated-run async-generator warnings
described above. The three-request Runner success/429/success control and blocked
outbound-target control are separate from the architecture table. None of these
counts is a population failure-rate or capacity estimate.

Run the complete offline suite:

```powershell
python -m pytest tests -q
```

To also retain the measured scorecard and exception/lifecycle evidence, choose a
new path before running the same suite:

```powershell
$env:AGENTGUARD_TEST_TRANSPORT_REPORT = 'reports/transport_ownership_new_run.json'
python -m pytest tests -q
```

The optional artifact uses exclusive creation and never overwrites an earlier
report. `reports/` remains gitignored. Development probes and final qualification
must not be pooled into workload or capacity estimates. Counts and warning totals
in the generated artifact are observations from that specific run.
