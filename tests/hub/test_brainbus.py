"""The bus writes to the Brain: the ``brain`` rule action, its queue, the fallback without entities, HTTP and tools."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from hoard_link.hub import actions, brainbus, tools
from hoard_link.hub.rules import example_rules

from ._hub_fakes import http, make_hub, serve, wait_for
from .conftest import free_port


class FakeBrain:
    """Faustus's ``/api/brain`` in memory: notes by path, entities by folded name. ``down`` answers 503;
    ``entities_api = False`` is a Faustus without the route to create entities."""

    def __init__(self) -> None:
        self.notes: dict[str, str] = {}
        self.entities: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.entities_api = True
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _entity(self, name, etype):
                return outer.entities.setdefault(name.casefold(), {
                    "id": f"e{len(outer.entities) + 1}", "name": name, "type": etype or "other", "summary": "",
                    "mentions": [], "relations": []})

            def _handle(self):
                url = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.calls.append((self.command, url.path))
                if outer.down:
                    return self._json({"detail": "starting"}, 503)
                if url.path == "/api/brain/status":
                    return self._json({"enabled": True, "notes": len(outer.notes), "entities": len(outer.entities)})
                if url.path == "/api/brain/note" and self.command == "GET":
                    path = parse_qs(url.query).get("path", [""])[0]
                    if path not in outer.notes:
                        return self._json({"detail": "No such note"}, 404)
                    return self._json({"path": path, "content": outer.notes[path]})
                if url.path == "/api/brain/note" and self.command == "PUT":
                    outer.notes[body["path"]] = body["content"]
                    return self._json({"note": {"path": body["path"]}, "applied": {}})
                if url.path == "/api/brain/entities" and self.command == "POST":
                    if not outer.entities_api:
                        return self._json({"detail": "Method Not Allowed"}, 405)
                    name = str(body.get("name") or "").strip()
                    if not name:
                        return self._json({"detail": "name is required"}, 400)
                    created = name.casefold() not in outer.entities
                    ent = self._entity(name, body.get("type"))
                    ent["summary"] = body.get("summary") or ent["summary"]
                    for ref in body.get("mentions") or []:
                        if ref not in ent["mentions"]:
                            ent["mentions"].append(ref)
                    for rel in body.get("relations") or []:
                        other = self._entity(str(rel["to"]), rel.get("to_type"))
                        edge = {"rel": rel["rel"], "dst": other["id"]}
                        if edge not in ent["relations"]:
                            ent["relations"].append(edge)
                    return self._json({"entity": ent, "created": created, "relations": ent["relations"],
                                       "path": f"Entities/{ent['type'].capitalize()}/{ent['name']}.md"})
                return self._json({"detail": "Not Found"}, 404)

            do_GET = do_PUT = do_POST = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def factory(rule_id: str) -> dict:
    return next(r for r in brainbus.factory_rules() if r["id"] == rule_id)


@pytest.fixture
def brain():
    b = FakeBrain()
    yield b
    b.stop()


@pytest.fixture
def hub(tmp_path, brain):
    h = make_hub(tmp_path)
    facet = h.facet("brain")
    facet.retry_steps = (0.05,)
    facet.set_target(brain.url)
    yield h
    h.close()


def settled(facet, n: int = 1):
    """Wait until ``n`` writes left the queue (done, partial or failed)."""
    return wait_for(lambda: sum(v for k, v in facet.counts().items() if k != "pending") >= n and not facet.counts()["pending"], timeout=20)


# ---- templates ---------------------------------------------------------------------------------------------------
def test_templates_fall_back_drop_empty_lines_and_join_lists():
    ctx = {"event": {"type": "x.y", "source": "x", "data": {"merchant": "Tienda Norte", "order_ref": "", "id": 7.0,
                                                               "items": ["Zapatillas", "Cordones"], "carrier": ""}}}
    assert brainbus.render("Envío ${event.data.merchant} ${event.data.order_ref|event.data.id}", ctx) == "Envío Tienda Norte 7"
    assert brainbus.render("${event.data.nope|'sin nombre'}", ctx) == "sin nombre"
    assert brainbus.render_lines(["Artículos: ${event.data.items}", "Transportista: ${event.data.carrier}", "fijo"], ctx) == [
        "Artículos: Zapatillas, Cordones", "fijo"]
    assert brainbus.safe_title('a/b: "c" [d]') == "a b c d"


def test_every_recommended_rule_is_valid_and_its_sample_writes_something():
    ids = {r["id"] for r in example_rules()}
    for rule in brainbus.factory_rules():
        assert rule["id"] in ids and rule["cooldown_s"] == 0
        assert actions.validate(rule["then"]) == []
        sample = rule["sample"]
        ctx = actions.base_context(event={"type": sample["type"], "source": sample["type"].split(".")[0], "data": sample["data"]},
                                   rule={"id": rule["id"]})
        plan = brainbus.build_plan(rule["then"][0], ctx)
        assert "skip" not in plan and plan["key"] and "${" not in json.dumps(plan), rule["id"]
    assert "sample" not in example_rules()[-1]
    assert actions.validate([{"kind": "brain"}]) and actions.validate([{"kind": "brain", "note": {"text": "x"}}])


def test_an_event_without_the_name_writes_nothing(hub):
    facet = hub.facet("brain")
    rule = factory("rule-brain-people-commitment")
    res = facet.submit(rule["then"][0], actions.base_context(event={"type": "people.commitment.noted", "data": {"text": "algo"}}))
    assert res["ok"] and res["skipped"] and facet.counts()["pending"] == 0


# ---- the rule end to end ---------------------------------------------------------------------------------------------
def test_event_creates_entity_and_note_once_and_a_later_event_updates(hub, brain):
    facet = hub.facet("brain")
    assert hub.rules.add({k: v for k, v in factory("rule-brain-people-commitment").items() if k != "sample"})["ok"]
    data = {"id": 41, "person_id": 7, "person": "Marta Ibarra", "text": "Enviarle el presupuesto", "due": "2026-11-20"}
    hub.events.emit("people.commitment.noted", data, source="people")
    assert settled(facet, 1)
    path = "Familia/Compromisos/Compromisos con Marta Ibarra.md"
    assert list(brain.notes) == [path]
    note = brain.notes[path]
    assert "Enviarle el presupuesto · vence 2026-11-20" in note and "[[Marta Ibarra]]" in note and "hub_key:" in note
    marta = brain.entities["marta ibarra"]
    assert marta["type"] == "person" and marta["mentions"] == [f"note:{path}"]

    # the same event again: nothing is queued, nothing is written
    writes_before = len([c for c in brain.calls if c[0] in ("PUT", "POST")])
    hub.events.emit("people.commitment.noted", data, source="people")
    assert wait_for(lambda: (facet._thing("people:person:7") or {}).get("repeats") == 1)
    assert len(facet.history()) == 1 and len([c for c in brain.calls if c[0] in ("PUT", "POST")]) == writes_before
    assert len(brain.entities) == 1 and brain.notes[path] == note

    # a later commitment with the same person: same entity, same note, one more line
    hub.events.emit("people.commitment.noted", {**data, "id": 42, "text": "Llamarla el lunes", "due": ""}, source="people")
    assert settled(facet, 2)
    assert list(brain.notes) == [path] and len(brain.entities) == 1
    assert brain.notes[path].count("\n- ") == 2 and "Llamarla el lunes" in brain.notes[path]
    hist = facet.history()
    assert [w["status"] for w in hist] == ["done", "done"] and hist[0]["url"].startswith(brain.url + "/brain?note=Familia/")
    assert hub.events.query(type="hub.brain.written")[0]["data"]["entity"] == "Marta Ibarra"


def test_note_links_to_the_shop_and_replace_mode_keeps_hand_edits(hub, brain):
    facet = hub.facet("brain")
    ship = factory("rule-brain-shipment")
    ctx = lambda t, d: actions.base_context(event={"id": 1, "type": t, "source": "phileas", "data": d}, rule={"id": ship["id"]})  # noqa: E731
    base = {"shipment_id": "s1", "merchant": "Tienda Norte", "order_ref": "4471", "items": ["Zapatillas"], "carrier": "Correos"}
    assert facet.submit(ship["then"][0], ctx("phileas.shipment.new", base), wait=20)["status"] == "done"
    path = "Familia/Envíos/Envío Tienda Norte 4471.md"
    assert "[[Tienda Norte]]" in brain.notes[path] and brain.entities["tienda norte"]["type"] == "organization"
    assert brain.entities["tienda norte"]["mentions"] == [f"note:{path}"]
    out = facet.submit(ship["then"][0], ctx("phileas.shipment.delivered", {**base, "delivered_at": "2026-11-12T10:00:00"}), wait=20)
    assert out["status"] == "done" and "Entregado el 2026-11-12T10:00:00" in brain.notes[path] and len(brain.notes) == 1

    deadline = factory("rule-brain-kafka-deadline")
    dctx = lambda due: actions.base_context(event={"type": "kafka.deadline.created", "source": "kafka", "data": {  # noqa: E731
        "deadline_id": "t1", "title": "Fin de la garantía", "due": due, "kind": "warranty_end"}})
    assert facet.submit(deadline["then"][0], dctx("2029-11-09"), wait=20)["status"] == "done"
    dpath = "Familia/Plazos/Fin de la garantía.md"
    assert "date: 2029-11-09" in brain.notes[dpath] and "Vence: 2029-11-09" in brain.notes[dpath]
    brain.notes[dpath] = brain.notes[dpath] + "\nMi apunte a mano.\n"
    assert facet.submit(deadline["then"][0], dctx("2029-12-01"), wait=20)["status"] == "done"
    assert "- Vence: 2029-12-01\n- Tipo: warranty_end" in brain.notes[dpath] and "Vence: 2029-11-09" not in brain.notes[dpath]
    assert "date: 2029-12-01" in brain.notes[dpath] and "date: 2029-11-09" not in brain.notes[dpath]
    assert "Mi apunte a mano." in brain.notes[dpath] and brain.notes[dpath].count("Vence:") == 1
    # the hub's text was rewritten by hand: the next version goes at the end, the hand-written one stays
    brain.notes[dpath] = brain.notes[dpath].replace("Vence: 2029-12-01", "Vence (lo cambié yo): enero")
    assert facet.submit(deadline["then"][0], dctx("2030-01-15"), wait=20)["status"] == "done"
    final = brain.notes[dpath]
    assert 0 < final.index("lo cambié yo") < final.index("Mi apunte a mano.") < final.index("Vence: 2030-01-15")
    assert "<!--" not in final and final.count("Vence: 2030-01-15") == 1


# ---- the queue ---------------------------------------------------------------------------------------------------------
def test_when_faustus_does_not_answer_the_write_waits_and_lands_later(hub, brain):
    facet = hub.facet("brain")
    brain.down = True
    rule = factory("rule-brain-kafka-deadline")
    ctx = actions.base_context(event={"type": "kafka.deadline.created", "data": {"deadline_id": "t9", "title": "Plazo de devolución", "due": "2026-12-01"}})
    out = facet.submit(rule["then"][0], ctx, wait=0.6)
    assert out["ok"] and out["status"] == "pending" and "no responde" in out["error"]
    again = facet.submit(rule["then"][0], ctx)          # the same thing while it waits is not queued twice
    assert again["duplicate"] and again["write_id"] == out["write_id"]
    assert facet.counts()["pending"] == 1 and not brain.notes and facet.status()["last_error"]
    brain.down = False
    facet.retry()
    assert settled(facet, 1) and facet.counts()["done"] == 1
    assert "Familia/Plazos/Plazo de devolución.md" in brain.notes


def test_without_the_entity_route_the_note_is_written_and_the_entity_follows(hub, brain):
    facet = hub.facet("brain")
    brain.entities_api = False
    rule = factory("rule-brain-people-commitment")
    ctx = actions.base_context(event={"type": "people.commitment.noted", "data": {"person_id": 3, "person": "Pablo Sanz", "text": "Devolverle el libro"}})
    out = facet.submit(rule["then"][0], ctx, wait=20)
    assert out["status"] == "partial" and out["result"]["entities_pending"] and not brain.entities
    assert "Devolverle el libro" in brain.notes["Familia/Compromisos/Compromisos con Pablo Sanz.md"]
    # an entity on its own becomes a card note until the route exists
    only = {"kind": "brain", "key": "k:ana", "entity": {"name": "Ana Ruiz", "type": "person", "summary": "Vecina"}}
    assert facet.submit(only, actions.base_context(event={"type": "x.y", "data": {}}), wait=20)["status"] == "partial"
    assert "Vecina" in brain.notes["Familia/Personas/Ana Ruiz.md"]

    brain.entities_api = True
    facet._requeue_partial(brain.url)
    assert wait_for(lambda: facet.counts()["partial"] == 0 and facet.counts()["pending"] == 0, timeout=20)
    assert set(brain.entities) == {"pablo sanz", "ana ruiz"} and facet.counts()["done"] == 2
    assert len(brain.notes) == 2          # the same notes, not new ones


def test_a_refused_write_is_marked_failed_and_does_not_block_the_next(hub, brain, monkeypatch):
    facet = hub.facet("brain")
    bad = {"kind": "brain", "key": "k:bad", "note": {"title": "Rota", "text": "x"}}
    good = {"kind": "brain", "key": "k:good", "note": {"title": "Buena", "text": "y"}}
    real_write = brainbus.BrainClient.write_note
    refuse = {"on": True}

    def write(self, path, content):
        if refuse["on"] and "Rota" in path:
            raise brainbus.Rejected("escribir: HTTP 400: ruta inválida")
        return real_write(self, path, content)

    monkeypatch.setattr(brainbus.BrainClient, "write_note", write)
    ctx = actions.base_context(event={"type": "x.y", "data": {}})
    assert facet.submit(bad, ctx, wait=20)["status"] == "failed"
    assert facet.submit(good, ctx, wait=20)["status"] == "done"
    failed = facet.history(status="failed")[0]
    assert failed["error"].startswith("escribir: HTTP 400")
    refuse["on"] = False
    assert facet.retry(failed["id"])["requeued"] == 1 and settled(facet, 2)
    assert "Familia/Rota.md" in brain.notes and facet.counts()["failed"] == 0


# ---- where Faustus is ----------------------------------------------------------------------------------------------------
def test_target_follows_the_placement_unless_an_address_is_fixed(hub, brain, monkeypatch):
    facet = hub.facet("brain")
    assert facet.target() == {"base": brain.url, "source": "fixed", "moving": False}
    assert facet.set_target("")["ok"]
    assert facet.target()["source"] == "pc" and facet.target()["base"] == "http://127.0.0.1:1"
    state = {"location": "sparks", "node": {"name": "Spark3"}, "urls": {"faustus": "https://spark3.example.ts.net/"}, "job": {"running": False}}
    monkeypatch.setattr(hub.placement, "state", lambda: state)
    assert facet.target() == {"base": "https://spark3.example.ts.net", "source": "placement", "node": "Spark3", "moving": False}
    state["job"] = {"running": True}
    assert facet.target()["moving"] is True
    # while Faustus moves the write waits; once it is back it lands
    out = facet.submit({"kind": "brain", "key": "k:mv", "note": {"title": "Durante el traslado", "text": "z"}},
                       actions.base_context(event={"type": "x.y", "data": {}}), wait=0.4)
    assert out["status"] == "pending" and not brain.notes
    state["job"] = {"running": False}
    state["urls"] = {"faustus": brain.url}
    facet.retry()
    assert settled(facet, 1) and "Familia/Durante el traslado.md" in brain.notes
    assert facet.set_target("ftp://x")["ok"] is False


# ---- HTTP and tools ----------------------------------------------------------------------------------------------------------
def test_http_preview_write_history_and_rule_switch(hub, brain):
    server = serve(hub)
    base = hub.config.url
    auth = {"Authorization": "Bearer " + hub.token}
    try:
        status, body = http(base + "/api/brain?probe=1")
        assert status == 200 and body["target"]["reachable"] is True and body["target"]["entities_api"] is True
        rules = {r["id"]: r for r in body["rules"]}
        assert rules["rule-brain-shipment"]["installed"] is False and rules["rule-brain-shipment"]["sample"]["data"]["merchant"]
        assert http(base + "/api/brain/test", {"rule_id": "rule-brain-shipment"})[0] == 401

        status, body = http(base + "/api/brain/test", {"rule_id": "rule-brain-shipment"}, auth)
        assert status == 200 and body["matches"] is True
        w = body["writes"][0]
        assert w["effect"] == "create" and w["note"]["path"] == "Familia/Envíos/Envío Tienda Norte 4471.md"
        assert "Transportista: Correos" in w["note"]["text"] and w["links"] == [{"to": "Tienda Norte", "type": "organization", "rel": "related_to"}]
        assert not brain.notes and not hub.facet("brain").history()          # a preview writes nothing

        status, body = http(base + "/api/brain/test", {"rule_id": "rule-brain-shipment", "write": True}, auth)
        assert status == 200 and body["writes"][0]["written"]["status"] == "done"
        assert body["writes"][0]["written"]["url"].startswith(brain.url + "/brain?note=")
        status, body = http(base + "/api/brain/test", {"rule_id": "rule-brain-shipment", "write": True}, auth)
        assert body["writes"][0]["effect"] == "unchanged" and body["writes"][0]["written"]["unchanged"] is True
        other = http(base + "/api/brain/test", {"rule_id": "rule-brain-shipment", "event": {"type": "other.thing", "data": {}}}, auth)[1]
        assert other["matches"] is False and other["writes"][0]["skipped"]
        assert http(base + "/api/brain/test", {"rule_id": "nope"}, auth)[0] == 404

        status, body = http(base + "/api/brain/history?limit=5")
        assert status == 200 and len(body["writes"]) == 1 and body["writes"][0]["summary"].startswith("nota Familia/Envíos/")

        status, body = http(base + "/api/brain/rules/rule-brain-shipment", {"enabled": True}, auth)
        assert status == 200 and body["installed"] and hub.rules.get("rule-brain-shipment")["enabled"] is True
        assert http(base + "/api/brain/rules/rule-brain-shipment", {"enabled": False}, auth)[1]["rule"]["enabled"] is False
        assert http(base + "/api/brain/rules/nope", {"enabled": True}, auth)[0] == 404
        assert http(base + "/api/brain/target", {"url": brain.url + "/"}, auth)[1]["target"]["base"] == brain.url
        assert http(base + "/api/brain/retry", {}, auth)[1]["ok"] is True
        assert "brain.js" in json.dumps(http(base + "/api/facets")[1])
    finally:
        server.shutdown()
        server.server_close()


def test_tools(hub, brain):
    names = {t["name"] for t in tools.all_tools()}
    assert {"hub_brain_status", "hub_brain_rule_set", "hub_brain_test", "hub_brain_history", "hub_brain_retry", "hub_brain_target"} <= names
    assert tools.call(hub, "hub_brain_rule_set", {"id": "rule-brain-minutes"})["installed"] is True
    st = tools.call(hub, "hub_brain_status", {"probe": True})
    assert st["target"]["reachable"] and next(r for r in st["rules"] if r["id"] == "rule-brain-minutes")["enabled"]
    out = tools.call(hub, "hub_brain_test", {"rule_id": "rule-brain-minutes", "write": True})
    assert out["writes"][0]["written"]["status"] == "done"
    assert {"marta ibarra", "pablo sanz"} == set(brain.entities)
    note = brain.notes["Familia/Reuniones/Revisión del taller 2026-11-09.md"]
    assert "Asistentes: Marta Ibarra, Pablo Sanz" in note and "[[Pablo Sanz]]" in note
    hist = tools.call(hub, "hub_brain_history", {"rule": "rule-brain-minutes"})
    assert hist["writes"][0]["status"] == "done" and hist["writes"][0]["url"]
    # a rule written by hand goes through the ordinary rule tools
    added = tools.call(hub, "hub_rule_add", {"name": "Receta → nota", "when": {"type": "cookhoard.recipe.imported"}, "cooldown_s": 0,
                                             "then": [{"kind": "brain", "key": "cook:${event.data.recipe_id}",
                                                       "note": {"folder": "Familia/Recetas", "title": "${event.data.title}",
                                                                "text": ["Origen: ${event.data.source}"]}}]})
    assert added["ok"]
    hub.events.emit("cookhoard.recipe.imported", {"recipe_id": 5, "title": "Tortilla", "source": "web"}, source="cookhoard")
    assert wait_for(lambda: "Familia/Recetas/Tortilla.md" in brain.notes, timeout=20)
    assert tools.call(hub, "hub_brain_target", {"url": ""})["target"]["source"] == "pc"


# ---- the rule engine around it ---------------------------------------------------------------------------------------------
def test_a_brain_rule_does_not_wait_behind_a_slow_action_of_another_rule(hub, brain):
    """Rules run in order on one thread, so a backup that takes minutes would hold every later rule; one that only
    queues a Brain write (or emits) has its own thread."""
    import threading

    from hoard_link.hub.rules import is_fast
    release, entered = threading.Event(), threading.Event()
    real = hub.rules._run_actions

    def runner(acts, ctx, caller):
        if caller == "rule:slow":
            entered.set()
            release.wait(20)
            return [{"kind": "hub", "ok": True}]
        return real(acts, ctx, caller)

    hub.rules._run_actions = runner
    slow = {"id": "slow", "name": "slow", "when": {"type": "x.slow"}, "then": [{"kind": "hub", "tool": "hub_backup_run", "args": {}}], "cooldown_s": 0}
    fast = {k: v for k, v in factory("rule-brain-kafka-deadline").items() if k != "sample"}
    assert hub.rules.add(slow)["ok"] and hub.rules.add(fast)["ok"]
    assert is_fast(fast) and not is_fast(slow) and not is_fast({"then": []})
    try:
        hub.events.emit("x.slow", {}, source="x")
        assert entered.wait(5)
        hub.events.emit("kafka.deadline.created", {"deadline_id": "t5", "title": "Renovar el seguro", "due": "2027-01-10"}, source="kafka")
        assert wait_for(lambda: "Familia/Plazos/Renovar el seguro.md" in brain.notes, timeout=20)
        assert hub.rules.get("slow")["runs"] == 0          # the slow one is still at it
    finally:
        release.set()
    assert wait_for(lambda: hub.rules.get("slow")["runs"] == 1, timeout=10)


def test_the_last_event_of_a_type_is_found_past_the_newest_rows(hub):
    """``type`` filters in SQL: the limit counts matching rows, not the newest rows whatever their type."""
    hub.events.emit("kafka.deadline.created", {"deadline_id": "old"}, source="kafka")
    hub.events.emit("lumiere.render.done", {"id": 1}, source="lumiere")          # stored as lumiere.job.done
    for i in range(40):
        hub.events.emit("agent.call", {"n": i}, source="x")
    assert [e["data"]["deadline_id"] for e in hub.events.query(type="kafka.deadline.created", limit=1)] == ["old"]
    assert len(hub.events.query(type="kafka.*|phileas.*", limit=1)) == 1
    assert hub.events.query(type="lumiere.render.done", limit=1)[0]["type"] == "lumiere.job.done"
    assert hub.events.query(type="lumiere.job.done", limit=1)[0]["data"]["_orig_type"] == "lumiere.render.done"
    assert hub.events.query(type="nothing.like.this", limit=5) == []
    assert len(hub.events.query(type="agent.call", limit=7)) == 7 and len(hub.events.query(limit=5)) == 5
