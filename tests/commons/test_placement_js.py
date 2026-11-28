"""js/hoard-commons/placement.js: the Express Hoards find Faustus where the Hub placed it (twin of hoard_link.where)."""

from __future__ import annotations

import json
import textwrap

from tests.commons.jsrun import JS_DIR
from tests.commons.test_copilot_js import run

PLACEMENT = (JS_DIR / "placement.js").resolve().as_uri()
COPILOT = (JS_DIR / "copilot.js").resolve().as_uri()


def test_pure_helpers_order_and_filter_the_addresses():
    out = run(f"""
        import * as p from {json.dumps(PLACEMENT)};
        const sparks = {{ location: "sparks", job: {{ running: false }},
          urls: {{ faustus: "https://spark3.example.ts.net/", hermes: "https://spark3.example.ts.net:5221/products/" }} }};
        console.log(JSON.stringify({{
          placed: p.placedUrl("faustus", sparks),
          hermes: p.placedUrl("hermes", sparks),
          unknown: p.placedUrl("prometheus", sparks),
          onPc: p.placedUrl("faustus", {{ ...sparks, location: "pc" }}),
          moving: p.placedUrl("faustus", {{ ...sparks, job: {{ running: true }} }}),
          bad: p.placedUrl("faustus", {{ location: "sparks", urls: {{ faustus: "nope" }} }}),
          order: p.withPlacement("faustus", ["http://127.0.0.1:7000/", "http://127.0.0.1:7001"], sparks),
          same: p.withPlacement("faustus", ["https://spark3.example.ts.net"], sparks),
          none: p.withPlacement("faustus", ["http://127.0.0.1:7000"], {{}}),
          hub: p.hubUrl({{ env: {{}}, configPath: null }}),
        }}));
    """, env={"HOARD_HUB_URL": ""})
    assert out["placed"] == "https://spark3.example.ts.net"
    assert out["hermes"] == "https://spark3.example.ts.net:5221/products"
    assert out["unknown"] == out["onPc"] == out["moving"] == out["bad"] == ""
    assert out["order"] == ["http://127.0.0.1:7000", "http://127.0.0.1:7001", "https://spark3.example.ts.net"]
    assert out["same"] == ["https://spark3.example.ts.net"]
    assert out["none"] == ["http://127.0.0.1:7000"]
    assert out["hub"] == "http://127.0.0.1:8810"


def test_the_copilot_relay_finds_faustus_on_the_spark():
    out = run(f"""
        import http from "node:http";
        import fs from "node:fs";
        import {{ fileURLToPath }} from "node:url";
        import {{ buildCopilot }} from {json.dumps(COPILOT)};

        const listen = (handler) => new Promise((resolve) => {{
          const server = http.createServer(handler).listen(0, "127.0.0.1", () => resolve(server));
        }});
        let hubDoc = {{}};
        const faustus = await listen((req, res) => {{
          const ok = req.url === "/api/hoard-copilot/capabilities";
          res.writeHead(ok ? 200 : 404, {{ "Content-Type": "application/json" }});
          res.end(ok ? '{{"contract_version":"1.0"}}' : "{{}}");
        }});
        const hub = await listen((req, res) => {{
          res.writeHead(200, {{ "Content-Type": "application/json" }});
          res.end(JSON.stringify(hubDoc));
        }});
        const dead = await listen(() => {{}});
        const deadPort = dead.address().port;
        dead.close();                                              // a port nobody listens on: the PC's Faustus, switched off
        const faustusUrl = "http://127.0.0.1:" + faustus.address().port;
        const env = {{ HOARD_HUB_URL: "http://127.0.0.1:" + hub.address().port }};
        const dataDir = fileURLToPath(new URL("./placement", import.meta.url));
        fs.mkdirSync(dataDir);
        const copilot = buildCopilot({{ appId: "qa", dataDir,
          faustusUrls: ["http://127.0.0.1:" + deadPort], env }});
        const ask = async () => {{
          let body = null, status = 200;
          const res = {{ json: (value) => {{ body = value; }}, status(code) {{ status = code; return this; }} }};
          await copilot.handlers.info({{}}, res);
          return {{ status, body }};
        }};
        hubDoc = {{ location: "sparks", job: {{ running: false }}, urls: {{ faustus: faustusUrl }} }};
        const onSpark = await ask();
        console.log(JSON.stringify({{ onSpark: onSpark.body?.settings_url, status: onSpark.status }}));
        process.exit(0);
    """, env={"HOARD_HUB_URL": ""})
    assert out["status"] == 200
    assert out["onSpark"].endswith("/studio/settings")             # answered by the fake Faustus the Hub placed, not the dead address
