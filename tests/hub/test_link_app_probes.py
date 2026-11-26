from collections import Counter
import io
import json
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

from hoard_link.hub import contract, link_app_probes, link_check, link_probes
from hoard_link.hub.link_probes import FAILED, RESPONDS, STOPPED, UNCHECKED, LinkProbes
from hoard_link.hub.registry import read_manifest
from tests.hub.test_link_check import PRIVATE, HubDouble, forbidden, now_deadline, poll


APP_PORT = 55012
HEALTH_TOOL = "fixture_health"
CATALOGUE = "/api/agent/tools"
CALL = "/api/agent/call"
HTTP_TRANSPORT = contract._get, contract._post


def schema(name, *, read_only=True, required=()):
    return {"name": name, "annotations": {"readOnlyHint": read_only},
            "inputSchema": {"type": "object", "required": list(required)}, "description": PRIVATE}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "faustus"
    root.mkdir()
    monkeypatch.setenv("FAUSTUS_DIR", str(root))
    hub = HubDouble(tmp_path / "data")
    manifest = tmp_path / "faustus-plugin.json"
    manifest.write_text(json.dumps({
        "id": "fixture",
        "app": {"url_default": f"http://127.0.0.1:{APP_PORT}"},
        "x-family": {"link_check": {"tools": [HEALTH_TOOL, "fixture_write"], "health_tool": HEALTH_TOOL}},
    }), encoding="utf-8")
    app = read_manifest(manifest)
    assert app is not None
    hub.registry[app.id] = app
    calls = []
    tools = [schema("fixture_write", read_only=False), schema(HEALTH_TOOL)]

    def get(url, token, timeout):
        assert token == "" and 0 < timeout <= 2
        assert urlsplit(url).port == APP_PORT
        calls.append(("GET", urlsplit(url).path))
        return 200, {"tools": tools, "instructions": PRIVATE}

    def post(url, payload, token, timeout, headers):
        assert token == "" and 0 < timeout <= 2
        assert urlsplit(url).port == APP_PORT
        assert payload == {"name": HEALTH_TOOL, "tool": HEALTH_TOOL, "arguments": {}, "caller": "hub-link-check"}
        assert headers == {}
        calls.append(("POST", urlsplit(url).path))
        return 200, {"ok": True, "result": {"ok": True, "private": PRIVATE}}

    monkeypatch.setattr(contract, "_get", get)
    monkeypatch.setattr(contract, "_post", post)
    monkeypatch.setattr(contract.procs, "_listening_pids_cli", forbidden)
    return hub, app, calls, tools


def probe(probes, kind, name="fixture"):
    return link_app_probes.probe_registered_app(probes, f"registered_{kind}:{name}", now_deadline())


def test_compares_declared_and_served_tool_names(fixture):
    hub, app, calls, tools = fixture
    probes = LinkProbes(hub, listeners=lambda: {APP_PORT})
    check = probe(probes, "catalogue")
    assert (check.state, check.outcome, check.called) == (RESPONDS, "tool_catalogue_matches", "hub_app_tools")
    assert check.metrics == {
        "http_status": 200, "served_tools": 2, "declared_tools": 2, "missing_tools": 0, "unexpected_tools": 0,
    }
    tools[0] = schema("unexpected_tool")
    drift = probe(probes, "catalogue")
    assert (drift.state, drift.outcome) == (FAILED, "tool_catalogue_drift")
    assert drift.metrics["missing_tools"] == drift.metrics["unexpected_tools"] == 1
    assert calls == [("GET", CATALOGUE)] * 2
    assert PRIVATE not in repr((check, drift))

    app.family["link_check"]["tools"] = [HEALTH_TOOL, HEALTH_TOOL]
    assert probe(probes, "catalogue").outcome == "invalid_tool_declaration"
    tools[:] = [schema(HEALTH_TOOL), schema(HEALTH_TOOL)]
    assert probe(probes, "catalogue").outcome == "invalid_catalogue"


def test_partial_or_missing_manifest_never_claims_exact_match(fixture):
    hub, app, _, tools = fixture
    del app.family["link_check"]["tools"]
    probes = LinkProbes(hub, listeners=lambda: {APP_PORT})
    check = probe(probes, "catalogue")
    assert (check.state, check.outcome, check.metrics["served_tools"]) == (UNCHECKED, "no_tool_declaration", 2)

    app.family["module_controls"] = {"fixture": {"list": HEALTH_TOOL, "set": "fixture_write"}}
    check = probe(probes, "catalogue")
    assert (check.state, check.outcome, check.metrics["declared_tools"]) == (UNCHECKED, "partial_tool_declaration", 2)
    tools.pop()
    check = probe(probes, "catalogue")
    assert (check.state, check.outcome, check.metrics["missing_tools"]) == (FAILED, "tool_catalogue_drift", 1)


def test_bridge_manifest_defines_the_compatibility_catalogue(fixture):
    hub, app, calls, _ = fixture
    directory = Path(app.manifest_path).parent
    hub.register("hermes", APP_PORT)
    hub.register("phileas", APP_PORT, prefix="/compat/phileas", folder=directory)
    (directory / "bridge.json").write_text(json.dumps({
        "owner": "hermes", "tools": [{"schema": {"name": name}} for name in ("fixture_write", HEALTH_TOOL)],
    }), encoding="utf-8")
    result = probe(LinkProbes(hub, listeners=lambda: {APP_PORT}), "catalogue", "phileas")
    assert result.outcome == "tool_catalogue_matches"
    assert calls == [("GET", f"/compat/phileas{CATALOGUE}")]


def test_health_runs_one_read_only_call_through_the_http_proxy(fixture):
    hub, _, calls, _ = fixture
    check = probe(LinkProbes(hub, listeners=lambda: {APP_PORT}), "health")
    assert (check.state, check.outcome, check.scope) == (RESPONDS, "app_proxy_health", "app_proxy")
    assert check.metrics == {"http_status": 200}
    assert calls == [("GET", CATALOGUE), ("POST", CALL)]
    assert PRIVATE not in repr(check)


@pytest.mark.parametrize("published", [
    None, schema(HEALTH_TOOL, read_only=False), schema(HEALTH_TOOL, read_only="true"),
    schema(HEALTH_TOOL, required=("item_id",)),
])
def test_missing_or_unannotated_health_tool_is_never_called(fixture, published, monkeypatch):
    hub, _, calls, tools = fixture
    tools.pop()
    if published is not None:
        tools.append(published)
    monkeypatch.setattr(contract, "_post", forbidden)
    check = probe(LinkProbes(hub, listeners=lambda: {APP_PORT}), "health")
    assert (check.state, check.outcome) == (UNCHECKED, "no_health_probe")
    assert calls == [("GET", CATALOGUE)]


def test_health_does_not_retry_legacy_route_or_accept_error_result(fixture, monkeypatch):
    hub, _, calls, _ = fixture
    probes = LinkProbes(hub, listeners=lambda: {APP_PORT})

    def unavailable(url, *args):
        calls.append(("POST", urlsplit(url).path))
        return 404, {"detail": "Not Found"}

    monkeypatch.setattr(contract, "_post", unavailable)
    check = probe(probes, "health")
    assert (check.state, check.metrics["http_status"]) == (FAILED, 404)
    assert calls == [("GET", CATALOGUE), ("POST", CALL)]
    monkeypatch.setattr(contract, "_post", lambda *args: (200, {"ok": False, "error": PRIVATE}))
    check = probe(probes, "health")
    assert check.state == FAILED
    assert PRIVATE not in repr(check)


def test_proxy_and_catalogue_decline_redirects(fixture, monkeypatch):
    hub, _, _, tools = fixture
    attempted = []
    real_get, real_post = HTTP_TRANSPORT

    class Opener:
        def open(self, request, *, timeout):
            attempted.append((request.get_method(), request.full_url))
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, io.BytesIO(b"{}"))

    def no_redirect(*handlers):
        policy = next(handler for handler in handlers if isinstance(handler, contract.NoRedirect))
        assert policy.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:5232/") is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", no_redirect)
    probes = LinkProbes(hub, listeners=lambda: {APP_PORT})
    monkeypatch.setattr(contract, "_get", real_get)
    assert probe(probes, "catalogue").metrics["http_status"] == 302
    monkeypatch.setattr(contract, "_get", lambda *_: (200, {"tools": tools}))
    monkeypatch.setattr(contract, "_post", real_post)
    assert probe(probes, "health").metrics["http_status"] == 302
    assert attempted == [("GET", f"http://127.0.0.1:{APP_PORT}{CATALOGUE}"),
                         ("POST", f"http://127.0.0.1:{APP_PORT}{CALL}")]


def test_apps_without_http_contract_get_rows_without_network(fixture, monkeypatch):
    hub, app, _, _ = fixture
    app.family["agent_contract"] = False
    monkeypatch.setattr(contract, "_get", forbidden)
    monkeypatch.setattr(contract, "_post", forbidden)
    probes = LinkProbes(hub, listeners=lambda: {APP_PORT})
    assert probe(probes, "catalogue").outcome == "no_http_tool_contract"
    assert probe(probes, "health").outcome == "no_health_probe"


def test_stopped_and_excluded_apps_never_reach_either_transport(fixture, monkeypatch):
    hub, _, calls, _ = fixture
    hub.register("baird", 5232, folder="must-not-be-read")
    hub.register("wrong-port", 5232)
    monkeypatch.setattr(contract, "_get", forbidden)
    monkeypatch.setattr(contract, "_post", forbidden)
    probes = LinkProbes(hub, listeners=lambda: set())
    for kind in ("catalogue", "health"):
        assert probe(probes, kind).state == STOPPED
        hub.manually_stopped.add("fixture")
        assert probe(probes, kind).outcome == "manually_stopped"
        hub.manually_stopped.clear()
        assert probe(probes, kind, "baird").outcome == "operator_excluded"
        assert probe(probes, kind, "wrong-port").outcome == "endpoint_not_verified"
        assert probe(LinkProbes(hub, ["fixture"], listeners=forbidden), kind).outcome == "operator_excluded"
    assert calls == []
    assert "baird" not in hub.lookups


def test_registry_alias_cannot_indirectly_probe_an_excluded_owner(fixture):
    hub, app, calls, _ = fixture
    app.family["absorbed_by"] = {"id": "Baird"}
    probes = list(link_app_probes.app_links(hub.apps))
    assert len(probes) == 2
    assert {row.probe for row in probes} == {"excluded:baird"}
    assert calls == []


def test_manual_stop_after_catalogue_prevents_proxy_call(fixture, monkeypatch):
    hub, _, calls, _ = fixture
    previous = contract._get

    def stopping(*args):
        result = previous(*args)
        hub.manually_stopped.add("fixture")
        return result

    monkeypatch.setattr(contract, "_get", stopping)
    monkeypatch.setattr(contract, "_post", forbidden)
    check = probe(LinkProbes(hub, listeners=lambda: {APP_PORT}), "health")
    assert (check.state, check.outcome) == (STOPPED, "manually_stopped")
    assert calls == [("GET", CATALOGUE)]


def test_run_contains_registry_rows_and_sanitized_bilingual_evidence(fixture, monkeypatch):
    hub, _, _, _ = fixture
    hub.register("stopped", APP_PORT + 1)
    hub.register("no-probe", APP_PORT)
    hub.register("baird", 5232, folder="must-not-be-read")
    monkeypatch.setattr(link_probes, "_listener_ports", lambda: {APP_PORT})
    monkeypatch.setattr(link_check, "LINKS", tuple(row for row in link_check.LINKS if row.id == "9.1"))
    checker = link_check.LinkCheckFacet(hub, probe_timeout=2)
    try:
        run = poll(checker, checker.call({})["run_id"])
        rows = {row["id"]: row for row in run["links"]}
        assert run["summary"]["total"] == 7
        assert Counter(row["destination"] for row in rows.values() if row["id"] != "9.1") == {
            "fixture": 2, "stopped": 2, "no-probe": 2,
        }
        assert rows["1.3.app.fixture"]["state"] == RESPONDS
        assert rows["1.2.app.fixture"]["inventory_id"] == "1.2"
        assert rows["1.2.app.no-probe"]["evidence"]["detail"].startswith("El cat\u00e1logo respondi\u00f3.")
        assert rows["1.2.app.stopped"]["status"] == rows["1.3.app.stopped"]["status"] == "parado: sin iniciar"
        assert rows["1.3.app.no-probe"]["status"] == "no comprobado: sin sonda"
        assert rows["9.1"]["evidence"]["detail"] == "No comprobado por orden de la persona."
        english = poll(checker, run["run_id"], "en")
        assert next(r for r in english["links"] if r["id"] == "1.3.app.no-probe")["status"] == "not checked: no probe"
        assert PRIVATE not in json.dumps(run)
        assert PRIVATE not in (Path(hub.config.data_dir) / "link-check-history.json").read_text(encoding="utf-8")
    finally:
        checker.close()
