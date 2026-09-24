from __future__ import annotations

import asyncio
from typing import Callable, Optional

import httpx
import pytest

from hoard_link.config import LinkConfig
from hoard_link.link import Link


class FakeClock:
    """A controllable clock + no-delay sleep for wait_idle() tests."""

    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)  # yield control, but don't actually wait


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def make_link(
    handler: Callable[[httpx.Request], httpx.Response],
    config: Optional[LinkConfig] = None,
    fake_clock: Optional[FakeClock] = None,
) -> Link:
    client = make_client(handler)
    link = Link(config or LinkConfig.load(None, env={}, app="test"), client=client)
    if fake_clock is not None:
        link._now = fake_clock.now
        link._sleep = fake_clock.sleep
    return link


def json_response(status_code: int, data) -> httpx.Response:
    return httpx.Response(status_code, json=data)


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class Router:
    """A tiny (method, port, path) -> response dispatcher for MockTransport.

    Anything not explicitly registered raises ConnectError, i.e. "nothing
    is listening on that port" — the common case when probing ten llama.cpp
    ports for the one that answers.
    """

    def __init__(self):
        self.routes: dict[tuple[str, Optional[int], str], object] = {}

    def add(self, method: str, port: Optional[int], path: str, response) -> "Router":
        self.routes[(method.upper(), port, path)] = response
        return self

    def get(self, port: Optional[int], path: str, response) -> "Router":
        return self.add("GET", port, path, response)

    def post(self, port: Optional[int], path: str, response) -> "Router":
        return self.add("POST", port, path, response)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.port, request.url.path)
        entry = self.routes.get(key)
        if entry is None:
            raise httpx.ConnectError("connection refused", request=request)
        if callable(entry):
            return entry(request)
        return entry


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    """Never reach (or spawn) a real Hoard Hub from the test suite: the GPU
    lease client talks to 127.0.0.1:8810, which in these tests is either
    nothing or a route on a MockTransport."""
    monkeypatch.setenv("HOARD_HUB_AUTOSTART", "0")
    monkeypatch.setenv("HOARD_HUB_URL", "http://127.0.0.1:8810")
