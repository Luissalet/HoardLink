"""fam_notify.Router (the auto | hub | own switch every app used to re-implement) and its Node twins in js/hoard-link.js
(notifyRouter, buildToastPs1, showToast). The Python and JS sides run the same scenarios and must agree."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hoard_link import family, fam_notify
from hoard_link import notify_channels as nc
from hoard_link.fam_notify import Router
from tests.hub.conftest import free_port

ROOT = Path(__file__).resolve().parents[2]
LINK_JS = ROOT / "js" / "hoard-link.js"


def run_link_js(body: str, payload: Any = None, *, timeout: float = 60.0) -> Any:
    """Run ``body`` (an async function body with ``fam`` = js/hoard-link.js and ``input``) under node; return its JSON result."""
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed")
    script = (f"import * as fam from {json.dumps(LINK_JS.as_uri())};\n"
              "const input = JSON.parse(process.argv[2]);\n"
              f"const out = await (async () => {{\n{body}\n}})();\n"
              "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([exe, str(f), json.dumps(payload)], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


# ---- the fakes -------------------------------------------------------------------------------------------------------

class FakeHub:
    """An object with ``notify`` / ``hub_available`` like the fam_notify module; records what it was asked."""

    def __init__(self, answer=None, up=True, raises=False):
        self.answer = answer if answer is not None else {"ok": True, "id": 7, "held": None, "delivered": ["windows"]}
        self.up, self.raises = up, raises
        self.calls: list[tuple] = []

    def hub_available(self):
        if self.raises:
            raise RuntimeError("boom")
        return self.up

    def notify(self, title, body="", **kw):
        self.calls.append((title, body, kw))
        if self.raises:
            raise RuntimeError("boom")
        return self.answer


class Own:
    def __init__(self, result=None, raises=False):
        self.calls: list[tuple] = []
        self.result, self.raises = result, raises

    def __call__(self, title, body="", **kw):
        self.calls.append((title, body, kw))
        if self.raises:
            raise ValueError("own broke")
        return self.result


def router(via="auto", hub=None, own=None):
    return Router(lambda: via, own if own is not None else Own(), app_name="Test", hub=hub or FakeHub())


# ---- Python: the switch --------------------------------------------------------------------------------------------

def test_auto_uses_the_hub_when_it_answers_and_passes_everything_on():
    hub, own = FakeHub(), Own()
    res = router("auto", hub, own).send("Price dropped", "Kindle 79 EUR", priority="high", url="http://x.test", group="price", dedupe_key="p:1", sphere="personal")
    assert (res["via"], res["ok"], res["why"], res["id"]) == ("hub", True, "", 7) and res["held"] == "" and res["hub"]["delivered"] == ["windows"]
    assert hub.calls == [("Price dropped", "Kindle 79 EUR", {"priority": "high", "url": "http://x.test", "group": "price", "dedupe_key": "p:1", "sphere": "personal"})]
    assert own.calls == []


def test_defaults_are_empty_strings_not_nones_for_the_hub():
    hub = FakeHub()
    router("auto", hub).send("Just a title")
    assert hub.calls[0][2] == {"priority": "normal", "url": "", "group": "", "dedupe_key": "", "sphere": None}


@pytest.mark.parametrize("held", ["quiet", "duplicate", "digest", "rate", "disabled"])
def test_auto_does_not_repeat_through_own_channels_when_the_hub_held_it(held):
    hub, own = FakeHub({"ok": True, "id": 3, "held": held}), Own()
    res = router("auto", hub, own).send("Hi")
    assert (res["via"], res["ok"], res["why"], res["held"]) == ("hub", True, f"held: {held}", held)
    assert own.calls == []


def test_auto_falls_back_to_own_when_the_hub_is_away_or_refuses():
    for hub in (FakeHub(up=False), FakeHub({"ok": False, "error": "hub unreachable"}), FakeHub({"ok": False, "status": 401, "error": "refused"}),
                FakeHub({"ok": False}), FakeHub(raises=True), FakeHub("weird")):
        own = Own(result={"ok": True})
        res = router("auto", hub, own).send("Hi", priority="high")
        assert (res["via"], res["ok"]) == ("own", True), hub.answer
        assert own.calls == [("Hi", "", {"priority": "high", "url": None, "group": None, "dedupe_key": None, "sphere": None})]


def test_hub_mode_reports_the_failure_instead_of_hiding_it():
    own = Own()
    res = router("hub", FakeHub({"ok": False, "error": "hub unreachable"}, up=False), own).send("Hi")
    assert (res["via"], res["ok"], res["why"]) == ("hub", False, "hub unreachable") and own.calls == []
    assert router("hub", FakeHub({"ok": False, "status": 500}), own).send("Hi")["why"] == "hub notify failed (500)"
    assert router("hub", FakeHub(raises=True), own).send("Hi")["why"] == "hub notify: RuntimeError"
    assert router("hub", FakeHub(up=False), own).hub_up() is False                 # "hub" ignores availability: it calls the hub anyway


def test_own_mode_never_calls_the_hub():
    hub, own = FakeHub(), Own()
    res = router("own", hub, own).send("Hi")
    assert res["via"] == "own" and res["ok"] is True and hub.calls == []


def test_the_setting_is_normalised_and_a_broken_getter_is_auto():
    assert router("  OWN ").via_setting() == "own" and router("nonsense").via_setting() == "auto" and router(None).via_setting() == "auto"

    def bad():
        raise RuntimeError("settings db gone")
    assert Router(bad, Own(), hub=FakeHub()).via_setting() == "auto"
    assert Router(None, Own(), hub=FakeHub()).via_setting() == "auto"


@pytest.mark.parametrize("raw,ok,why", [
    ({"ok": True, "extra": 1}, True, ""), ({"ok": False, "error": "no network"}, False, "no network"), ({"error": "x"}, False, "x"), ({}, True, ""),
    (True, True, ""), (None, True, ""), (False, False, "failed"), ("", True, ""), ("smtp down", False, "smtp down")])
def test_own_results_are_normalised(raw, ok, why):
    res = router("own", own=Own(result=raw)).send("Hi")
    assert (res["via"], res["ok"], res["why"]) == ("own", ok, why)


def test_own_that_raises_never_escapes_and_a_missing_own_is_reported():
    assert router("own", own=Own(raises=True)).send("Hi") == {"via": "own", "ok": False, "why": "ValueError"}
    assert Router(lambda: "own", None, hub=FakeHub()).send("Hi") == {"via": "own", "ok": False, "why": "no own channel configured"}


def test_via_status():
    assert router("auto", FakeHub(up=True)).via_status() == {"setting": "auto", "effective": "hub", "hub_available": True}
    assert router("auto", FakeHub(up=False)).via_status() == {"setting": "auto", "effective": "own", "hub_available": False}
    assert router("hub", FakeHub(up=False)).via_status() == {"setting": "hub", "effective": "hub", "hub_available": False}
    assert router("own", FakeHub(up=True)).via_status() == {"setting": "own", "effective": "own", "hub_available": False}


# ---- Python: the real fam_notify module against a fake hub over HTTP -------------------------------------------------

class WebHub:
    def __init__(self, held=None):
        self.posts: list[dict] = []
        outer, self.held = self, held

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._send(200, {"ok": True, "notifications": []})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.posts.append(json.loads(self.rfile.read(n) or b"{}"))
                self._send(200, {"ok": True, "id": 9, "held": outer.held, "delivered": [] if outer.held else ["windows"]})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def isolated_family():
    saved = dict(family._state)
    fam_notify._reset_cache()
    yield
    family._state.clear()
    family._state.update(saved)
    fam_notify._reset_cache()


def test_default_hub_is_the_fam_notify_module(tmp_path, isolated_family):
    web = WebHub(held="quiet")
    try:
        (tmp_path / "mcp-token").write_text("tok", encoding="utf-8")
        family.configure("fake", token_file=str(tmp_path / "mcp-token"), hub=web.url)
        own = Own()
        res = Router(lambda: "auto", own, "Fake").send("Real hub", "body", priority="high", dedupe_key="k1")
        assert (res["via"], res["ok"], res["held"], res["why"]) == ("hub", True, "quiet", "held: quiet") and own.calls == []
        assert web.posts[0] == {"title": "Real hub", "body": "body", "priority": "high", "dedupe_key": "k1"}
    finally:
        web.close()
    fam_notify._reset_cache()
    family.configure("fake", token_file=str(tmp_path / "mcp-token"), hub=f"http://127.0.0.1:{free_port()}")
    own = Own(result=True)
    res = Router(lambda: "auto", own, "Fake").send("Nobody home")
    assert res["via"] == "own" and res["ok"] is True and own.calls[0][0] == "Nobody home"


# ---- Node: the toast ---------------------------------------------------------------------------------------------------

TOAST_CASES = [
    ["Plain title", "plain body", None, {}],
    ["Tom & <Jerry>", 'say "hi" & \'bye\'', "http://127.0.0.1:5200/#/x?a=1&b=2", {}],
    ["Accents áéíóú ñ", "cuerpo con ü", "https://example.com/a b", {}],
    ["x", "y", "file:///c:/x", {}],
    ["x", "y", "hoard://a/b/1", {}],
    ["x", "y", "HTTPS://Example.com", {}],
    ["x", "y", "javascript:alert(1)", {}],
    ["x", "y", "", {}],
    ["T" * 200, "B" * 500, None, {}],
    ["emoji \U0001F600" * 30, "\U0001F600" * 400, None, {}],
    ["ctrl\x00\x01\x1f chars\x0b", "a\x08b", None, {}],
    ["x", "y", None, {"app_name": "Kafka's Hoard"}],
    ["x", "y", "http://a.test", {"app_name": "N" * 100, "app_id": "My.App"}],
    ["", "", None, {}],
]


def test_node_build_toast_ps1_is_identical_to_python():
    py = [nc.build_toast_ps1(t, b, u, **o) for t, b, u, o in TOAST_CASES]
    js = run_link_js("""
      return input.map(([t, b, u, o]) => fam.buildToastPs1(t, b, u, { appName: o.app_name ?? null, ...(o.app_id ? { appId: o.app_id } : {}) }));
    """, TOAST_CASES)
    assert js == py


def test_node_helpers_agree_with_python():
    urls = ["http://a.test", " https://a.test ", "hoard://x", "file:///x", "javascript:x", "", None, "HTTP://UP.test"]
    got = run_link_js("return { esc: input.s.map((s) => fam.xmlEscape(s)), url: input.u.map((u) => fam.httpUrl(u)) };",
                      {"s": ["a & b <c> \"d\" 'e'", "ctrl\x00\x1f", "plain", ""], "u": urls})
    assert got["esc"] == [nc.xml_escape(s) for s in ["a & b <c> \"d\" 'e'", "ctrl\x00\x1f", "plain", ""]]
    assert got["url"] == [nc.http_url(u) for u in urls]


def test_node_show_toast_runs_powershell_hidden_and_reports_failures():
    got = run_link_js("""
      const ran = [];
      const mk = (code, error) => async (o) => { ran.push({ command: o.command, args: o.args.slice(0, 4), file: o.args[4] }); return error ? { code: null, error } : { code }; };
      const ok = await fam.showToast("Hi", "there", "http://a.test", { platform: "win32", runner: mk(0) });
      const bad = await fam.showToast("Hi", "there", null, { platform: "win32", runner: mk(3) });
      const err = await fam.showToast("Hi", "there", null, { platform: "win32", runner: mk(null, "ENOENT") });
      const off = await fam.showToast("Hi", "there", null, { platform: "linux", runner: mk(0) });
      const fs = await import("node:fs");
      return { ok, bad, err, off, n: ran.length, args: ran[0].args, command: ran[0].command, leftover: ran.map((r) => fs.existsSync(r.file)) };
    """)
    assert got["ok"] == {"ok": True} and got["bad"] == {"ok": False, "error": "powershell exit 3"} and got["err"] == {"ok": False, "error": "ENOENT"}
    assert got["off"] == {"ok": False, "unsupported": True, "error": "unsupported"} and got["n"] == 3
    assert got["command"] == "powershell" and got["args"] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] and got["leftover"] == [False] * 3


# ---- Node: the router, scenario by scenario against the Python one -----------------------------------------------------

SCENARIOS = [
    # name, via, hub behaviour, own result
    ("auto-hub", "auto", {"answer": {"ok": True, "id": 7, "held": None, "delivered": ["windows"]}}, True),
    ("auto-held", "auto", {"answer": {"ok": True, "id": 3, "held": "quiet"}}, True),
    ("auto-down", "auto", {"up": False}, {"ok": True}),
    ("auto-refused", "auto", {"answer": {"ok": False, "status": 401, "error": "refused"}}, "smtp down"),
    ("auto-raises", "auto", {"raises": True}, False),
    ("hub-down", "hub", {"up": False, "answer": {"ok": False, "error": "hub unreachable"}}, True),
    ("hub-500", "hub", {"answer": {"ok": False, "status": 500}}, True),
    ("hub-held", "hub", {"answer": {"ok": True, "id": 1, "held": "duplicate"}}, True),
    ("own", "own", {}, {"ok": False, "error": "no network"}),
    ("weird-setting", "NONSENSE", {"answer": {"ok": True, "id": 2, "held": None}}, None),
]


def py_scenario(via, hub_spec, own_result):
    hub = FakeHub(hub_spec.get("answer"), up=hub_spec.get("up", True), raises=hub_spec.get("raises", False))
    own = Own(result=own_result)
    res = Router(lambda: via, own, "Test", hub=hub).send("Title", "Body", priority="high", url="http://a.test", group="g", dedupe_key="k", sphere="work")
    return {"res": {k: v for k, v in res.items() if k in ("via", "ok", "why", "held", "id")},
            "hub_calls": len(hub.calls), "own_calls": len(own.calls), "status": router_status(via, hub)}


def router_status(via, hub):
    return Router(lambda: via, Own(), hub=hub).via_status()


def test_node_notify_router_matches_the_python_router():
    expected = {name: py_scenario(via, spec, own) for name, via, spec, own in SCENARIOS}
    got = run_link_js("""
      const out = {};
      for (const [name, via, spec, ownResult] of input) {
        const hubCalls = []; const ownCalls = [];
        const hub = {
          async hubAvailable() { if (spec.raises) throw new Error("boom"); return spec.up ?? true; },
          async notify(title, body, o) { hubCalls.push([title, body, o]); if (spec.raises) throw new Error("boom"); return spec.answer ?? { ok: true, id: 7, held: null, delivered: ["windows"] }; },
        };
        const router = fam.notifyRouter({ via: async () => via, ownSend: async (t, b, o) => { ownCalls.push([t, b, o]); return ownResult; }, app: "Test", hub });
        const res = await router.send("Title", "Body", { priority: "high", url: "http://a.test", group: "g", dedupeKey: "k", sphere: "work" });
        const pick = {}; for (const k of ["via", "ok", "why", "held", "id"]) if (k in res) pick[k] = res[k];
        out[name] = { res: pick, hub_calls: hubCalls.length, own_calls: ownCalls.length, status: await router.viaStatus(), hubArgs: hubCalls[0] || null, ownArgs: ownCalls[0] || null };
      }
      return out;
    """, SCENARIOS)
    for name, want in expected.items():
        have = got[name]
        # JS results omit keys that Python sets to "" / None only when the hub did not answer; compare what both define
        assert {k: have["res"].get(k) for k in ("via", "ok", "why")} == {k: want["res"][k] for k in ("via", "ok", "why")}, name
        assert (have["hub_calls"], have["own_calls"], have["status"]) == (want["hub_calls"], want["own_calls"], want["status"]), name
        if want["res"].get("held") is not None and want["res"]["via"] == "hub":
            assert have["res"]["held"] == want["res"]["held"], name
    assert got["auto-hub"]["hubArgs"][2] == {"priority": "high", "url": "http://a.test", "group": "g", "dedupeKey": "k", "sphere": "work"}
    assert got["auto-down"]["ownArgs"][2] == {"priority": "high", "url": "http://a.test", "group": "g", "dedupeKey": "k", "sphere": "work"}


def test_node_notify_router_defaults_and_errors():
    got = run_link_js("""
      const none = await fam.notifyRouter({ via: "own" }).send("Hi");
      const boom = await fam.notifyRouter({ via: "own", ownSend: async () => { throw new TypeError("x"); } }).send("Hi");
      const bad = await fam.notifyRouter({ via: () => { throw new Error("db gone"); }, hub: { hubAvailable: async () => false, notify: async () => ({ ok: true }) }, ownSend: () => true }).send("Hi");
      const calls = [];
      await fam.notifyRouter({ via: "hub", hub: { hubAvailable: async () => true, notify: async (t, b, o) => { calls.push(o); return { ok: true }; } } }).send("Just a title");
      return { none, boom, bad, calls };
    """)
    assert got["none"] == {"via": "own", "ok": False, "why": "no own channel configured"}
    assert got["boom"] == {"via": "own", "ok": False, "why": "TypeError"}
    assert got["bad"] == {"via": "own", "ok": True, "why": ""}                       # a broken getter is "auto"; the hub is down: own
    assert got["calls"] == [{"priority": "normal", "url": "", "group": "", "dedupeKey": "", "sphere": None}]
