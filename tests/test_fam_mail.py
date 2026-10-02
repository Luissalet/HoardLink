"""The app-side mail client (hoard_link.fam_mail) against a real hub over HTTP, with a fake helper behind the gateway."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hoard_link import fam_mail, family
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server
from tests.hub.conftest import free_port, write_manifest
from tests.hub.test_mailgate import FakeRunner, FakeSpheres, answer, rec, req


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "apps"
    tokens = {}
    for app in ("ledger", "people"):
        folder = write_manifest(root / f"{app.title()}'s Hoard", app, free_port(), service=f"{app}-hoard")
        (folder / "data").mkdir()
        tokens[app] = f"token-of-{app}"
        (folder / "data" / "mcp-token").write_text(tokens[app], encoding="utf-8")
    faustus = tmp_path / "faustus"
    (faustus / "mcp_servers").mkdir(parents=True)
    (faustus / "mcp_servers" / "email_server.py").write_text("", encoding="utf-8")
    (faustus / "venv" / "bin").mkdir(parents=True)
    (faustus / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path / "hubdata"), roots=[str(root)], icon_dirs=[], faustus_urls=["http://127.0.0.1:1"],
                    jobs_enabled=False, faustus_dir=str(faustus))
    hub = Hub(cfg)
    gate = hub.facet("mailgate")
    gate.background = False
    gate.runner = FakeRunner()
    hub._facets_by_id["spheres"] = FakeSpheres()
    server = make_server(hub, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fam_mail.forget_availability()
    family.configure("ledger", str(root / "Ledger's Hoard" / "data"), hub=cfg.url)
    yield {"hub": hub, "gate": gate, "url": cfg.url, "tmp": tmp_path, "root": root}
    server.shutdown()
    family.configure("", None, hub=None)
    fam_mail.forget_availability()
    hub.close()


def seed(w):
    adir = Path(w["gate"].attachments_dir)
    adir.mkdir(parents=True, exist_ok=True)
    sha = "ab" * 32
    (adir / f"{sha}.pdf").write_bytes(b"%PDF-1.4 the invoice")
    w["gate"].post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 10}))
    w["gate"].runner.answers.append(answer([
        rec(1, "Tu factura de octubre", "billing@tienda.es", text="importe 12,50 EUR", links=[{"url": "https://pay.example/1", "label": "pagar"}],
            attachments=[{"name": "factura.pdf", "mime": "application/pdf", "size": 20, "sha": sha, "path": str(adir / f"{sha}.pdf")}]),
        rec(2, "Hola", "pepe@example.com"),
        rec(3, "Factura del trabajo", "x@corp.com", account="Work", account_address="work@corp.com")]))
    assert w["gate"].run_pass()["ok"]
    return sha


def test_unavailable_until_the_hub_has_read_the_inbox(world):
    assert fam_mail.available() is False                                   # gateway off
    world["gate"].post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 10}))
    fam_mail.forget_availability()
    assert fam_mail.available() is False                                   # on, but no pass yet: keep using the own helper
    seed(world)
    fam_mail.forget_availability()
    assert fam_mail.available() is True
    world["gate"].post(req("POST", "/api/mail/config", body={"enabled": False}))
    assert fam_mail.available() is True                                    # cached for 30 s
    fam_mail.forget_availability()
    assert fam_mail.available() is False


def test_a_stalled_hub_pass_is_not_available(world):
    seed(world)
    fam_mail.forget_availability()
    assert fam_mail.available() is True
    world["gate"].store.set_state("last_ok_ts", time.time() - 6 * 3600)    # interval is 10 min: the thread must be dead
    fam_mail.forget_availability()
    assert fam_mail.available() is False


def test_interest_messages_claim_and_attachments(world, tmp_path):
    sha = seed(world)
    reg = fam_mail.register_interest({"subject_terms": ["factura"], "has_attachment": True})
    assert reg["ok"] and reg["app"] == "ledger"
    page = fam_mail.messages(since_id=0, limit=10)
    assert page["ok"] and [m["subject"] for m in page["messages"]] == ["Tu factura de octubre"]      # not the work one, not "Hola"
    m = page["messages"][0]
    # the gateway's keys and the Kafka helper's
    assert m["message_id"] == "<Personal-1@x>" and m["from"] == "billing@tienda.es" and m["from_address"] == "billing@tienda.es"
    assert m["text"] == "importe 12,50 EUR" and m["links"][0]["url"] == "https://pay.example/1" and m["account"] == "Personal"
    assert m["ts"] == m["date_ts"] and m["date"].endswith("-0000")
    assert m["from_self"] is False and m["sphere"] == "personal"
    att = m["attachments"][0]
    assert att["name"] == "factura.pdf" and Path(att["path"]).read_bytes() == b"%PDF-1.4 the invoice" and att["url"].endswith(sha)
    assert page["last_id"] == m["id"]
    again = fam_mail.messages(since_id=page["last_id"])
    assert again["messages"] == [] and again["last_id"] == page["last_id"]
    # lighter listing, and the wider one (interest off) still stays inside the app's spheres
    assert fam_mail.messages(full=False)["messages"][0]["text"] == "" and fam_mail.messages(full=False)["messages"][0]["snippet"]
    wide = fam_mail.messages(interest=False)
    assert sorted(x["subject"] for x in wide["messages"]) == ["Hola", "Tu factura de octubre"]
    # claim
    res = fam_mail.claim([m["id"]], "payment", "hoard://ledger/tx/12")
    assert res["ok"] and res["claimed"] == 1
    assert world["gate"].store.get(m["id"])["claims"][0]["ref"] == "hoard://ledger/tx/12"
    # copy: the local file when readable, the download through the hub when not
    dest = tmp_path / "ledger-attachments"
    local = fam_mail.copy_attachment(att, str(dest))
    assert Path(local).read_bytes() == b"%PDF-1.4 the invoice" and Path(local).name == f"{sha}.pdf"
    other = fam_mail.copy_attachment({**att, "path": "/nonexistent/x.pdf"}, str(tmp_path / "second"))
    assert Path(other).read_bytes() == b"%PDF-1.4 the invoice"
    again_copy = fam_mail.copy_attachment(att, str(dest))
    assert again_copy == local
    assert fam_mail.copy_attachment({"name": "x.pdf"}, str(tmp_path / "third")) == ""
    assert fam_mail.copy_attachment({**att, "path": "", "url": f"/api/mail/attachments/{'0' * 64}"}, str(tmp_path / "fourth")) == ""


def test_an_app_without_a_matching_interest_sees_nothing(world):
    seed(world)
    assert fam_mail.messages()["messages"] == []                          # no interest registered yet
    fam_mail.register_interest({"from_domains": ["nowhere.test"]})
    assert fam_mail.messages()["messages"] == []


def test_sphere_scoped_interest(world):
    seed(world)
    fam_mail.register_interest({"subject_terms": ["factura"]}, sphere="work")
    assert fam_mail.messages()["messages"] == []                          # ledger may not read work mail, whatever it registers


def test_hub_unreachable_never_raises(world, tmp_path):
    family.configure("ledger", str(world["root"] / "Ledger's Hoard" / "data"), hub="http://127.0.0.1:1")
    fam_mail.forget_availability()
    assert fam_mail.available(timeout=0.3) is False
    assert fam_mail.register_interest({"subject_terms": ["x"]}, timeout=0.3) == {"ok": False, "error": "hub unreachable"}
    page = fam_mail.messages(since_id=7, timeout=0.3)
    assert page == {"ok": False, "error": "hub unreachable", "messages": [], "last_id": 7}
    assert fam_mail.claim([1], "k", "r", timeout=0.3) == {"ok": False, "error": "hub unreachable"}
    assert fam_mail.copy_attachment({"url": "/api/mail/attachments/" + "a" * 64}, str(tmp_path / "d"), timeout=0.3) == ""


def test_wrong_token_is_explained(world, tmp_path):
    bad = tmp_path / "bad-token"
    bad.write_text("not-a-token", encoding="utf-8")
    family.configure("ledger", token_file=str(bad), hub=world["url"])
    fam_mail.forget_availability()
    assert fam_mail.available() is False
    res = fam_mail.messages()
    assert res["ok"] is False and "refused this app's token" in res["error"]


# ---- the Node twin (the mail functions of js/hoard-link.js) --------------------------

JS_DIR = Path(__file__).resolve().parents[1] / "js"

JS_SCRIPT = r"""
const lib = await import(process.argv[2]);
const [tokenFile, hub, dest] = process.argv.slice(3);
lib.configure({ app: "ledger", tokenFile, hub });
const out = {};
out.availableBefore = await lib.mailAvailable();
out.interest = (await lib.mailRegisterInterest({ subject_terms: ["factura"], has_attachment: true })).ok;
const page = await lib.mailMessages({ sinceId: 0, limit: 10 });
out.subjects = page.messages.map((m) => m.subject);
const m = page.messages[0];
out.shape = [m.message_id, m.from, m.from_address, m.account, m.from_self, typeof m.ts, m.date.length > 0];
out.last = page.last_id === m.id;
out.claim = (await lib.mailClaim([m.id], "payment", "hoard://ledger/tx/12")).claimed;
const a = m.attachments[0];
out.copyLocal = await lib.mailCopyAttachment(a, dest);
out.copyHub = await lib.mailCopyAttachment({ ...a, path: "/nonexistent/x.pdf", sha: "" }, dest + "-2");
out.copyNone = await lib.mailCopyAttachment({ name: "x.pdf" }, dest + "-3");
lib.configure({ app: "ledger", tokenFile, hub: "http://127.0.0.1:1" });
lib.mailForgetAvailability();
out.down = [await lib.mailAvailable(300), await lib.mailRegisterInterest({}), (await lib.mailMessages({ sinceId: 4, timeoutMs: 300 }))];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node not installed")
def test_js_twin(world, tmp_path):
    import json
    import subprocess
    w = world
    seed(w)
    fam_mail.forget_availability()
    base = (JS_DIR / "hoard-link.js").read_text(encoding="utf-8")
    merged = base
    lib = tmp_path / "lib.mjs"
    lib.write_text(merged, encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(JS_SCRIPT, encoding="utf-8")
    token_file = w["root"] / "Ledger's Hoard" / "data" / "mcp-token"
    dest = tmp_path / "js-attachments"
    run = subprocess.run(["node", str(script), lib.as_uri(), str(token_file), w["url"], str(dest)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["availableBefore"] is True and out["interest"] is True
    assert out["subjects"] == ["Tu factura de octubre"]
    assert out["shape"] == ["<Personal-1@x>", "billing@tienda.es", "billing@tienda.es", "Personal", False, "number", True]
    assert out["last"] is True and out["claim"] == 1
    assert Path(out["copyLocal"]).read_bytes() == b"%PDF-1.4 the invoice"
    assert Path(out["copyHub"]).read_bytes() == b"%PDF-1.4 the invoice"        # downloaded through the hub
    assert out["copyNone"] == ""
    assert out["down"][0] is False and out["down"][1] == {"ok": False, "error": "hub unreachable"}
    assert out["down"][2]["ok"] is False and out["down"][2]["messages"] == [] and out["down"][2]["last_id"] == 4
