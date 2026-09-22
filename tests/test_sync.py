from __future__ import annotations

import httpx

from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.link import Link


def test_sync_facade_chat_runs_in_background_thread():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "sync-hola"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = LinkConfig(
        capabilities={
            "llm": CapabilityConfig(url="http://127.0.0.1:8081/v1/chat/completions", model="m", api="openai")
        }
    )
    link = Link(cfg, client=client)
    try:
        result = link.sync.chat([{"role": "user", "content": "hi"}])
        assert result.text == "sync-hola"

        res = link.sync.resolve("llm")
        assert res.resolved

        status = link.sync.status()
        assert "llm" in status
    finally:
        link.sync.close()


def test_sync_facade_reuses_same_loop_thread_across_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = LinkConfig(
        capabilities={"llm": CapabilityConfig(url="http://127.0.0.1:8081/v1/chat/completions", model="m", api="openai")}
    )
    link = Link(cfg, client=client)
    try:
        link.sync.chat([{"role": "user", "content": "1"}])
        thread1 = link.sync._thread
        link.sync.chat([{"role": "user", "content": "2"}])
        thread2 = link.sync._thread
        assert thread1 is thread2
    finally:
        link.sync.close()
