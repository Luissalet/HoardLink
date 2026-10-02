"""Contracts requested by apps while moving to the shared implementation."""
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from hoard_link import agentkit, family, fam_agenda, guard, service


def test_family_tool_keeps_every_embedding_dimension():
    data = {"vectors": [[0.5] * 300 for _ in range(80)]}
    tool = agentkit.Tool("embed", "Embedding vectors.", None, agentkit.ann(True), lambda *_: data, capped=False)
    assert agentkit.call_tool([tool], None, "embed", {}) == data
    ordinary = agentkit.Tool("list", "A listing.", None, agentkit.ann(True), lambda *_: data)
    assert "truncated" in agentkit.call_tool([ordinary], None, "list", {})
    assert not agentkit.is_uncapped()
    with agentkit.uncapped():
        assert agentkit.is_uncapped()
        assert agentkit.call_tool([ordinary], None, "list", {}) == data
    assert not agentkit.is_uncapped()


@pytest.mark.parametrize("error,status,code", [(LookupError("missing"), 404, "not_found"), (PermissionError("hidden"), 403, "forbidden")])
def test_expected_tool_errors_do_not_become_internal_failures(monkeypatch, error, status, code):
    monkeypatch.setattr(family, "record_call", lambda *a, **k: None)
    def call(name, args):
        raise error
    app = FastAPI()
    app.include_router(agentkit.make_agent_router(tools_fn=lambda: [], call_fn=call, token_fn=lambda: "t" * 40,
                                                 instructions="", app_name="test"))
    with TestClient(app) as client:
        result = client.post("/api/agent/call", json={"name": "get"}, headers={"Authorization": "Bearer " + "t" * 40})
    assert result.status_code == status and result.json()["code"] == code


def test_direct_tool_routes_can_require_the_same_token(tmp_path, monkeypatch):
    monkeypatch.setattr(family, "record_call", lambda *a, **k: None)
    app = FastAPI()
    @app.post("/api/agent/example")
    def example():
        return {"ok": True}
    family.install_fastapi(app, "test", str(tmp_path), protect_tool_routes=True)
    token = (tmp_path / "mcp-token").read_text().strip()
    with TestClient(app) as client:
        assert client.get("/api/agent/tools").status_code == 200
        assert client.post("/api/agent/example").status_code == 401
        assert client.post("/api/agent/example/", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/agent/example", headers={"Authorization": "bEaReR " + token}).status_code == 200
        assert client.post("/api/agent/call", json={"name": "example"}).status_code == 401


def test_agenda_preserves_only_a_bounded_deduplication_key():
    raw = {"title": "Maintenance", "start": "2026-10-05", "dedupe_key": "x" * 300}
    assert fam_agenda.normalize_item(raw)["dedupe_key"] == "x" * 200
    raw["dedupe_key"] = "  "
    assert "dedupe_key" not in fam_agenda.normalize_item(raw)


def test_vendored_merchant_imports_stay_inside_the_copy(monkeypatch):
    root = Path(agentkit.__file__).parent
    spec = importlib.util.spec_from_file_location("merchant_vendor", root / "__init__.py", submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "merchant_vendor", package)
    spec.loader.exec_module(package)
    module = importlib.import_module("merchant_vendor.merchants")
    assert module.fold.__module__ == "merchant_vendor.text"
    assert module.ups_valid.__module__ == "merchant_vendor.tracking"


def test_wsgi_and_http_server_use_the_same_guard():
    calls = []
    app = guard.wsgi_middleware(lambda e, s: [b"allowed"], port_getter=lambda: 8800)
    assert app({"HTTP_HOST": "localhost:8800", "REQUEST_METHOD": "GET"}, lambda *a: calls.append(a)) == [b"allowed"]
    body = app({"HTTP_HOST": "hostile.example", "REQUEST_METHOD": "HEAD"}, lambda *a: calls.append(a))
    assert body == [b""] and calls[-1][0] == "403 Forbidden"
    handler = SimpleNamespace(headers={"Host": "hostile.example"}, command="GET", server=SimpleNamespace(server_address=("localhost", 8800)),
                              wfile=io.BytesIO(), send_response=lambda s: calls.append(s), send_header=lambda *a: None, end_headers=lambda: None)
    assert guard.check_handler(handler) is False
    assert calls[-1] == 403 and b"error" in handler.wfile.getvalue()


def test_safe_reads_can_keep_the_existing_activity_api_contract():
    headers = {"host": "localhost:8800", "origin": "https://foreign.example"}
    assert guard.check_request("GET", headers, guard_safe_methods=False) is None
    assert guard.check_request("POST", headers, guard_safe_methods=False)[0] == 403
    assert guard.check_request("GET", {"host": "foreign.example"}, guard_safe_methods=False)[0] == 403


def test_handler_validation_is_reported_as_bad_input():
    app = FastAPI()
    service.install_error_handlers(app)
    class Input(BaseModel):
        count: int
    @app.get("/invalid")
    def invalid():
        raise ValueError("bad value")
    @app.get("/validation")
    def validation():
        Input(count="invalid")
    with TestClient(app) as client:
        assert client.get("/invalid").status_code == 400
        result = client.get("/validation")
        assert result.status_code == 400 and result.json()["code"] == "invalid_arguments"
        assert result.json()["issues"][0]["loc"] == "count"
