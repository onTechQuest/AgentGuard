# Milestone 13D.2A: controlled concurrency isolation

These tests qualify **request-state isolation under controlled concurrent
execution**. They do not qualify the shared production asynchronous HTTP client
for cross-thread use, live OpenAI concurrency, provider capacity, throughput,
queueing, or saturation. They do not activate production concurrency.

## Harness boundary

`tests/concurrency/` uses bounded `ThreadPoolExecutor` workers around the existing
`run_support_agent_detailed()` entry point. Production routing validation,
planning completeness/recovery, policy resolution, execution plans, sequential
required operations, projection, budgets, and telemetry execute unchanged.

Before workers start, a fixture installs one context-selected synchronous Runner
stub and controlled read-only tool doubles. Each request owns its scripted model
outputs, facts, dispatch counters, and fake monotonic clock. The stub returns
structured router/recovery outputs and synthesizes a scripted answer from the
actual projected tool history. Independent expected facts validate that history.
No production provider or client is constructed. Shared-client access and socket
connects are explicitly blocked in the fixture.

Each stub entry is a controlled dispatch. Tests reconcile those entries with
logical model-call telemetry and attempt records, requiring one dispatch per
attempt and zero extra attempts. Recovery contributes a separate logical call.
This boundary cannot establish the retry/connection behavior of a real transport;
the existing isolated-client transport tests remain a separate source of evidence.

Barriers force router overlap; events impose specific stage dependencies, including
one request reaching synthesis before another finishes routing, and reverse
synthesis completion. A lock protects the shared checkpoint log. Waits have
defensive timeouts; executor cleanup occurs after workers finish. The harness
contains no unbounded waits or timing-dependent sleeps. Abort tests exercise
broken synchronization cleanup. Arbitrary blocking external adapters are outside
this controlled harness contract.

## Request ownership contract

Every independent top-level request must own a distinct:

- `RequestBudget`, request ID, and root cancellation signals;
- `ProductionExecutionTelemetry`, component spans, and attempt records;
- `RequestRetryState` and usage counters;
- planning result, execution plan, and execution trace when those stages run;
- final result or terminal exception.

Sharing these roots across independent requests is invalid usage. The test
coordinator rejects shared budgets/IDs within a submitted population. A request
object also rejects reuse. Identity checks retain owners until the audit ends,
avoiding false results caused by Python reusing released object IDs.

Child budgets intentionally share their own root's ID and cancellation signals.
The tests cancel a child while another root remains healthy. Frozen budget
metadata does not imply that its mutable signals are safe to share across roots.

Repeated reads of the same order still own separate projected dictionaries,
operation IDs, traces, telemetry, and results. Tool fixtures intentionally supply
request-specific markers so cross-request routing cannot pass unnoticed.
Operations within a request remain sequential; synthesis checks that all required
operations have completed and received the expected arguments and facts.

## Context propagation

Tests cover worker reuse after success, routing failure, and deadline rejection,
plus nested synchronous requests and restoration of all runtime context bindings.

On the tested Python 3.11 executor path, plain submission does not propagate the
calling request's context. A component offloaded that way cannot assume that
telemetry or budget context is available. Explicit `copy_context()` propagates
bindings but shares referenced objects; it does not deep-copy request state.
Tests verify that a new top-level request replaces inherited roots and restores
the inherited context after completion. They do not endorse concurrent mutation
of one request's roots by arbitrary copied-context child tasks.

## Completed snapshots and publication

Workers create snapshots after the production request has returned or raised and
its decorators have finished. The collector receives a frozen value containing
JSON text and immutable identity metadata, not live telemetry lists. Mutable test
owners remain worker-owned during execution; the parent may inspect them only
after completion. Nested-test results have one outer-worker owner until joining.

Only the parent coordinator collects aggregate rows and publishes a report.
Tests reject worker-side publication/collection and refuse overwriting an existing
aggregate path. Existing aggregate writers are not parallelized or changed.

Incident tests separately invoke the existing recorder concurrently using both a
shared campaign recorder and separate recorder instances. They check unique file
and request IDs, appropriate shared/distinct run IDs, parseable JSON after writer
completion, and exact response/fact correlation. Six test incidents are written
under pytest temporary directories. No repository qualification report is replaced.
This does not claim atomic reader visibility during an incident write or safety
of simultaneous writes to existing aggregate `.tmp` paths.

## Test evidence and next phase

Run only offline tests:

```powershell
python -m pytest tests -q
```

The concurrency terminal scorecard reports executed request count, request-ID
collisions, telemetry/tool/budget contamination, duplicate operations, incorrect
answers, failure-isolation violations, controlled-dispatch amplification,
authorization bypass, and incident-file collisions. Deliberately corrupted
snapshots verify that the scorecard detects violations; those fabricated rows
are not counted as real executions or added to the campaign summary.

Expected failures include router and synthesis exceptions, a simulated 429,
deadline exhaustion, cancellation, and broken synchronization. Those are successful
test outcomes only when the failure remains isolated and healthy peers complete.

For 13D.2B, qualify transport/client/event-loop ownership separately. In particular,
exercise actual default pool lifecycle and contention using an offline network
backend, rather than inferring safety from these Runner stubs or independent mock
clients. Establish startup/shutdown ownership and prove no hidden dispatch retries
before proposing any live concurrency campaign. No production retry, deadline,
prompt, model, tool, dataset, gate, CI, or async orchestration changes are made here.
