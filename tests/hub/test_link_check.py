from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import io
import json
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import pytest

from hoard_link.hub import agent_routes, dojo, facets, link_check, link_probes, mcp, tools
from hoard_link.service_contracts import SERVICES
from hoard_link.hub.mailgate import MailGate, MailStore
from hoard_link.hub.server import _HubHandler
from hoard_link.hub.link_inventory import COMPATIBILITY_LINKS, INVENTORY, LINKS, SERVICE_LINKS
from hoard_link.hub.link_probes import FAILED, RESPONDS, STOPPED, UNCHECKED, LinkProbes, Observation
from hoard_link.hub.registry import App


PRIVATE = "private payload abc@example.test C:/private-directory"


def forbidden(*args, **kwargs):
    raise AssertionError("The checker called a forbidden operation")


def now_deadline():
    return time.monotonic() + 2


class HubDouble:
    start = stop = restart = open = call_app = app_tools = snapshot = faustus_status = emit = forbidden

    def __init__(self, path):
        self.config = SimpleNamespace(data_dir=str(path), language="es", faustus_urls=["http://127.0.0.1:7000"])
        self.registry = {}
        self.manually_stopped = set()
        self.lookups = []
        self.placement_state = {"location": "pc"}
        self.placement = SimpleNamespace(state=lambda: self.placement_state, status=forbidden, remote_health=forbidden,
                                         remote_app=forbidden, _ssh=forbidden)
        self.facet_map = {}
        self.facets = []

    @property
    def apps(self):
        return list(self.registry.values())

    def get(self, name):
        assert name != "baird", "Baird must be excluded before registry lookup"
        self.lookups.append(name)
        return self.registry.get(name)

    def automation_blocked(self, app):
        assert app != "baird"
        return app in self.manually_stopped

    def facet(self, name):
        assert name not in {"notify", "chats", "today", "search", "purchases", "order_receipts"}
        return self.facet_map.get(name)

    def register(self, name, port, *, prefix="", folder="", role=None):
        manifest = str(Path(folder) / "faustus-plugin.json") if folder else ""
        app = App(name, name, "", folder, f"http://127.0.0.1:{port}{prefix}", "/api/health",
                  role, manifest_path=manifest)
        self.registry[name] = app
        return app


@pytest.fixture
def hub(tmp_path, monkeypatch):
    base = tmp_path / "hub"
    faustus = tmp_path / "faustus"
    faustus.mkdir()
    monkeypatch.setenv("FAUSTUS_DIR", str(faustus))
    monkeypatch.setenv("HOARD_HUB_DATA_DIR", str(base))
    return HubDouble(base)


def poll(facet, run_id, lang="es"):
    deadline = time.monotonic() + link_check.PROBE_TIMEOUT_S + 1
    while time.monotonic() < deadline:
        run = facet.call({"run_id": run_id, "lang": lang})
        if run.get("phase") == "completed":
            return run
        threading.Event().wait(0.005)
    pytest.fail("Background run did not complete")


def test_inventory_covers_all_parent_ids_and_every_compatibility_and_service():
    expected_c = """
        1.3 1.7 1.8 1.9 1.10 1.11 1.12 1.13 1.15 1.16 1.17 1.18 1.19
        2.9 2.12 3.1 3.2 3.3 3.4 3.6 3.7 3.9
        4.1 4.2 4.3 4.4 4.5 4.6 4.7 4.9 5.3 5.4 5.5 5.6
        6.2 6.3 6.4 6.5 6.8 6.9 6.10 6.11 8.2 8.8 8.11 8.13 9.1
    """.split()
    assert {row.id for row in INVENTORY if row.inventory_status == "C"} == set(expected_c)
    assert Counter(row.inventory_status for row in INVENTORY) == {"C": 47, "P": 23, "D": 6, "R": 14}
    expected_bridges = """
        tantalus phileas cassandra argus echo funes diskhoard galton laplace midas borges gutenberg
        scheherazade lumiere daedalus gepetto gulliver heron platos babel theseus durer
    """.split()
    assert {row.id for row in COMPATIBILITY_LINKS} == {f"1.17.{app}" for app in expected_bridges}
    assert {(row.id, row.destination, row.probe) for row in SERVICE_LINKS} == {
        (f"4.5.{service}", owner, f"service:{service}") for service, owner, _ in SERVICES
    }
    assert len({row.id for row in LINKS}) == len(LINKS) == len(INVENTORY) + len(COMPATIBILITY_LINKS) + len(SERVICE_LINKS)
    assert all(row.origin and row.destination and row.via and row.probe for row in LINKS)


def test_stopped_apps_and_excluded_bridge_owners_never_reach_transport(hub):
    hub.register("cookhoard", 5210)
    hub.register("ledger", 5180)
    hub.register("talos", 5220)
    hub.register("funes", 5220, prefix="/legacy/funes")
    hub.register("nightingale", 5189)
    hub.manually_stopped.add("nightingale")
    attempted = []

    def transport(*args, **kwargs):
        attempted.append((args, kwargs))
        forbidden()

    probes = LinkProbes(hub, ["talos"], request=transport, listeners=lambda: {5189, 5220})
    assert probes.run("prerequisites:cookhoard,funes,kafka,tantalus,ledger:cook_workflow", now_deadline(), {}).state == STOPPED
    assert probes.run("mail_reader:ledger", now_deadline(), {}).state == STOPPED
    assert probes.run("faustus", now_deadline(), {}).state == STOPPED
    assert probes.native_tool("nightingale", "nightingale_modules", now_deadline()).outcome == "manually_stopped"
    assert probes.run("bridge:funes", now_deadline(), {}).outcome == "operator_excluded"
    assert probes.run("service:stt", now_deadline(), {}).outcome == "operator_excluded"
    assert not attempted
    assert "funes" not in hub.lookups


def test_baird_id_and_port_are_excluded_before_any_connection(hub, monkeypatch):
    hub.register("atlas", 5232)
    hub.register("baird", 5232, folder="not-a-test-input")
    reads = []
    transport = lambda *args, **kwargs: reads.append((args, kwargs))
    probes = LinkProbes(hub, request=transport, listeners=forbidden)
    assert probes.health("baird", now_deadline()).outcome == "operator_excluded"
    assert probes.catalogue("atlas", now_deadline()).outcome == "endpoint_not_verified"
    assert probes.run("excluded:baird", now_deadline(), {}).outcome == "operator_excluded"
    assert not reads
    assert hub.lookups == ["atlas"]


def test_remote_absorbed_app_uses_active_owner_and_refreshes_only_stale_health(hub):
    hub.register("phileas", 5221, prefix="/shipments")
    hub.placement_state = {"location": "sparks", "node": {"name": "test"},
                           "urls": {"hermes": "https://fake.invalid:8443"}}
    hub.placement._remote_health = {"checked": time.time(), "ok": True,
                                     "apps": {"hermes": {"service": "inactive", "healthy": False}}}
    called = []

    def transport(app, method, path, args, **kwargs):
        called.append((app.url, method, path))
        return 200, {"tools": [{"name": "mail_status"}]}

    probes = LinkProbes(hub, request=transport, listeners=forbidden)
    failed = probes.catalogue("phileas", now_deadline())
    assert (failed.state, failed.outcome, failed.location) == (FAILED, "no_running_service", "remote")
    assert not called
    hub.placement._remote_health["apps"]["hermes"]["service"] = "active"
    assert probes.catalogue("phileas", now_deadline(), ["mail_status"]).state == RESPONDS
    assert called == [("https://fake.invalid:8443/shipments", "GET", "/api/agent/tools")]
    hub.placement_state["since"] = time.time() + 1
    refreshed = []

    def passive_refresh(*, since):
        refreshed.append(since)
        return hub.placement._remote_health

    hub.placement.remote_health = passive_refresh
    assert probes.catalogue("phileas", now_deadline()).outcome == "remote_state_unknown"
    assert probes.catalogue("phileas", now_deadline()).outcome == "remote_state_unknown"
    assert refreshed == [hub.placement_state["since"]]
    assert len(called) == 1


def test_placement_refresh_queries_the_configured_node_without_changing_placement(hub):
    from hoard_link.hub.placement import Placement

    calls = []

    def runner(command, **kwargs):
        assert command[-2] == "fixture-node"
        assert command[-1] == Placement._health_command()
        calls.append(command[-1])
        return SimpleNamespace(returncode=0, stdout=b"faustus active 200\nhermes active 200\nprometheus active 200\n")

    placement = hub.placement = Placement(hub, runner=runner)
    state = placement._update(location="sparks", node={"name": "Fixture", "ssh": "fixture-node"}, since=time.time())
    placement._remote_health = {"checked": state["since"] - 1, "ok": True, "apps": {}}
    before = placement.path.read_bytes()
    probes = LinkProbes(hub, listeners=forbidden, request=forbidden)
    result = probes.placement(now_deadline())
    assert result.state == RESPONDS and result.metrics["healthy_http"] == 3
    assert len(calls) == 1 and placement.path.read_bytes() == before


def test_native_tool_boundary_and_bridge_catalogue_are_explicitly_read_only(hub, tmp_path):
    hub.register("nightingale", 5189, role="nightingale-hoard")
    hub.register("atlas", 5203)
    hub.register("cassandra", 5220, prefix="/services", folder=tmp_path)
    (tmp_path / "bridge.json").write_text(json.dumps({
        "owner": "talos", "tools": [{"schema": {"name": "svc_incidents"}}],
    }), encoding="utf-8")
    called = []
    nightingale_modules = {"name": "nightingale_modules", "annotations": {"readOnlyHint": True},
                           "inputSchema": {"type": "object", "properties": {}}}

    def transport(app, method, path, args, *, timeout):
        assert 0 < timeout <= 2
        called.append((app.id, method, path, args))
        if path == "/api/health":
            return 200, {"service": "nightingale-hoard", "private": PRIVATE}
        if method == "POST":
            return 200, {"modules": [{"name": PRIVATE, "data_dir": PRIVATE}]}
        if app.id == "nightingale":
            return 200, {"tools": [nightingale_modules]}
        return 200, {"tools": [{"name": "svc_incidents", "description": PRIVATE}]}

    probes = LinkProbes(hub, request=transport, listeners=lambda: {5189, 5203, 5220})
    results = [
        probes.run("app_tool:nightingale:nightingale_modules", now_deadline(), {}),
        probes.run("bridge:cassandra", now_deadline(), {}),
        probes.run("app_catalog:atlas:atlas_projects", now_deadline(), {}),
    ]
    assert [result.state for result in results] == [RESPONDS, RESPONDS, FAILED]
    assert results[-1].outcome == "missing_tools"
    assert PRIVATE not in repr(results)
    assert called == [
        ("nightingale", "GET", "/api/health", None),
        ("nightingale", "GET", "/api/agent/tools", None),
        ("nightingale", "POST", "/api/agent/call",
         {"name": "nightingale_modules", "tool": "nightingale_modules", "arguments": {}}),
        ("cassandra", "GET", "/api/agent/tools", None),
        ("atlas", "GET", "/api/agent/tools", None),
    ]
    with pytest.raises(ValueError, match="allowlist"):
        probes.native_tool("nightingale", "nightingale_set_module", now_deadline())
    with pytest.raises(ValueError, match="allowlist"):
        endpoint = probes._get_app("nightingale", now_deadline())
        probes._http(endpoint, "/api/mail/search", now_deadline())
    assert len(called) == 5

    nightingale_modules["annotations"].clear()
    assert probes.native_tool("nightingale", "nightingale_modules", now_deadline()).outcome == "no_health_probe"
    assert called[5:] == [("nightingale", "GET", "/api/health", None),
                          ("nightingale", "GET", "/api/agent/tools", None)]


def test_app_transport_never_follows_redirects_to_the_excluded_port(hub, monkeypatch):
    hub.register("atlas", 5203)
    opened = []

    class Opener:
        def open(self, req, *, timeout):
            opened.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, io.BytesIO(b"{}"))

    def build(*handlers):
        redirect_handler = next(handler for handler in handlers if isinstance(handler, link_probes.contract.NoRedirect))
        new_url = "http://127.0.0.1:5232/api/health"
        assert redirect_handler.redirect_request(None, None, 302, "Found", {}, new_url) is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    probes = LinkProbes(hub, listeners=lambda: {5203, 5232})
    check = probes.catalogue("atlas", now_deadline())
    assert check.state == FAILED and check.metrics == {"http_status": 302}
    assert opened == ["http://127.0.0.1:5203/api/agent/tools"]


def test_run_is_private_passive_and_writes_only_its_history(hub, monkeypatch):
    cfg = Path(hub.config.data_dir)
    cfg.mkdir()
    codes = cfg.parent / "atlas-source"
    codes.mkdir()
    atlas = hub.register("atlas", 5203, folder=codes)
    Path(atlas.manifest_path).write_text('{"mcp":{"command":"test"}}', encoding="utf-8")
    hub.register("mercator", 5195)
    hub.register("nightingale", 5189, role="nightingale-hoard")
    hub.register("baird", 5232, folder="not-a-test-input")
    violations = []
    metadata_calls = []

    def reject(name):
        def record(*args, **kwargs):
            violations.append(name)
            forbidden()
        return record

    for name in ("start", "stop", "restart", "open", "call_app", "app_tools", "snapshot", "faustus_status", "emit"):
        monkeypatch.setattr(hub, name, reject(name))

    def relay_description(app, *, check):
        metadata_calls.append((app, check))
        assert app == "atlas" and check is False
        return {"mcp_path": "/api/mcp/apps/atlas/mcp"}

    exclusion = cfg / "link-check.json"
    exclusion.write_text('{"excluded_apps":["mercator"]}', encoding="utf-8")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE events(ts, source, data)")
    db.execute("INSERT INTO events VALUES(?, ?, ?)", (123.0, "faustus", PRIVATE))
    # A cursor attempting to read the payload or write immediately fails.
    def authorizer(action, _table, column, _db_name, _cause):
        if action == sqlite3.SQLITE_READ and column == "data":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    db.set_authorizer(authorizer)
    hub.events = SimpleNamespace(_db=db, _lock=threading.Lock(), emit=forbidden, query=forbidden)
    hub.rules = SimpleNamespace(_lock=threading.Lock(), rules=[{"last": {"ok": True, "ts": 124, "error": PRIVATE}}],
                                list=forbidden, run=forbidden)
    hub.facet_map.update(
        mailgate=SimpleNamespace(config=lambda: {"enabled": True, "interval_min": 10}, clock=lambda: 400.0,
                                 last_successful_pass=lambda: 200.0, last_reader_claim=lambda reader: None,
                                 faustus_dir=lambda: None,
                                 helper_status=reject("helper_status"), mail_accounts=reject("mail_accounts"),
                                 send_mail=reject("send_mail"), run_pass=reject("mail_pass")),
        mcp_relay=SimpleNamespace(describe=relay_description, list_apps=reject("mcp_list_all"), handle=reject("mcp_session")),
        brain=SimpleNamespace(target=lambda: {"base": "http://127.0.0.1:7000"},
                              counts=lambda: {"done": 8}, submit=reject("brain_submit"), client=reject("brain_client")),
        fleet=SimpleNamespace(cached=None, status=reject("fleet_status"), reconcile=reject("fleet_reconcile")),
    )
    hub.link = SimpleNamespace(_status_cache=(0, None), status=reject("link_status"), chat=reject("link_chat"))
    monkeypatch.setattr(subprocess, "Popen", reject("Popen"))
    monkeypatch.setattr(subprocess, "run", reject("subprocess.run"))
    monkeypatch.setattr(webbrowser, "open", reject("browser"))
    monkeypatch.setattr(link_probes.procs, "_listening_pids_cli", reject("listener CLI"))
    monkeypatch.setattr(link_probes, "_listener_ports", lambda: {5189, 5203, 5195, 7000, 5232})
    called = []

    def transport(app, method, path, args, **kw):
        called.append((app.id, method, path))
        assert app.id not in {"baird", "mercator"} and app.port != 5232
        if path == "/api/health":
            return 200, {"service": "nightingale-hoard" if app.id == "nightingale" else "faustus", "private": PRIVATE}
        if path == "/api/hoard-copilot/capabilities":
            return 200, {"contract_version": "1.0", "private": PRIVATE}
        if path == "/api/agent/tools":
            return 200, {"tools": [
                {"name": "atlas_projects", "description": PRIVATE},
                {"name": "nightingale_modules", "annotations": {"readOnlyHint": True},
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}
        if method == "POST" and args["name"] == "nightingale_modules":
            return 200, {"modules": [{"path": PRIVATE}]}
        forbidden()

    monkeypatch.setattr(link_probes, "_request_app", transport)

    def proxy_catalogue(url, token, timeout):
        assert url in {"http://127.0.0.1:5203/api/agent/tools", "http://127.0.0.1:5189/api/agent/tools"}
        return 200, {"tools": [{"name": "atlas_projects", "description": PRIVATE}]}

    monkeypatch.setattr(link_probes.contract, "_get", proxy_catalogue)
    monkeypatch.setattr(link_probes.contract, "_post", reject("unexpected proxy POST"))
    facet = link_check.LinkCheckFacet(hub)
    try:
        started = facet.call({})
        assert started["phase"] == "running"
        run = poll(facet, started["run_id"])
        rows = {row["id"]: row for row in run["links"]}
        expected_total = len(LINKS) + 6  # Catalogue and proxy rows for three registry apps.
        assert run["history_saved"] and run["summary"]["total"] == expected_total
        assert sum(run["summary"][state] for state in link_check.STATES) == expected_total
        assert rows["4.5.dictation"]["destination"] == "boltagon"
        assert rows["3.1"]["evidence"]["metrics"] == {"events": 1, "last_event_at": 123.0}
        assert rows["3.2"]["evidence"]["metrics"]["enabled"] == 1
        assert rows["3.3"]["evidence"]["called"] == "faustus GET /api/health + Hub Brain queue counts"
        assert rows["3.6"]["evidence"]["scope"] == "prerequisites"
        assert rows["1.7"]["status"] == "responde"
        assert rows["1.16"]["evidence"]["metrics"] == {"declared": 1, "described": 1}
        assert metadata_calls and set(metadata_calls) == {("atlas", False)}
        assert not violations
        assert all(app not in {"baird", "mercator"} for app, _, _ in called)
        assert rows["1.8"]["evidence"]["outcome"] == "operator_excluded"
        assert rows["2.1"]["evidence"]["metrics"]["age_s"] == 200
        assert rows["9.1"]["status"] == "no comprobado"
        assert rows["9.1"]["evidence"]["detail"] == "No comprobado por orden de la persona."
        assert (cfg / "link-check-history.json").is_file()
        assert {p.name for p in cfg.iterdir()} == {"link-check.json", "link-check-history.json"}
        assert PRIVATE not in json.dumps(run)
        assert PRIVATE not in (cfg / "link-check-history.json").read_text(encoding="utf-8")
        assert called and all(path in link_probes.GET_PATHS for _, method, path in called if method == "GET")
        assert {(app, path) for app, method, path in called if method == "POST"} == {("nightingale", "/api/agent/call")}
    finally:
        facet.close()
        db.close()


def test_history_persists_first_failure_and_reports_recovery(hub, monkeypatch):
    link = next(row for row in INVENTORY if row.id == "1.7")
    monkeypatch.setattr(link_check, "LINKS", (link,))
    states = iter([FAILED, UNCHECKED, FAILED, RESPONDS, FAILED])

    class ProbeDouble:
        def __init__(self, *_):
            pass

        def run(self, probe, deadline, results):
            return Observation(next(states), "fixture", "fixture")

    monkeypatch.setattr(link_check, "LinkProbes", ProbeDouble)

    def run_once():
        facet = link_check.LinkCheckFacet(hub)
        try:
            run = poll(facet, facet.call({})["run_id"])
            return run["links"][0]
        finally:
            facet.close()

    first = run_once()
    assert first["failure_since"] == first["checked_at"]
    assert run_once()["failure_since"] == first["failure_since"]
    assert run_once()["failure_since"] == first["failure_since"]
    assert run_once()["failure_since"] is None
    later = run_once()
    assert later["failure_since"] == later["checked_at"] > first["checked_at"]


def test_background_run_coalesces_requests_and_enforces_probe_deadlines(hub, monkeypatch):
    monkeypatch.setattr(link_check, "LINKS", INVENTORY[:2])
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    class ProbeDouble:
        def __init__(self, *_):
            pass

        def run(self, probe, deadline, results):
            if not results:
                entered.set()
                release.wait(3)
                returned.set()
            return Observation(RESPONDS, "fixture", "fixture")

    monkeypatch.setattr(link_check, "LinkProbes", ProbeDouble)
    facet = link_check.LinkCheckFacet(hub, probe_timeout=0.25, run_timeout=1.5)
    try:
        start = facet.call({})
        assert entered.wait(1)
        assert facet.call({})["run_id"] == start["run_id"]
        assert facet.call({"run_id": start["run_id"]})["phase"] == "running"
        run = poll(facet, start["run_id"], "en")
        first, second = run["links"]
        assert first["evidence"]["outcome"] == "probe_timeout"
        assert first["status"] == "not responding"
        assert second["state"] == RESPONDS
        assert not returned.is_set()
    finally:
        release.set()
        facet.close()
        assert returned.wait(1)


def test_global_budget_marks_remaining_checks_and_retains_baird_exclusion(hub, monkeypatch):
    baird = next(row for row in INVENTORY if row.id == "9.1")
    monkeypatch.setattr(link_check, "LINKS", (*INVENTORY[:2], baird))
    release = threading.Event()
    returned = threading.Event()

    class SlowProbe:
        def __init__(self, *_):
            pass

        def run(self, probe, deadline, results):
            release.wait(3)
            returned.set()
            return Observation(RESPONDS, "fixture", "fixture")

    monkeypatch.setattr(link_check, "LinkProbes", SlowProbe)
    facet = link_check.LinkCheckFacet(hub, probe_timeout=2, run_timeout=0.25)
    try:
        run = poll(facet, facet.call({})["run_id"])
        assert [row["evidence"]["outcome"] for row in run["links"]] == [
            "probe_timeout", "run_timeout", "operator_excluded",
        ]
    finally:
        release.set()
        facet.close()
        assert returned.wait(1)


def test_facet_get_and_normal_agent_mcp_contract_without_hub_listener(hub, monkeypatch):
    monkeypatch.setattr(link_check, "LINKS", tuple(row for row in INVENTORY if row.id == "9.1"))
    hub.facets = facets.load_all(hub, modules=("link_check",))
    facet = hub.facets[0]
    name = "hub_link_check"
    spec = next(tool for tool in tools.all_tools() if tool["name"] == name)
    assert spec["annotations"]["readOnlyHint"] is True
    described = agent_routes.describe(f"/api/agent/tools/{name}")
    assert described == spec

    def get(query, agent=lambda: True):
        request = facets.Request("GET", "/api/link-check", query, {}, forbidden, agent)
        return facets.dispatch(hub.facets, request)

    def fake_request(method, path, body, **kwargs):
        assert method == "POST" and path == "/api/agent/call"
        assert kwargs.get("passive") is True
        assert agent_routes.CONTEXT not in body["arguments"]
        args = {**body["arguments"], agent_routes.CONTEXT: {"order_id": 123}}
        answer, status = agent_routes.invoke(hub, body["tool"], args)
        assert "receipt_id" not in answer
        return status, answer

    monkeypatch.setattr(mcp, "_hub_up", lambda: True)
    monkeypatch.setattr(mcp, "_ensure_hub", forbidden)
    monkeypatch.setattr(mcp, "request_context", forbidden)
    monkeypatch.setattr(mcp, "_request", fake_request)

    try:
        assert get({}).get("status") == 404
        assert get({}, lambda: False).status == 401
        discover = mcp.handle({"id": 1, "method": "tools/list"})["result"]["tools"]
        assert next(tool for tool in discover if tool["name"] == name)["annotations"]["readOnlyHint"] is True
        reply = mcp.handle({"id": 2, "method": "tools/call", "params": {"name": name}})["result"]
        started = json.loads(reply["content"][0]["text"])
        run = poll(facet, started["run_id"])
        latest = get({"lang": ["en"]})
        assert latest["run_id"] == run["run_id"] and latest["links"][0]["status"] == "not checked"
        assert get({"run_id": [run["run_id"]]}) == run
        monkeypatch.setattr(mcp, "_request", lambda *args, **kwargs: (302, None))
        redirected = mcp.handle({"id": 3, "method": "tools/call", "params": {"name": name}})["result"]
        assert redirected["isError"] is True
        monkeypatch.setattr(mcp, "_hub_up", lambda: False)
        monkeypatch.setattr(mcp, "_request", forbidden)
        reply = mcp.handle({"id": 4, "method": "tools/call", "params": {"name": name}})["result"]
        assert reply["isError"] is True
    finally:
        facet.close()


def test_mail_gateway_uses_only_last_success_and_its_interval(hub):
    gate = SimpleNamespace(config=lambda: {"enabled": True, "interval_min": 5},
                           last_successful_pass=lambda: 200, first_delay_s=20,
                           clock=lambda: 501, helper_status=forbidden, mail_accounts=forbidden, run_pass=forbidden)
    hub.facet_map["mailgate"] = gate
    probes = LinkProbes(hub, listeners=forbidden, request=forbidden)
    stale = probes.mail()
    assert stale.state == FAILED and stale.outcome == "mail_stale"
    assert stale.metrics == {"age_s": 301, "interval_s": 300, "last_success_at": 200}
    gate.clock = lambda: 500
    assert probes.mail().state == RESPONDS
    gate.last_successful_pass = lambda: None
    hub.started_at = 480
    assert probes.mail().outcome == "mail_since_start"
    gate.clock = lambda: 801
    assert probes.mail().outcome == "mail_first_pass_overdue"


def test_deployment_compares_saved_commits_without_git_or_ssh(hub, tmp_path):
    paths = {}
    sha = "a" * 40
    for app in ("faustus", "hermes", "prometheus"):
        folder = tmp_path / app
        (folder / ".git" / "refs").mkdir(parents=True)
        (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (folder / ".git" / "packed-refs").write_text(f"{sha} refs/heads/main\n", encoding="ascii")
        paths[app] = folder
    hub.placement.local_dirs = lambda: paths
    hub.placement_state = {"location": "sparks", "deployed": {app: sha + "|marker" for app in paths}}
    probes = LinkProbes(hub, listeners=forbidden, request=forbidden)
    matches = probes.deployment()
    assert matches.state == RESPONDS and matches.metrics == {"expected": 3, "compared": 3, "matching": 3}
    hub.placement_state["deployed"]["faustus"] = "b" * 40
    assert probes.deployment().outcome == "deployment_differs"



def test_console_orphan_probe_reads_only_metadata_from_existing_databases(hub, tmp_path, monkeypatch):
    from hoard_link.hub import agora_worker_processes

    path = tmp_path / "console.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE workers(id, pid, state, log_path, process_started_at, created_at, updated_at, parent_id, session_id, task)")
        db.execute("INSERT INTO workers VALUES('worker', 555, 'unknown', 'worker.jsonl', 1, 1, 2, 'parent', 'sid', ?)", (PRIVATE,))
        db.execute("INSERT INTO workers VALUES('legacy', 556, 'unknown', 'legacy.jsonl', NULL, 1, 2, 'parent', 'sid', ?)", (PRIVATE,))

    def inspect_orphan(row):
        assert row["pid"] == 555
        assert "task" not in row.keys()
        return {"age_seconds": 40, "private": PRIVATE}

    unopened = SimpleNamespace(_db_path=tmp_path / "unopened.db")
    console = SimpleNamespace(
        _db_lock=threading.RLock(), _db_path=path, _space_views={"unopened": unopened},
        _orphan_process=inspect_orphan, for_space=forbidden, overview=forbidden, _db=forbidden,
    )
    hub.facet_map["agora_console"] = console
    monkeypatch.setattr(agora_worker_processes, "live_pids", lambda: {555, 556})
    before = path.read_bytes()
    obs = LinkProbes(hub, request=forbidden, listeners=forbidden).console_orphans()
    assert obs.state == FAILED
    assert obs.metrics == {"spaces_checked": 1, "orphans": 1, "legacy_unverified": 1}
    assert PRIVATE not in repr(obs)
    assert path.read_bytes() == before
    assert {p.name for p in tmp_path.glob("*.db*")} == {"console.db"}


def test_mcp_checks_the_hub_url_before_opening_a_connection(monkeypatch):
    monkeypatch.setattr(mcp, "_url", lambda: "http://127.0.0.1:5232")
    monkeypatch.setattr(mcp, "_token", forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    assert mcp._hub_up() is False
    assert mcp._request("POST", "/api/agent/call", {"tool": "hub_link_check"}, passive=True) == (None, None)


def test_invalid_exclusion_settings_skip_probes_and_keep_baird_excluded(hub, monkeypatch):
    def probes(*args):
        forbidden()

    path = Path(hub.config.data_dir)
    path.mkdir()
    settings = path / "link-check.json"
    settings.write_text('{"excluded_apps": "atlas"}', encoding="utf-8")
    links = tuple(row for row in INVENTORY if row.id in {"1.7", "9.1"})
    monkeypatch.setattr(link_check, "LINKS", links)
    monkeypatch.setattr(link_check, "LinkProbes", probes)
    facet = link_check.LinkCheckFacet(hub)
    try:
        run = poll(facet, facet.call({})["run_id"])
        assert [(r["state"], r["evidence"]["outcome"]) for r in run["links"]] == [
            (UNCHECKED, "invalid_config"), (UNCHECKED, "operator_excluded"),
        ]
        assert settings.read_text(encoding="utf-8") == '{"excluded_apps": "atlas"}'
    finally:
        facet.close()



def test_native_compatibility_uses_module_catalogue_and_owner_listener(hub):
    hub.register("coase", 54901)
    alias = hub.register("midas", 5240)
    alias.family["absorbed_by"] = {"id": "coase", "module": "markets"}
    hub.register("writer", 54902)
    fiction = hub.register("scheherazade", 8766)
    fiction.family["absorbed_by"] = {"id": "writer", "module": "fiction"}
    called = []

    def transport(app, method, path, args, *, timeout):
        called.append((app.url, method, path))
        if app.id == "scheherazade":
            return 200, {"tools": [{"name": "wh_story_modules"}]}
        return 200, {"module": "markets", "tools": [{"name": "market_catalogue"}]}

    probes = LinkProbes(hub, request=transport, listeners=lambda: {54901, 54902})
    assert probes.compatibility("midas", now_deadline()).state == RESPONDS
    assert probes.compatibility("scheherazade", now_deadline()).state == RESPONDS
    assert called == [
        ("http://127.0.0.1:54901/compat/markets", "GET", "/api/agent/tools"),
        ("http://127.0.0.1:54902", "GET", "/api/agent/tools"),
    ]
    alias.family["absorbed_by"]["module"] = "wrong"
    assert probes.compatibility("midas", now_deadline()).outcome == "bridge_owner_missing"
    assert len(called) == 2


def test_cook_prerequisite_uses_catalogue_even_while_running(hub):
    hub.register("cookhoard", 5210)
    hub.register("kafka", 5200)
    called = []

    def transport(app, method, path, body, **kwargs):
        called.append((app.id, method, path))
        return 200, {"tools": [{"name": "catalogue_marker"}]}

    probes = LinkProbes(hub, request=transport, listeners=lambda: {5200, 5210})
    assert probes.health("cookhoard", now_deadline()).outcome == "health_route_not_reviewed"
    result = probes.run("prerequisites:cookhoard,kafka:cook_workflow", now_deadline(), {})
    assert (result.state, result.outcome) == (UNCHECKED, "cook_workflow")
    assert called == [
        ("cookhoard", "GET", "/api/agent/tools"),
        ("kafka", "GET", "/api/agent/tools"),
    ]


def run_inventory_rows(hub, monkeypatch, ids):
    links = tuple(link for link in LINKS if link.id in ids)
    assert {link.id for link in links} == set(ids)
    monkeypatch.setattr(link_check, "LINKS", links)
    monkeypatch.setattr(link_check, "app_links", lambda _: ())
    facet = link_check.LinkCheckFacet(hub)
    try:
        run_id = facet.call({})["run_id"]
        result = poll(facet, run_id)
        return ({row["id"]: row for row in result["links"]},
                {row["id"]: row for row in facet.status(run_id, "en")["links"]})
    finally:
        facet.close()


def test_literal_spark_links_ignore_local_manual_stops_and_refresh_passively(hub, tmp_path, monkeypatch):
    owner = "https://spark.example.test"
    hub.placement_state = {
        "location": "sparks", "node": {"name": "Spark 3"}, "since": time.time() - 10,
        "urls": {"hermes": f"{owner}:5221", "faustus": f"{owner}:7000", "prometheus": f"{owner}:5205",
                 "placed-app": f"{owner}:5019"},
    }
    hub.manually_stopped.update({"phileas", "tantalus", "hermes", "faustus", "prometheus", "placed-app"})
    hub.register("prometheus", 5205)
    hub.register("placed-app", 5019)
    for alias in ("phileas", "tantalus"):
        folder = tmp_path / alias
        folder.mkdir()
        hub.register(alias, 5221, prefix=f"/compat/{alias}", folder=folder)
        (folder / "bridge.json").write_text(json.dumps({
            "owner": "hermes", "tools": [{"schema": {"name": "fixture_mail_state"}}],
        }), encoding="utf-8")

    hub.placement._remote_health = {
        "checked": time.time() - 60, "ok": True, "apps": {
            "hermes": {"healthy": False, "service": "inactive"},
            "faustus": {"healthy": False, "service": "inactive"},
        },
    }
    refreshes = []

    def refresh(*, since):
        refreshes.append(since)
        hub.placement._remote_health = {
            "checked": time.time(), "ok": True, "apps": {
                app: {"healthy": True, "service": "active"} for app in hub.placement_state["urls"]
            },
        }
        return hub.placement._remote_health

    hub.placement.remote_health = refresh
    hub.facet_map.update(
        brain=SimpleNamespace(target=lambda: {"base": f"{owner}:7000"}, counts=lambda: {"done": 1}),
        fleet=SimpleNamespace(cached={"generated_ts": 200, "items": [
            {"owner": "phileas", "kind": "watcher", "id": "phileas:mail", "last_success_ts": 182, "state": "ok"},
            {"owner": "tantalus", "kind": "watcher", "id": "tantalus:mail", "last_success_ts": 175, "state": "ok"},
        ]}, status=forbidden),
    )
    requests = []

    def transport(app, method, path, args, **kwargs):
        assert method == "GET" and app.url.startswith(owner) and args is None
        requests.append((app.url, path))
        if path == "/api/health":
            return 200, {"service": "faustus"}
        if path == "/api/hoard-copilot/capabilities":
            return 200, {"contract_version": "1"}
        assert path == "/api/agent/tools"
        return 200, {"tools": [{"name": "fixture_mail_state", "description": PRIVATE}]}

    monkeypatch.setattr(link_probes, "_request_app", transport)
    monkeypatch.setattr(link_probes, "_listener_ports", forbidden)
    ids = {"1.17.tantalus", "1.17.phileas", "2.3", "2.4", "3.3", "3.9", "5.1", "5.6", "6.6", "6.10"}
    spanish, english = run_inventory_rows(hub, monkeypatch, ids)
    assert len(refreshes) == 1
    assert all(row["state"] == RESPONDS for row in spanish.values())
    for row_id in ids:
        assert spanish[row_id]["evidence"]["location"] == "remote"
        assert spanish[row_id]["status"] == "responde: en otra máquina"
        assert english[row_id]["status"] == "responds: on another machine"
    for alias in ("phileas", "tantalus"):
        assert (f"{owner}:5221/compat/{alias}", "/api/agent/tools") in requests
    assert spanish["2.3"]["evidence"]["metrics"]["last_success_at"] == 182
    assert spanish["2.4"]["evidence"]["metrics"]["watchers"] == 1
    assert spanish["2.4"]["evidence"]["metrics"]["last_success_at"] == 175
    assert spanish["3.9"]["evidence"]["metrics"]["watchers"] == 2
    assert LinkProbes(hub, request=transport, listeners=forbidden).catalogue("placed-app", now_deadline()).location == "remote"


def test_placed_catalogues_use_real_published_urls_and_refreshed_services(hub, tmp_path, monkeypatch):
    from hoard_link.hub.placement import PORTS, Placement

    health_calls = []

    def ssh(argv, **kwargs):
        assert argv[-2] == "fixture-node"
        command = argv[-1]
        health_calls.append(command)
        assert "baird" not in command and ":5232" not in command
        stdout = b"faustus active 200\nhermes active 200\nprometheus active 200\n"
        if ("systemctl --user is-active placed-extra.service" in command
                and "http://127.0.0.1:55021/portal/" in command):
            stdout += b"placed-extra active 200\n"
        return SimpleNamespace(returncode=0, stdout=stdout)

    placement = hub.placement = Placement(hub, runner=ssh)
    monkeypatch.setattr(placement, "_ssh_step", lambda command, **kwargs: (
        '{"BackendState":"Running","Self":{"DNSName":"fixture.example.test."}}'
        if command == "tailscale status --json 2>/dev/null || true" else forbidden(command)
    ))
    monkeypatch.setattr(placement, "_served_ports", lambda: {443, PORTS["hermes"], PORTS["prometheus"]})
    urls = placement._tailnet_urls()
    assert urls["hermes"] == "https://fixture.example.test:5221/products/"
    urls.update({
        "placed-extra": "https://fixture.example.test:55021/portal/",
        "baird": "https://fixture.example.test:5232/",
        "excluded-port-fixture": "https://fixture.example.test:5232/",
    })
    state = placement._update(location="sparks", since=time.time(), urls=urls,
                              node={"name": "Fixture", "ssh": "fixture-node"})
    placement._remote_health = {"checked": state["since"] - 60, "ok": True, "apps": {}}

    for alias in ("phileas", "tantalus"):
        folder = tmp_path / alias
        folder.mkdir()
        hub.register(alias, PORTS["hermes"], prefix=f"/compat/{alias}", folder=folder)
        (folder / "bridge.json").write_text(json.dumps({
            "owner": "hermes", "tools": [{"schema": {"name": "fixture_mail_state"}}],
        }), encoding="utf-8")
    hub.register("placed-extra", 55021)
    hub.manually_stopped.update({"hermes", "phileas", "tantalus", "placed-extra"})
    expected_urls = [
        "https://fixture.example.test:5221/compat/phileas/api/agent/tools",
        "https://fixture.example.test:5221/compat/tantalus/api/agent/tools",
        "https://fixture.example.test:55021/portal/api/agent/tools",
    ]
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            assert request.get_method() == "GET" and request.data is None and timeout > 0
            url = request.full_url
            requests.append(url)
            if url not in expected_urls:
                raise urllib.error.HTTPError(url, 404, "Fixture route not found", {}, io.BytesIO(b"{}"))
            response = io.BytesIO(b'{"tools":[{"name":"fixture_mail_state"}]}')
            response.status = 200
            return response

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    probes = LinkProbes(hub, listeners=forbidden)
    observations = [probes.compatibility(alias, time.monotonic() + link_check.PROBE_TIMEOUT_S)
                    for alias in ("phileas", "tantalus")]
    observations.append(probes.catalogue("placed-extra", time.monotonic() + link_check.PROBE_TIMEOUT_S))
    assert [(row.state, row.outcome, row.location) for row in observations] == [
        (RESPONDS, "catalogue_matches", "remote"),
    ] * 3
    assert requests == expected_urls
    assert len(health_calls) == 1
    assert set(placement._remote_health["apps"]) == {"faustus", "hermes", "prometheus", "placed-extra"}


def test_placement_refresh_skips_operator_excluded_services_and_aliases(hub):
    from hoard_link.hub.placement import Placement

    commands, requests = [], []

    def ssh(argv, **kwargs):
        assert argv[-2] == "fixture-node"
        commands.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout=(
            b"faustus active 200\nhermes active 200\nprometheus active 200\n"
            b"placed-fixture active 200\n"
        ))

    def transport(app, method, path, args, **kwargs):
        requests.append((app.url, method, path, args))
        return 200, {"tools": [{"name": "sample"}]}

    placement = hub.placement = Placement(hub, runner=ssh)
    urls = {
        "placed-fixture": "https://fixture.example.test:55021/portal/",
        "placed-skip": "https://fixture.example.test:55022/health/",
        "placed-alias": "https://fixture.example.test:55023/alias/",
    }
    state = placement._update(location="sparks", since=time.time(), urls=urls,
                              node={"name": "Fixture", "ssh": "fixture-node"})
    previous = {"checked": state["since"] - 60, "ok": True, "apps": {}}
    placement._remote_health = previous
    hub.register("placed-fixture", 55021)
    hub.register("placed-skip", 55022)
    hub.register("placed-alias", 55023).family["absorbed_by"] = {"id": "placed-skip"}
    (Path(hub.config.data_dir) / "link-check.json").write_text(
        '{"excluded_apps":["placed-skip"]}', encoding="utf-8",
    )
    facet = link_check.LinkCheckFacet(hub)
    try:
        probes = LinkProbes(hub, excluded=facet._settings(), request=transport, listeners=forbidden)
    finally:
        facet.close()

    for excluded in ("placed-skip", "placed-alias"):
        assert probes.catalogue(excluded, now_deadline()).outcome == "operator_excluded"
    assert not commands and not requests
    row = probes.run("placement", now_deadline(), {})
    assert row.state == RESPONDS and row.metrics["services"] == row.metrics["healthy_http"] == 4
    assert probes.catalogue("placed-fixture", now_deadline()).state == RESPONDS
    assert len(commands) == 1
    assert "http://127.0.0.1:55021/portal/" in commands[0]
    for name in ("placed-skip.service", "placed-alias.service", "55022", "55023"):
        assert name not in commands[0]
    assert placement._remote_health is previous
    assert requests == [
        ("https://fixture.example.test:55021/portal", "GET", "/api/agent/tools", None),
    ]


def test_literal_local_manual_and_not_started_rows_are_distinct(hub, monkeypatch):
    hub.register("cookhoard", 5210)
    hub.register("ledger", 5180)
    hub.register("atlas", 5203)
    hub.manually_stopped.update({"cookhoard", "ledger", "faustus"})
    monkeypatch.setattr(link_probes, "_listener_ports", lambda: {5210, 5180, 7000})
    monkeypatch.setattr(link_probes, "_request_app", forbidden)
    es, en = run_inventory_rows(hub, monkeypatch, {"4.2", "2.5", "6.6", "1.7"})
    for identity in ("4.2", "2.5", "6.6"):
        assert es[identity]["state"] == STOPPED
        assert es[identity]["evidence"]["outcome"] == "manually_stopped"
        assert es[identity]["status"] == "parado: parada manual"
        assert en[identity]["status"] == "stopped: manual stop"
    assert es["1.7"]["state"] == STOPPED
    assert es["1.7"]["evidence"]["outcome"] == "no_listener"
    assert es["1.7"]["status"] == "parado: sin iniciar"
    assert en["1.7"]["status"] == "stopped: not started"


def test_literal_mail_rows_see_previous_process_receipt_twenty_seconds_after_restart(hub, monkeypatch):
    data = Path(hub.config.data_dir)
    data.mkdir()
    (data / "mail.json").write_text('{"enabled":true,"interval_min":10}', encoding="utf-8")
    first_process = MailStore(str(data / "mail.db"))
    first_process.set_state("last_ok_ts", 995.0)
    first_process.set_state("accounts", [PRIVATE])
    first_process.close()
    hub.started_at = 1000
    gate = MailGate(hub, clock=lambda: 1020)
    gate.call_helper = forbidden
    hub.facet_map["mailgate"] = gate
    saved = (data / "mail.db").read_bytes()
    ids = {"2.1", "2.13", "4.5.mail"}
    recovered, _ = run_inventory_rows(hub, monkeypatch, ids)
    for row in recovered.values():
        assert row["state"] == RESPONDS
        assert row["evidence"]["outcome"] == "mail_fresh"
        assert row["evidence"]["metrics"]["last_success_at"] == 995
        assert row["evidence"]["metrics"]["age_s"] == 25
    assert gate._store is None and (data / "mail.db").read_bytes() == saved
    assert not (data / "mail.db-wal").exists() and not (data / "mail.db-shm").exists()
    assert PRIVATE not in repr(recovered)

    (data / "mail.db").unlink()
    es, en = run_inventory_rows(hub, monkeypatch, ids)
    for identity in ids:
        assert es[identity]["state"] == RESPONDS
        assert es[identity]["evidence"]["scope"] == "gateway_state"
        assert es[identity]["evidence"]["detail"] == "Sin pasada desde el arranque (hace 20 s)."
        assert en[identity]["evidence"]["detail"] == "No pass since startup (20 s ago)."
        assert "last_success_at" not in es[identity]["evidence"]["metrics"]
    assert gate._store is None and not (data / "mail.db").exists()


def test_literal_reader_caller_dojo_and_quota_rows_read_only_operational_metadata(hub, tmp_path, monkeypatch):
    hub.token = "fixture-hub-token"
    hub.token_owner = {"fixture-helper-token": "kafka"}.get
    hub.config.person_profile = {"id": "fixture-operator"}
    hub.facet_map["spheres"] = SimpleNamespace(app_allowed=lambda sphere, who: sphere == "personal",
                                               list=lambda: [{"id": "personal"}])
    gate = MailGate(hub, clock=lambda: 1020)
    installation = tmp_path / "faustus" / "data"
    installation.mkdir()
    (installation / "mail_installation.json").write_text(PRIVATE, encoding="utf-8")
    (installation / "mail_callers.json").write_text(PRIVATE, encoding="utf-8")
    gate.faustus_dir = lambda: installation.parent
    gate.call_helper = forbidden
    hub.facet_map["mailgate"] = gate

    try:
        gate.store.insert_message(subject=PRIVATE, text=PRIVATE, message_id="fixture-only",
                                  interests=["kafka", "people"])
        for reader in ("kafka", "people"):
            request = facets.Request("GET", "/api/mail/messages", {}, {}, lambda: reader, forbidden)
            assert gate.get(request)["count"] == 1
        probes = LinkProbes(hub, request=forbidden, listeners=forbidden)
        people = probes.gateway_reader("people", now_deadline())
        assert people.state == RESPONDS and people.metrics["last_scan_at"] == 1020
        denied = facets.Request("GET", "/api/mail/messages", {"sphere": ["work"]}, {}, lambda: "people", forbidden)
        assert gate.get(denied)["status"] == 403

        handler = _HubHandler.__new__(_HubHandler)
        handler.hub = hub
        handler._json = lambda payload, status=200: status
        handler.headers = {"Authorization": "Bearer rejected"}
        assert handler._mail_helper_caller() == 401 and gate.last_caller_at is None
        handler.headers = {"Authorization": "Bearer fixture-helper-token"}
        assert handler._mail_helper_caller() == 200 and gate.last_caller_at == 1020

        class Response(io.StringIO):
            pass

        class Opener:
            def open(self, request, timeout):
                assert request.full_url == "http://127.0.0.1:7000/api/dojo"
                assert request.get_method() == "GET"
                return Response(json.dumps({"exams": [{"title": PRIVATE}, {"title": PRIVATE}]}))

        monkeypatch.setattr(dojo.where, "reachable", lambda urls: urls[0])
        monkeypatch.setattr(dojo, "build_opener", lambda *_: Opener())
        assert len(dojo.call(hub, {"operation": "list"}, "fixture-agent")["exams"]) == 2
        monkeypatch.setattr(dojo, "build_opener", forbidden)

        with closing_quota_db(hub) as db:
            db.executemany("INSERT INTO agora_usage_pools VALUES (?, ?, ?, ?, ?)", [
                ("codex", 1019, PRIVATE, PRIVATE, json.dumps([{"label": PRIVATE, "measurement": "measured",
                                                               "remaining_percent": 87}])),
                ("claude", 1018, PRIVATE, PRIVATE, json.dumps([{"label": PRIVATE, "measurement": "unknown"}])),
                ("cursor", 950, PRIVATE, PRIVATE, json.dumps([{"measurement": "measured",
                                                              "reset_at": "1970-01-01T00:16:40+00:00"}])),
            ])
            db.set_authorizer(quota_metadata_only)
            monkeypatch.setattr(link_probes, "_listener_ports", lambda: {7000})
            monkeypatch.setattr(link_probes, "_request_app", forbidden)
            rows, _ = run_inventory_rows(hub, monkeypatch, {"2.2", "2.6", "2.10", "6.5", "7.3"})
            assert rows["2.2"]["state"] == RESPONDS
            assert rows["2.2"]["evidence"]["metrics"] == {"last_scan_at": 1020, "messages_returned": 1}
            assert rows["2.6"]["state"] == FAILED and rows["2.6"]["evidence"]["outcome"] == "mail_reader_scan_failed"
            assert rows["2.10"]["evidence"]["metrics"] == {
                "installation_exists": True, "registry_exists": True, "last_used_at": 1020,
            }
            assert rows["6.5"]["evidence"]["metrics"]["exams"] == 2
            quota = rows["7.3"]["evidence"]
            assert quota["outcome"] == "quota_measured"
            assert quota["metrics"]["codex_fresh"] and quota["metrics"]["codex_measured_at"] == 1019
            assert not quota["metrics"]["claude_fresh"] and quota["metrics"]["claude_measured_at"] is None
            assert not quota["metrics"]["cursor_fresh"] and quota["metrics"]["cursor_measured_at"] == 950
            assert PRIVATE not in json.dumps(rows)
    finally:
        gate.close()


def closing_quota_db(hub):
    from contextlib import closing

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE agora_usage_pools(provider_id, reported_at, pool_id, source, windows_json)")
    agora = SimpleNamespace(clock=lambda: 1020.0, db=SimpleNamespace(query=lambda *args: db.execute(*args).fetchall()),
                            _USAGE_PROVIDERS={"codex", "claude", "cursor", "faustus"}, _USAGE_STALE_S=60)
    hub.facet_map["agora"] = SimpleNamespace(agora=agora)
    return closing(db)


def quota_metadata_only(action, _table, key, *_):
    if action == sqlite3.SQLITE_READ and key in {"pool_id", "source"}:
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_UPDATE}:
        # SQLite's json_each virtual table calls authorizer for internal schema setup.
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def test_common_dictation_link_only_reads_the_registered_catalogue(hub, monkeypatch):
    hub.register("boltagon", 55026)
    expected = [
        "dictation_understand", "dictation_audio", "dictation_session_open", "dictation_session_get",
        "dictation_session_turn", "dictation_session_fix", "dictation_session_cancel", "dictation_session_undo",
    ]
    advertised = expected.copy()
    calls = []

    def transport(app, method, path, args, **kwargs):
        assert (app.id, method, path, args) == ("boltagon", "GET", "/api/agent/tools", None)
        calls.append(app.id)
        return 200, {"tools": [{"name": name} for name in advertised]}

    monkeypatch.setattr(link_probes, "_listener_ports", lambda: {55026})
    monkeypatch.setattr(link_probes, "_request_app", transport)
    rows, _ = run_inventory_rows(hub, monkeypatch, {"4.5.dictation"})
    checked = rows["4.5.dictation"]
    assert checked["destination"] == "boltagon" and checked["state"] == RESPONDS
    assert checked["evidence"]["metrics"]["matched_tools"] == len(expected)
    advertised.remove("dictation_session_undo")
    probes = LinkProbes(hub, request=transport, listeners=lambda: {55026})
    missing = probes.service("dictation", now_deadline())
    assert missing.state == FAILED and missing.outcome == "missing_tools"
    hub.manually_stopped.add("boltagon")
    assert probes.service("dictation", now_deadline()).outcome == "manually_stopped"
    assert calls == ["boltagon", "boltagon"]


def test_literal_funes_catalogue_wins_over_foreign_lifecycle_state(hub, monkeypatch):
    from hoard_link import _famsvc

    hub.register("talos", 55120)
    hub.register("funes", 55121, prefix="/compat/funes")
    monkeypatch.setattr(_famsvc, "_cache", {})
    monkeypatch.setattr(_famsvc._f, "_hub", lambda: "http://fixture.invalid")
    monkeypatch.setattr(_famsvc._f, "_token", lambda: "fixture-token")
    monkeypatch.setattr(_famsvc._f, "_headers", lambda: {})
    def fetch(url, **kwargs):
        if url.endswith("/api/services/status"):
            return 200, {"services": [{"service": "stt", "available": True, "owner": "funes"}]}
        if url.endswith("/api/apps/funes"):
            return 200, {"state": "foreign"}
        forbidden()

    monkeypatch.setattr(_famsvc, "fetch", fetch)
    monkeypatch.setattr(_famsvc._f, "call", lambda owner, tool, args, **kw: {
        "ok": owner == "funes", "status": 200, "result": {"fixture": True},
    })
    assert _famsvc.available("stt")
    assert _famsvc.call_tool("funes", "transcribe_status", {"job_id": "fixture"})["ok"]

    required = link_probes.SERVICE_SPECS["stt"][1]
    tools_returned = [{"name": name} for name in required]

    def transport(app, method, path, args, **kw):
        assert app.url == "http://127.0.0.1:55120/compat/funes"
        assert (method, path, args) == ("GET", "/api/agent/tools", None)
        return 200, {"tools": tools_returned}

    monkeypatch.setattr(link_probes, "_listener_ports", lambda: {55120})
    monkeypatch.setattr(link_probes, "_request_app", transport)
    rows, _ = run_inventory_rows(hub, monkeypatch, {"4.5.stt"})
    row = rows["4.5.stt"]
    assert row["state"] == RESPONDS and row["evidence"]["outcome"] == "legacy_service_catalogue"
    tools_returned.pop()
    assert LinkProbes(hub, request=transport, listeners=lambda: {55120}).service("stt", now_deadline()).outcome == "missing_tools"


@pytest.mark.parametrize("state, provider", [
    ("ready", "boltagon"), ("down", "funes"), ("missing_tool", "funes"),
])
def test_media_and_voice_catalogues_cover_before_and_after_owner_change(hub, state, provider):
    hub.register("boltagon", 55451)
    hub.register("talos", 55452)
    hub.register("funes", 55453, prefix="/compat/funes")
    hub.register("borges", 55454)
    hub.register("writer", 55454)
    required = link_probes.SERVICE_SPECS["stt"][1]
    visited = []

    def catalogue(app, method, path, arguments, **kwargs):
        assert (method, path, arguments) == ("GET", "/api/agent/tools", None)
        visited.append(app.id)
        names = ("library_reindex",) if app.id == "borges" else required
        if app.id == "boltagon" and state == "missing_tool":
            names = ("transcribe_file",)
        return 200, {"tools": [{"name": tool} for tool in names]}

    ports = {55452, 55454} | ({55451} if state != "down" else set())
    probe = LinkProbes(hub, request=catalogue, listeners=lambda: ports)
    voice = probe.service("stt", now_deadline())
    media = probe.run("media_owners", now_deadline(), {})
    assert voice.state == media.state == RESPONDS
    assert voice.called.startswith(f"{provider} GET ")
    assert media.called.startswith(f"{provider} GET ") and media.metrics["stt_fallback"] == int(provider == "funes")
    assert "borges" in visited
    assert ("funes" in visited) == (provider == "funes")


@pytest.mark.parametrize("budget", [2.0, 8.0])
@pytest.mark.parametrize("probe_name", ["service:stt", "media_owners"])
def test_slow_primary_leaves_budget_for_funes_and_indexing(hub, monkeypatch, budget, probe_name):
    primary = hub.register("boltagon", 55461)
    hub.registry["cyrano"] = primary  # both IDs resolve to the same installed manifest
    hub.register("talos", 55462)
    hub.register("funes", 55463, prefix="/compat/funes")
    hub.register("writer", 55464)
    hub.register("borges", 55464)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(link_probes, "time", SimpleNamespace(monotonic=lambda: clock.now))
    visited = []

    def catalogue(app, method, path, arguments, *, timeout):
        assert (method, path, arguments) == ("GET", "/api/agent/tools", None)
        assert timeout > 0 and clock.now + timeout <= budget
        visited.append((app.id, timeout))
        if app.id == "boltagon":
            clock.now += timeout
            return None, {"transport": "timeout"}
        names = ("library_reindex",) if app.id == "borges" else link_probes.SERVICE_SPECS["stt"][1]
        return 200, {"tools": [{"name": tool} for tool in names]}

    probe = LinkProbes(hub, request=catalogue, listeners=lambda: {55461, 55462, 55464})
    row = probe.run(probe_name, budget, {})
    assert row.state == RESPONDS and row.called.startswith("funes GET ")
    assert visited[0][1] <= 2.0
    assert [name for name, _ in visited] == (["boltagon", "funes", "borges"] if probe_name == "media_owners"
                                               else ["boltagon", "funes"])
    if probe_name == "media_owners":
        assert row.metrics["stt_fallback"] == 1


def test_literal_vendored_copies_distinguish_versions_and_unpublished_drift(hub, tmp_path, monkeypatch):
    import shutil

    from hoard_link.hub.repos import run_git

    if shutil.which("git") is None:
        pytest.skip("Git is required to build a synthetic repository")
    root = tmp_path / "canonical"
    root.mkdir()
    package = root / "hoard_link"
    package.mkdir()
    js = root / "js"
    js.mkdir()
    python_file = package / "fam_mail.py"
    node_file = js / "hoard-link.js"

    def git(*args):
        result = run_git(str(root), *args)
        assert result.ok, result.err
        return result.out

    git("init", "-q")
    for version in range(3):
        python_file.write_bytes(f"python version {version}\n".encode())
        node_file.write_bytes(f"node version {version}\n".encode())
        git("add", "--", "hoard_link/fam_mail.py", "js/hoard-link.js")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "-c", "commit.gpgsign=false", "commit", "-qm", f"Publish {version}")
    monkeypatch.setattr(link_probes, "__file__", str(package / "hub" / "link_probes.py"))

    app_dir = tmp_path / "consumer"
    py_copy = app_dir / "hoard_link"
    py_copy.mkdir(parents=True)
    (py_copy / "__init__.py").touch()
    node_copy = app_dir / "server"
    node_copy.mkdir()
    (py_copy / "fam_mail.py").write_bytes(b"python version 1\n")
    (node_copy / "hoard-link.js").write_bytes(b"node version 2\n")
    hub.register("mail-consumer-fixture", 55000, folder=app_dir)

    es, en = run_inventory_rows(hub, monkeypatch, {"2.9"})
    row = es["2.9"]
    assert row["state"] == RESPONDS and row["evidence"]["outcome"] == "vendor_copies_behind"
    assert row["evidence"]["metrics"]["versions_behind_1"] == 1
    assert row["evidence"]["metrics"]["unmatched"] == 0
    assert "atrasadas 1 versión" in row["evidence"]["detail"]
    assert "1 version behind" in en["2.9"]["evidence"]["detail"]

    (node_copy / "hoard-link.js").write_bytes(b"node version 0\n")
    probes = LinkProbes(hub, request=forbidden, listeners=forbidden)
    check = probes.mail_library(time.monotonic() + link_check.PROBE_TIMEOUT_S)
    assert check.metrics["versions_behind_1"] == check.metrics["versions_behind_2"] == 1
    (node_copy / "hoard-link.js").write_bytes(b"node version 0\r\n")
    check = probes.mail_library(time.monotonic() + link_check.PROBE_TIMEOUT_S)
    assert check.state == FAILED and check.metrics["unmatched"] == 1
    assert check.metrics["versions_behind_1"] == 1
    assert git("status", "--porcelain") == ""
