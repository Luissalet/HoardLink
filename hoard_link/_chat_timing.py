"""Opt-in, content-free timing for the Hub's family chat route."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import logging
import os
from time import perf_counter

log = logging.getLogger(__name__)
_current: ContextVar[ChatTiming | None] = ContextVar("hoard_chat_timing", default=None)
_http_stages = {
    "connect_tcp": "backend_connect",
    "start_tls": "backend_tls",
    "send_request_headers": "backend_send",
    "send_request_body": "backend_send",
    "receive_response_headers": "backend_first_response",
    "receive_response_body": "backend_body",
}


class ChatTiming:
    def __init__(self):
        self.started = perf_counter()
        self.elapsed = {}
        self.counts = {}

    def add(self, name, start):
        self.elapsed[name] = self.elapsed.get(name, 0.0) + (perf_counter() - start) * 1000

    @contextmanager
    def stage(self, name):
        start = perf_counter()
        try:
            yield
        finally:
            self.add(name, start)

    def extensions(self):
        starts = {}
        self.counts["backend_requests"] = self.counts.get("backend_requests", 0) + 1

        async def trace(event, _info):
            method, state = event.rsplit(".", 2)[-2:]
            if method not in _http_stages:
                return
            if state == "started":
                starts[method] = perf_counter()
                if method == "connect_tcp":
                    self.counts["backend_connections"] = self.counts.get("backend_connections", 0) + 1
            elif (start := starts.pop(method, None)) is not None:
                self.add(_http_stages[method], start)

        return {"trace": trace}

    def snapshot(self):
        return {
            "ms": {**{key: round(value, 3) for key, value in self.elapsed.items()},
                   "total": round((perf_counter() - self.started) * 1000, 3)},
            "counts": {"backend_requests": 0, "backend_connections": 0, **self.counts},
        }


@contextmanager
def request():
    if os.environ.get("HOARD_LINK_CHAT_TIMINGS") != "1":
        yield
        return
    timing = ChatTiming()
    token = _current.set(timing)
    try:
        yield
    finally:
        _current.reset(token)
        log.info("link chat timing: %s", timing.snapshot())


def stage(name):
    timing = _current.get()
    return timing.stage(name) if timing is not None else nullcontext()


def measure(name):
    def wrap(func):
        @wraps(func)
        def timed(*args, **kwargs):
            with stage(name):
                return func(*args, **kwargs)
        return timed
    return wrap


def http_options():
    timing = _current.get()
    return {"extensions": timing.extensions()} if timing is not None else {}


def first_activity():
    timing = _current.get()
    if timing is not None:
        timing.elapsed["first_model_activity"] = (perf_counter() - timing.started) * 1000


def receipt(payload):
    timing = _current.get()
    return {**payload, "timing": timing.snapshot()} if timing is not None else payload
