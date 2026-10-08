from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
import tempfile

import httpx
import pytest

from hoard_link._comfy import ComfyClient
from hoard_link.config import LinkConfig
from hoard_link.types import CAPABILITIES
from tests.conftest import Router, make_link


@pytest.mark.asyncio
async def test_status_covers_every_capability():
    router = Router()
    link = make_link(router, config=LinkConfig())
    status = await link.status()
    assert set(status.keys()) == set(CAPABILITIES) | {"routes"}      # + the measured-routes block
    assert set(status["routes"]) == {"file", "updated_at", "source", "tasks", "problem"}
    for cap in CAPABILITIES:
        entry = status[cap]
        assert entry["capability"] == cap
        assert entry["state"] in ("resolved", "unavailable")
        assert isinstance(entry["reason"], str) and entry["reason"]


@pytest.mark.asyncio
async def test_link_comfy_returns_client_when_configured_url_set():
    router = Router()
    cfg = LinkConfig(comfy_url="http://127.0.0.1:8199")
    link = make_link(router, config=cfg)
    client = await link.comfy()
    assert isinstance(client, ComfyClient)
    assert client.url == "http://127.0.0.1:8199"


@pytest.mark.asyncio
async def test_link_comfy_returns_none_when_unreachable():
    router = Router()
    link = make_link(router, config=LinkConfig())
    client = await link.comfy()
    assert client is None


@pytest.mark.asyncio
async def test_link_comfy_resolves_via_loopback_probe():
    router = Router()
    router.get(8188, "/system_stats", httpx.Response(200, json={}))
    router.get(
        8188,
        "/object_info/CheckpointLoaderSimple",
        httpx.Response(200, json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[]]}}}}),
    )
    link = make_link(router, config=LinkConfig())
    client = await link.comfy()
    assert isinstance(client, ComfyClient)
    assert client.url == "http://127.0.0.1:8188"


def _llamacpp_registry(url: str = "http://127.0.0.1:8081/v1/chat/completions", model: str = "model.gguf"):
    return {
        "items": [{
            "url": url,
            "models": [model],
            "endpoint_id": "llama-1",
            "model_type": "llm",
            "backend": "llamacpp",
        }]
    }


def _llamacpp_probe_routes(router: Router, port: int = 8081, *, model: str = "model.gguf") -> Router:
    from tests.conftest import llama_health

    llama_health(port, router=router)
    router.get(port, "/props", httpx.Response(200, json={
        "model_path": f"C:/models/{model}",
        "default_generation_settings": {},
    }))
    router.get(port, "/v1/models", httpx.Response(200, json={"data": [{"id": model}]}))
    router.get(port, "/slots", httpx.Response(200, json=[]))
    return router


@pytest.mark.asyncio
async def test_faustus_llamacpp_registry_requires_ready_matching_model_and_keeps_custom_base():
    from hoard_link.config import LinkConfig

    router = Router()
    router.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    router.get(7000, "/api/models", httpx.Response(200, json=_llamacpp_registry(
        "http://127.0.0.1:8081/v1/chat/completions", "model.gguf"
    )))
    _llamacpp_probe_routes(router)
    link = make_link(router, config=LinkConfig(faustus_token="ody_test"))

    status = await link.status()
    assert status["llm"]["state"] == "resolved"
    assert status["llm"]["details"]["resident"] is True
    assert status["llm"]["details"]["served_model"] == "model.gguf"

    # A server that reports loading/not-ready is never promoted from registry metadata.
    loading = Router()
    loading.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    loading.get(7000, "/api/models", httpx.Response(200, json=_llamacpp_registry()))
    loading.get(8081, "/props", httpx.Response(503, json={"error": "model loading"}))
    unavailable = await make_link(loading, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    assert unavailable.state == "unavailable"
    assert any("offline, not ready" in reason for reason in unavailable.details["reasons"])

    # A healthy but differently loaded model is also not the registry item.
    mismatched = Router()
    mismatched.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    mismatched.get(7000, "/api/models", httpx.Response(200, json=_llamacpp_registry()))
    _llamacpp_probe_routes(mismatched, model="other.gguf")
    mismatch = await make_link(mismatched, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    # The stale Faustus row is rejected, then the independent loopback probe
    # truthfully reports the different model that is actually serving.
    assert mismatch.state == "resolved"
    assert mismatch.details["source"] == "loopback"
    assert mismatch.model == "other.gguf"

    # The exact configured host and port are probed even when the endpoint URL
    # carries a custom /v1 prefix; it is not replaced with a loopback default.
    custom = Router()
    custom.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    custom.get(7000, "/api/models", httpx.Response(200, json=_llamacpp_registry(
        "http://127.0.0.1:8082/v1/chat/completions", "model.gguf"
    )))
    _llamacpp_probe_routes(custom, port=8082)
    resolved = await make_link(custom, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    assert resolved.state == "resolved"
    assert resolved.url == "http://127.0.0.1:8082/v1/chat/completions"


@pytest.mark.asyncio
async def test_faustus_generic_openai_endpoint_is_not_mislabeled_llamacpp():
    from hoard_link.config import LinkConfig

    router = Router()
    router.get(7000, "/api/health", httpx.Response(200, json={"status": "healthy"}))
    router.get(7000, "/api/models", httpx.Response(200, json=_llamacpp_registry()))
    # A generic OpenAI-compatible service's /props response does not prove
    # llama.cpp identity. In particular, a generic /health endpoint is ignored.
    router.get(8081, "/health", httpx.Response(200, json={"status": "ok"}))
    router.get(8081, "/props", httpx.Response(200, json={"status": "ok"}))
    router.get(8081, "/v1/models", httpx.Response(200, json={"data": [{"id": "model.gguf"}]}))
    result = await make_link(router, config=LinkConfig(faustus_token="ody_test")).resolve("llm")
    assert result.state == "unavailable"
    assert any("did not identify itself as llama.cpp" in reason for reason in result.details["reasons"])


@pytest.mark.asyncio
async def test_faustus_registry_llamacpp_probe_uses_real_http_and_custom_v1_url(monkeypatch):
    from hoard_link.config import LinkConfig
    from hoard_link.link import Link
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.linkchat import LinkService
    from hoard_link.hub import tools as hub_tools

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/api/models":
                payload = _llamacpp_registry(f"{base}/v1/chat/completions", "served.gguf")
            elif self.path == "/health":
                payload = {"status": "ok"}
            elif self.path == "/props":
                payload = {"model_path": "C:/models/served.gguf", "default_generation_settings": {}}
            elif self.path == "/v1/models":
                payload = {"data": [{"id": "served.gguf"}]}
            elif self.path == "/slots":
                payload = []
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        async def fixed_faustus_url():
            return base

        links: list[Link] = []

        def link_factory(app: str) -> Link:
            link = Link(LinkConfig(faustus_token="ody_test"))
            monkeypatch.setattr(link, "_faustus_url", fixed_faustus_url)
            links.append(link)
            return link

        with tempfile.TemporaryDirectory(prefix="hoard-link-mcp-probe-") as data_dir:
            service = LinkService(HubConfig(data_dir=data_dir, jobs_enabled=False), link_factory=link_factory)
            hub = SimpleNamespace(link=service, facets=[])
            result = hub_tools.call(hub, "hub_link_status", {"force": True})
            assert result["llm"]["available"] is True
            assert result["llm"]["model"] == "served.gguf"
            assert result["llm"]["provider"] == "llamacpp"
            service.close()
        assert links
        assert {"/api/models", "/health", "/props", "/v1/models", "/slots"}.issubset(set(seen))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
