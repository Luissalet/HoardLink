"""The app-side client of the refs facet (hoard_link.fam_refs), against a real hub over loopback."""

from __future__ import annotations

import pytest

from hoard_link import family, fam_refs
from tests.hub._hub_fakes import FakeApp, make_hub, serve

LEDGER = "hoard://ledger/tx/12"
KAFKA = "hoard://kafka/document/7"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_HUB_AUTO_RULES", "0")
    ledger = FakeApp("ledger")
    hub = make_hub(tmp_path, [ledger], extra_apps=["kafka"])
    server = serve(hub)
    saved = family.status()
    token_file = tmp_path / "apps" / "Ledger's Hoard" / "data" / "mcp-token"
    family.configure("ledger", token_file=str(token_file), hub=hub.config.url, enabled=False)
    yield hub
    family.configure(saved["app"], token_file=saved["token_file"], hub=saved["hub_url"], enabled=saved["enabled"])
    server.shutdown()
    hub.close()
    ledger.stop()


def test_link_around_unlink_through_the_hub(client):
    res = fam_refs.link(LEDGER, KAFKA, "purchase", from_label="Amazon 23.90 EUR", to_label="Invoice 114")
    assert res["ok"] and res["created"] and res["edge"]["by"] == "ledger"
    assert fam_refs.link(LEDGER, KAFKA, "purchase")["created"] is False                      # idempotent
    g = fam_refs.around(LEDGER)
    assert g["ok"] and {n["uri"] for n in g["nodes"]} == {LEDGER, KAFKA}
    assert next(n for n in g["nodes"] if n["uri"] == KAFKA)["label"] == "Invoice 114"
    assert fam_refs.around(KAFKA, depth=2)["edges"][0]["rel"] == "purchase"
    # the hub refuses to let an app speak for others
    res = fam_refs.link(KAFKA, "hoard://people/contact/1")
    assert res["ok"] is False and "own app" in res["error"]
    assert fam_refs.link("bad", KAFKA)["ok"] is False
    assert fam_refs.unlink(LEDGER, KAFKA, "purchase")["removed"] == 1
    assert fam_refs.around(LEDGER)["edges"] == []


def test_never_raises_when_the_hub_is_not_there(tmp_path):
    saved = family.status()
    family.configure("ledger", token_file=str(tmp_path / "none"), hub="http://127.0.0.1:1", enabled=False)
    try:
        for res in (fam_refs.link(LEDGER, KAFKA, timeout=0.5), fam_refs.around(LEDGER, timeout=0.5), fam_refs.unlink(LEDGER, KAFKA, timeout=0.5)):
            assert res == {"ok": False, "error": "hub unreachable"}
    finally:
        family.configure(saved["app"], token_file=saved["token_file"], hub=saved["hub_url"], enabled=saved["enabled"])


# ---- the Node twin (refs functions of js/hoard-link.js) ----------

JS_SCRIPT = r"""
const family = await import(process.argv[2]);
family.configure({ app: "ledger", tokenFile: process.argv[3], hub: process.argv[4], enabled: false });
const a = await family.refsLink("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase", { fromLabel: "Pay" });
const b = await family.refsLink("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase");
const c = await family.refsAround("hoard://ledger/tx/12");
const d = await family.refsLink("hoard://kafka/document/7", "hoard://people/contact/1");
family.configure({ app: "ledger", tokenFile: process.argv[3], hub: "http://127.0.0.1:1", enabled: false });
const e = await family.refsAround("hoard://ledger/tx/12", 1, { timeoutMs: 500 });
const f = await family.refsUnlink("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase", { timeoutMs: 500 });
console.log(JSON.stringify({ a: [a.ok, a.created], b: [b.ok, b.created], c: c.nodes.map((n) => n.uri), d: [d.ok, d.error], e, f }));
"""


def test_js_twin(client, tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    root = Path(__file__).resolve().parents[1] / "js"
    merged = tmp_path / "hoard-link.mjs"
    merged.write_text((root / "hoard-link.js").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(JS_SCRIPT, encoding="utf-8")
    token_file = tmp_path / "apps" / "Ledger's Hoard" / "data" / "mcp-token"
    run = subprocess.run(["node", str(script), merged.as_uri(), str(token_file), client.config.url], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["a"] == [True, True] and out["b"] == [True, False]
    assert set(out["c"]) == {LEDGER, KAFKA}
    assert out["d"][0] is False and "own app" in out["d"][1]
    assert out["e"] == {"ok": False, "error": "hub unreachable"} and out["f"] == {"ok": False, "error": "hub unreachable"}
