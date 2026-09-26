"""Bounded, opt-in synchronous hosting; one thread/loop/client per worker.

No request work is replayed. Shutdown can cancel asynchronous model work, but
Python cannot forcibly stop an arbitrary blocking synchronous function. A missed
shutdown bound raises with ownership retained; the caller must not abandon it.
"""
import asyncio
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import json
import math
from threading import Condition, Thread
import time
from typing import Callable

from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from src.agent.owned_transport import OwnedTransport
from src.agent.request_budget import RequestBudget, RequestDeadlineExceeded, RequestBudgetRejected
from src.agent.runtime_reliability import default_runtime_policy
from src.agent.support_agent import run_support_agent_detailed
from src.agent import telemetry


class AdmissionRejected(RuntimeError):
    pass


class ShutdownTimeout(RuntimeError):
    def __init__(self, runtime):
        super().__init__("Worker shutdown bound exceeded; runtime still owns unfinished workers")
        self.runtime = runtime


class StartupFailure(RuntimeError):
    def __init__(self, runtime):
        super().__init__("Worker initialization failed; inspect runtime.status()")
        self.runtime = runtime


@dataclass(frozen=True)
class CompletedRequest:
    request_id: str
    worker_id: int
    outcome: str
    queue_wait_ms: float
    service_ms: float
    end_to_end_ms: float
    deadline_monotonic: float
    final_output: str | None
    telemetry_json: str
    execution_json: str
    dispatches_json: str


@dataclass(frozen=True)
class Admission:
    request_id: str
    accepted: bool
    reason: str | None
    queue_depth: int
    active_requests: int
    deadline_monotonic: float
    _future: Future = field(repr=False, compare=False)

    def result(self, timeout=None) -> CompletedRequest:
        if not self.accepted:
            raise AdmissionRejected(self.reason)
        return self._future.result(timeout)


@dataclass
class _Job:
    message: str
    label: str | None
    budget: RequestBudget
    future: Future


@dataclass
class _Worker:
    number: int
    thread: Thread | None = None
    loop: object = None
    job: _Job | None = None
    ready: bool = False
    closed: bool = False
    error: str | None = None
    lifecycle: tuple = ()
    cancel_callback_pending: bool = False


def _client_factory():
    # Same installed transport defaults, but an instance owned by this worker.
    http = DefaultAsyncHttpx2Client()
    try:
        return AsyncOpenAI(max_retries=0, http_client=http)
    except BaseException:
        asyncio.get_event_loop().run_until_complete(http.aclose())
        raise


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be positive finite seconds")
    return value


class WorkerRuntime:
    """Construct explicitly, then submit; no changes to the default entry path.

    client_factory runs in each owning worker and must return a fresh AsyncOpenAI
    client with its own transport. This seam supports local/offline qualification.
    No arbitrary model/provider override is accepted per request.
    """
    def __init__(self, max_workers: int, queue_capacity: int, *, client_factory: Callable = _client_factory,
                 clock: Callable = time.monotonic, startup_timeout: float = 10):
        if type(max_workers) is not int or max_workers <= 0 or type(queue_capacity) is not int or queue_capacity < 0:
            raise ValueError("Positive max_workers and nonnegative queue_capacity required")
        _seconds(startup_timeout)
        if not callable(client_factory) or not callable(clock):
            raise TypeError("client_factory and clock must be callable")
        self.max_workers, self.queue_capacity = max_workers, queue_capacity
        self._clock, self._factory = clock, client_factory
        self._policy = default_runtime_policy()
        self._condition, self._queue = Condition(), deque()
        self._workers = [_Worker(n) for n in range(max_workers)]
        self._accepting, self._stopping = False, False
        self._offered = self._admitted = self._rejected = self._active = self._peak = 0
        self._starts = self._completions = self._successful = 0
        self._transport_ids = set()
        try:
            for worker in self._workers:
                worker.thread = Thread(target=self._work, args=(worker,), name=f"agentguard-worker-{worker.number}")
                worker.thread.start()
        except BaseException as error:
            self._workers = [w for w in self._workers if w.thread and w.thread.ident is not None]
            try:
                self.shutdown(timeout=startup_timeout, cancel=True)
            finally:
                raise StartupFailure(self) from error
        deadline = time.monotonic() + startup_timeout
        with self._condition:
            while not all(w.ready or w.closed for w in self._workers):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            ready = all(w.ready and not w.closed for w in self._workers)
            self._accepting = ready
        if not ready:
            try:
                self.shutdown(timeout=startup_timeout, cancel=True)
            except (RuntimeError, ShutdownTimeout) as error:
                raise StartupFailure(self) from error
            raise StartupFailure(self)

    def submit(self, message: str, *, request_label: str | None = None) -> Admission:
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        # Ingress timestamp precedes even the admission lock, and is never reset.
        budget = RequestBudget(self._policy.request_deadline_ms, clock=self._clock)
        job = _Job(message, request_label, budget, Future())
        with self._condition:
            self._offered += 1
            free = next((w for w in self._workers if w.ready and not w.closed and w.job is None), None)
            reason = "runtime_stopped" if not self._accepting else None
            if reason is None:
                if free:
                    free.job = job  # Reserved worker slot; not part of waiting queue capacity.
                elif len(self._queue) < self.queue_capacity:
                    self._queue.append(job)
                else:
                    reason = "capacity_exhausted"
            if reason:
                self._rejected += 1
            else:
                self._admitted += 1
                self._condition.notify_all()
            return Admission(budget.request_id, reason is None, reason, len(self._queue), self._active,
                             budget.deadline_monotonic, job.future)

    def status(self):
        with self._condition:
            return {"offered": self._offered, "admitted": self._admitted, "rejected": self._rejected,
                    "configured_workers": self.max_workers, "queue_capacity": self.queue_capacity,
                    "queue_depth": len(self._queue), "active_workers": self._active,
                    "active_requests": self._active,
                    "peak_active_requests": self._peak, "starts": self._starts,
                    "completions": self._completions, "successful_completions": self._successful,
                    "accepting": self._accepting,
                    "workers": [{"worker_id": w.number, "alive": w.thread.is_alive(), "closed": w.closed,
                                 "error": w.error, "lifecycle": list(w.lifecycle)} for w in self._workers]}

    def cancel(self, request_id):
        with self._condition:
            job = next((j for j in self._queue if j.budget.request_id == request_id), None)
            if job is not None:
                self._queue.remove(job)
                self._finish_waiting(job, "cancellation")
                return True
            worker = next((w for w in self._workers if w.job and w.job.budget.request_id == request_id), None)
            if worker is None:
                return False
            self._cancel_worker(worker)
            return True

    def _cancel_worker(self, worker):
        worker.job.budget.request_cancellation()
        if worker.cancel_callback_pending:
            return
        worker.cancel_callback_pending = True
        def cancel_owned():
            # At most one control callback per worker, even if its loop is idle.
            # A later healthy request must never inherit an earlier cancellation.
            with self._condition:
                worker.cancel_callback_pending = False
                current = worker.job
                cancelled = current is not None and current.budget.cancellation_requested
            if cancelled:
                for task in asyncio.all_tasks(worker.loop):
                    task.cancel()
        worker.loop.call_soon_threadsafe(cancel_owned)

    def _finish_waiting(self, job, outcome):
        elapsed = max(0, (self._clock() - job.budget.started_at_monotonic) * 1000)
        job.future.set_result(CompletedRequest(job.budget.request_id, -1, outcome, elapsed, 0, elapsed,
                                             job.budget.deadline_monotonic, None, "null", "null", "[]"))
        self._completions += 1

    def _execute(self, worker, job, owner):
        started = self._clock()
        data, trace, output, outcome = None, None, None, "unknown_failure"
        with owner.request_scope():
            try:
                result = run_support_agent_detailed(job.message, request_label=job.label, request_budget=job.budget,
                                                     runtime_reliability_policy=self._policy)
                data = telemetry.snapshot(result.context_wrapper.production_telemetry)
                trace = result.context_wrapper.context.snapshot()
                output, outcome = str(result.final_output), "success"
            except BaseException as error:
                data = telemetry.snapshot(getattr(error, "production_telemetry", None))
                observed_trace = getattr(error, "trace", None)
                trace = observed_trace.snapshot() if observed_trace is not None else None
                category = (data or {}).get("terminal_failure_category")
                outcome = ({"RATE_LIMIT": "rate_limit", "PROVIDER_ERROR": "provider_failure",
                            "CANCELLED": "cancellation", "DEADLINE_EXHAUSTED": "deadline_rejection"}.get(category,
                           "cancellation" if isinstance(error, asyncio.CancelledError) else
                           "deadline_rejection" if isinstance(error, RequestDeadlineExceeded) else
                           "cancellation" if isinstance(error, RequestBudgetRejected) and job.budget.cancellation_requested
                           else "unknown_failure"))
            data_json, trace_json, dispatches_json = json.dumps(data), json.dumps(trace), json.dumps(owner.dispatches)
            ended = self._clock()
            return CompletedRequest(job.budget.request_id, worker.number, outcome,
                                    max(0, (started - job.budget.started_at_monotonic) * 1000),
                                    max(0, (ended - started) * 1000),
                                    max(0, (ended - job.budget.started_at_monotonic) * 1000),
                                    job.budget.deadline_monotonic, output,
                                    data_json, trace_json, dispatches_json)

    def _work(self, worker):
        loop, client = asyncio.new_event_loop(), None
        asyncio.set_event_loop(loop)
        worker.loop = loop
        lifecycle, executing = [], False
        def loop_error(loop, context):
            worker.error = "asyncio_lifecycle_error"
        loop.set_exception_handler(loop_error)
        try:
            client = self._factory()
            # Private transport identity is only inspected for ownership; it is
            # never patched. Reject a faulty factory returning shared resources.
            transport = client._client
            with self._condition:
                resources = {id(transport), id(getattr(transport, "_transport", transport))}
                if resources & self._transport_ids:
                    client = None  # Do not close another worker's resource.
                    raise RuntimeError("Worker transport factory returned a shared client")
                self._transport_ids.update(resources)
            owner = OwnedTransport(client, loop)
            owner.check()
            transport.event_hooks["request"].append(owner.on_request)
            with self._condition:
                worker.ready = True
                self._condition.notify_all()
            while True:
                with self._condition:
                    while worker.job is None and not self._stopping:
                        self._condition.wait()
                    if worker.job is None:
                        break
                    job = worker.job
                    self._active += 1
                    executing = True
                    self._starts += 1
                    self._peak = max(self._peak, self._active)
                result = self._execute(worker, job, owner)
                with self._condition:
                    self._active -= 1
                    executing = False
                    self._completions += 1
                    self._successful += result.outcome == "success"
                    worker.job = self._queue.popleft() if self._queue else None
                    job.future.set_result(result)
                    self._condition.notify_all()
        except BaseException as error:
            worker.error = type(error).__name__  # Never publish exception payload/credentials.
            with self._condition:
                self._accepting = False
                if executing:
                    self._active -= 1
                if worker.job and not worker.job.future.done():
                    self._finish_waiting(worker.job, "unknown_failure")
                    worker.job = None
                while self._queue:
                    self._finish_waiting(self._queue.popleft(), "unknown_failure")
                self._condition.notify_all()
        finally:
            try:
                lifecycle.append("stop_admission")
                async def close():
                    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    lifecycle.append("drain_work")
                    if client is not None:
                        await client.close()
                    lifecycle.append("close_client")
                    await loop.shutdown_asyncgens()
                    lifecycle.append("drain_generators")
                    await loop.shutdown_default_executor()
                    lifecycle.append("shutdown_executor")
                # Caller join is bounded. Never destroy a still-closing resource
                # just to meet that bound: this worker retains ownership until
                # cleanup returns; ShutdownTimeout exposes the runtime handle.
                loop.run_until_complete(close())
                if asyncio.all_tasks(loop):
                    raise RuntimeError("Worker cleanup left pending tasks")
            except BaseException as error:
                worker.error = "cleanup_" + type(error).__name__
            finally:
                loop.close()
                asyncio.set_event_loop(None)
                lifecycle.append("close_loop")
                with self._condition:
                    worker.closed = True
                    worker.lifecycle = tuple(lifecycle)
                    self._condition.notify_all()

    def shutdown(self, *, timeout=10, cancel=False):
        """Drain for half the bound, then cancel if needed; retain ownership on timeout."""
        timeout = _seconds(timeout)
        deadline = time.monotonic() + timeout
        with self._condition:
            self._accepting, self._stopping = False, True
            drain_until = time.monotonic() if cancel else time.monotonic() + timeout / 2
            self._condition.notify_all()
            while any(w.job for w in self._workers) and time.monotonic() < drain_until:
                self._condition.wait(max(0, drain_until - time.monotonic()))
            if any(w.job for w in self._workers):
                while self._queue:
                    self._finish_waiting(self._queue.popleft(), "cancellation")
                for worker in self._workers:
                    if worker.job:
                        self._cancel_worker(worker)
                self._condition.notify_all()
        for worker in self._workers:
            worker.thread.join(max(0, deadline - time.monotonic()))
        if any(w.thread.is_alive() for w in self._workers):
            raise ShutdownTimeout(self)
        if any(w.error for w in self._workers):
            raise RuntimeError("Worker lifecycle failure; inspect status for sanitized error types")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()
