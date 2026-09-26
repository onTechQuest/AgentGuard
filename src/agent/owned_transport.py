"""Explicit optional worker provider binding; the default request path is unchanged."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from threading import get_ident

from agents.models.openai_provider import OpenAIProvider


_owner = ContextVar("agentguard_owned_transport", default=None)


class OwnedTransport:
    def __init__(self, client, loop):
        self.client, self.loop, self.thread_id = client, loop, get_ident()
        self.provider = OpenAIProvider(openai_client=client)
        self.dispatches = []

    def check(self):
        if get_ident() != self.thread_id or asyncio.get_event_loop() is not self.loop:
            raise RuntimeError("Worker transport accessed outside its owning thread/loop")
        if self.client.is_closed():
            raise RuntimeError("Worker transport is closed")

    async def on_request(self, request):
        self.check()
        if _owner.get() is not self:
            raise RuntimeError("HTTP dispatch has no matching worker ownership scope")
        # HTTP-client request hooks observe local dispatch attempts, not remote
        # receipt or provider billing. Never retain URLs, headers or credentials.
        from src.agent import telemetry
        record, span = telemetry._request.get(), telemetry._span.get()
        self.dispatches.append({"request_id": record.request_id if record else None,
                                "component": span.component if span else None,
                                "retry_count": request.headers.get("x-stainless-retry-count")})

    @contextmanager
    def request_scope(self):
        self.check()
        self.dispatches = []
        token = _owner.set(self)
        try:
            yield self
        finally:
            _owner.reset(token)


def owned_provider():
    owner = _owner.get()
    if owner is None:
        return None
    owner.check()  # An invalid binding fails closed; never fall back to SDK globals.
    return owner.provider
