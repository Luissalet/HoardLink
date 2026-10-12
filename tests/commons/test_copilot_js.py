"""js/hoard-commons/copilot.js: the Faustus copilot for the Express Hoards (the twin of hoard_link.copilot.install_copilot).

The pure helpers run with node alone. The end-to-end check needs a real ``express``: set HOARD_TEST_NODE_MODULES to a folder
whose node_modules has it (any Express Hoard). It starts a fake Faustus, an app that only calls ``family.configure`` and
``installSpa``, and drives the relay the way the browser panel does."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from hoard_link.copilot_launcher import LAUNCHER_JS
from tests.commons.jsrun import JS_DIR, node

MODULE = (JS_DIR / "copilot.js").resolve().as_uri()
EXPRESS_MODULE = (JS_DIR / "express.js").resolve().as_uri()
FAMILY_MODULE = (JS_DIR.parent / "hoard-link.js").resolve().as_uri()
REAL = os.environ.get("HOARD_TEST_NODE_MODULES", "")
needs_real = pytest.mark.skipif(not REAL, reason="set HOARD_TEST_NODE_MODULES to a folder with express")


def run(script: str, *, cwd: str | None = None, env: dict | None = None, timeout: float = 60) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "check.mjs"
        file.write_text(textwrap.dedent(script), encoding="utf-8")
        proc = subprocess.run([node(), str(file)], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                              cwd=cwd or tmp, env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_launcher_file_matches_the_python_launcher():
    # scripts/sync_vendored.py regenerates it; a stale copy would give Node apps a different panel.
    assert (JS_DIR / "copilot-launcher.js").read_text(encoding="utf-8") == LAUNCHER_JS


def test_inject_multipart_and_config():
    out = run(f"""
        import * as c from {json.dumps(MODULE)};
        const page = '<html><body><div id="root"></div></BODY></html>';
        const once = c.injectLauncher(page);
        const boundary = 'XyZ';
        const ctx = JSON.stringify({{ view: {{ route: '/x' }}, origin: {{ app_id: 'spoof', surface: 'page' }} }});
        const body = Buffer.from(`--${{boundary}}\\r\\nContent-Disposition: form-data; name="message"\\r\\n\\r\\nhola\\r\\n` +
          `--${{boundary}}\\r\\nContent-Disposition: form-data; name="hoard_context"\\r\\n\\r\\n${{ctx}}\\r\\n` +
          `--${{boundary}}\\r\\nContent-Disposition: form-data; name="file"; filename="a.bin"\\r\\nContent-Type: application/octet-stream\\r\\n\\r\\n\\u0000\\u00ff\\r\\n` +
          `--${{boundary}}--\\r\\n`, 'latin1');
        const rewritten = c.multipartContext(body, 'multipart/form-data; boundary=' + boundary, {{ app_id: 'links', data_space_id: 's1' }});
        const text = rewritten.toString('latin1');
        const ctxOut = JSON.parse(text.split('name="hoard_context"\\r\\n\\r\\n')[1].split('\\r\\n--')[0]);
        console.log(JSON.stringify({{
          injected: once.includes('/api/copilot/launcher.js') && once.indexOf('launcher.js') < once.indexOf('</BODY>'),
          idempotent: c.injectLauncher(once) === once,
          noBody: c.injectLauncher('<p>x</p>').endsWith('</script>'),
          origin: ctxOut.origin, route: ctxOut.view.route,
          fileKept: text.includes('\\u0000\\u00ff') && text.includes('name="message"\\r\\n\\r\\nhola'),
          notMultipart: c.multipartContext(Buffer.from('x'), 'application/json', {{}}).toString(),
          defaults: c.resolveFaustus({{ appId: 'links', env: {{}} }}),
          appEnv: c.resolveFaustus({{ appId: 'job-hunter', env: {{ JOB_HUNTER_FAUSTUS_URL: 'http://h:1/', HOARD_FAUSTUS_URL: 'http://g:2', HOARD_FAUSTUS_TOKEN: 't' }} }}),
          explicit: c.resolveFaustus({{ appId: 'links', faustusUrls: ['http://x:3/'], token: 'k', env: {{ HOARD_FAUSTUS_URL: 'http://g:2' }} }}),
        }}));
    """)
    assert out["injected"] and out["idempotent"] and out["noBody"]
    assert out["origin"] == {"app_id": "links", "surface": "page", "data_space_id": "s1"}
    assert out["route"] == "/x" and out["fileKept"]
    assert out["notMultipart"] == "x"
    assert out["defaults"] == {"urls": ["http://127.0.0.1:7000", "http://127.0.0.1:7001"], "token": ""}
    assert out["appEnv"] == {"urls": ["http://h:1"], "token": "t"}
    assert out["explicit"] == {"urls": ["http://x:3"], "token": "k"}


@needs_real
def test_install_spa_gives_an_express_hoard_the_copilot(tmp_path):
    dist = tmp_path / "app" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body><div id=root></div></body></html>", encoding="utf-8")
    (tmp_path / "app" / "faustus-plugin.json").write_text(json.dumps({"id": "linkstest", "name": "Links' Test Hoard"}), encoding="utf-8")
    data = tmp_path / "data"
    out = run(f"""
        import {{ createRequire }} from "node:module";
        import http from "node:http";
        const require = createRequire({json.dumps(str(Path(REAL) / "package.json"))});
        const express = require("express");
        const family = await import({json.dumps(FAMILY_MODULE)});
        const {{ installSpa }} = await import({json.dumps(EXPRESS_MODULE)});

        // A fake Faustus: the capability probe, the panel script and a slow SSE turn that echoes what it received.
        const seen = [];
        const faustus = http.createServer(async (req, res) => {{
          const chunks = []; for await (const c of req) chunks.push(c);
          const body = Buffer.concat(chunks).toString('utf8');
          seen.push({{ path: req.url, method: req.method, auth: req.headers.authorization || '', version: req.headers['x-faustus-client-version'] || '', body }});
          if (req.url === '/api/hoard-copilot/capabilities') return res.writeHead(200, {{ 'content-type': 'application/json' }}).end('{{"contract_version":"1.0"}}');
          if (req.url === '/api/hoard-copilot/panel.js') return res.writeHead(200, {{ 'content-type': 'application/javascript' }}).end('export const panel = 1;');
          if (req.url === '/api/chat_stream') {{
            res.writeHead(200, {{ 'content-type': 'text/event-stream', 'x-odysseus-run-id': 'run-1', 'x-secret': 'no' }});
            res.write('data: {{"delta":"ho"}}\\n\\n');
            setTimeout(() => res.end('data: {{"delta":"la"}}\\n\\ndata: [DONE]\\n\\n'), 300);
            return;
          }}
          if (req.url.startsWith('/api/hoard-copilot/conversations/missing')) return res.writeHead(404, {{ 'content-type': 'application/json' }}).end('{{"detail":"Conversation not found"}}');
          res.writeHead(500).end();
        }});
        await new Promise(r => faustus.listen(0, '127.0.0.1', r));
        const faustusUrl = 'http://127.0.0.1:' + faustus.address().port;
        process.env.HOARD_FAUSTUS_URL = faustusUrl;
        process.env.HOARD_FAUSTUS_TOKEN = 'secret-token';

        const app = express();
        app.use(express.json());
        app.get('/api/ping', (req, res) => res.json({{ ok: true }}));
        installSpa(app, {json.dumps(str(dist))}, {{ express }});
        family.configure({{ app: 'linkstest', dataDir: {json.dumps(str(data))}, enabled: false }});
        const server = await new Promise(r => {{ const s = app.listen(0, '127.0.0.1', () => r(s)); }});
        const base = 'http://127.0.0.1:' + server.address().port;
        const proxy = (envelope) => fetch(base + '/api/copilot/proxy', {{ method: 'POST', headers: {{ 'content-type': 'application/json' }}, body: JSON.stringify(envelope) }});

        const info = await (await fetch(base + '/api/copilot/info')).json();
        const page = await (await fetch(base + '/')).text();
        const deep = await (await fetch(base + '/some/client/route')).text();
        const launcher = await fetch(base + '/api/copilot/launcher.js');
        const launcherText = await launcher.text();
        const panel = await (await fetch(base + '/api/copilot/panel.js')).text();
        const t0 = Date.now(); const arrivals = [];
        const turn = await proxy({{ path: '/api/chat_stream', method: 'POST', headers: {{ 'X-Faustus-Client-Version': '2.0', Cookie: 'x' }},
          body: {{ session: 's', message: 'hola', hoard_context: {{ origin: {{ app_id: 'spoof', surface: 'page' }}, view: {{ route: '/' }} }} }} }});
        const reader = turn.body.getReader(); let streamed = '';
        for (;;) {{ const {{ done, value }} = await reader.read(); if (done) break; streamed += Buffer.from(value).toString(); arrivals.push(Date.now() - t0); }}
        const gone = await proxy({{ path: '/api/hoard-copilot/conversations/missing', method: 'GET', headers: {{}} }});
        const refused = await proxy({{ path: '/api/settings', method: 'GET' }});
        const sneaky = await proxy({{ path: '//evil.example/api/chat_stream', method: 'GET' }});
        const turnSeen = seen.find(s => s.path === '/api/chat_stream');
        const sent = JSON.parse(turnSeen.body);
        console.log(JSON.stringify({{
          info, faustusUrl, injected: page.includes('/api/copilot/launcher.js'), deepInjected: deep.includes('/api/copilot/launcher.js'),
          launcherType: launcher.headers.get('content-type'), launcherOk: launcherText.includes('hoard-copilot-toggle'),
          panel, ping: await (await fetch(base + '/api/ping')).json(),
          apiMissing: (await fetch(base + '/api/nothing')).status,
          streamed, firstChunkEarly: arrivals[0] < 250, runId: turn.headers.get('x-odysseus-run-id'), leaked: turn.headers.get('x-secret'),
          origin: sent.hoard_context.origin, auth: turnSeen.auth, version: turnSeen.version,
          cookieForwarded: JSON.stringify(seen).includes('Cookie'),
          gone: gone.status, refused: refused.status, sneaky: sneaky.status,
        }}));
        server.close(); faustus.close();
        process.exit(0);
    """, env={"HOARD_COPILOT": ""})
    space = (data / "copilot-space-id").read_text(encoding="utf-8").strip()
    assert out["info"] == {"app_id": "linkstest", "app_name": "Links' Test Hoard", "data_space_id": space,
                           "panel_url": "/api/copilot/panel.js", "server": "/", "tool_server_id": "linkstest",
                           "settings_url": out["faustusUrl"] + "/studio/settings"}
    assert out["injected"] and out["deepInjected"]
    assert out["launcherType"].startswith("application/javascript") and out["launcherOk"]
    assert out["panel"] == "export const panel = 1;"
    assert out["ping"] == {"ok": True} and out["apiMissing"] == 404
    assert out["streamed"].endswith("data: [DONE]\n\n") and out["firstChunkEarly"], "the SSE turn must stream, not buffer"
    assert out["runId"] == "run-1" and out["leaked"] is None
    assert out["origin"] == {"app_id": "linkstest", "surface": "page", "data_space_id": space}
    assert out["auth"] == "Bearer secret-token" and out["version"] == "2.0" and not out["cookieForwarded"]
    assert out["gone"] == 404 and out["refused"] == 400 and out["sneaky"] == 400


@needs_real
def test_opt_out_unconfigured_and_own_relay(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    out = run(f"""
        import {{ createRequire }} from "node:module";
        const require = createRequire({json.dumps(str(Path(REAL) / "package.json"))});
        const express = require("express");
        const {{ installSpa }} = await import({json.dumps(EXPRESS_MODULE)});
        const {{ installCopilot }} = await import({json.dumps(MODULE)});
        async function serve(setup) {{
          const app = express(); setup(app);
          const server = await new Promise(r => {{ const s = app.listen(0, '127.0.0.1', () => r(s)); }});
          const base = 'http://127.0.0.1:' + server.address().port;
          const result = {{ info: (await fetch(base + '/api/copilot/info')).status, page: await (await fetch(base + '/')).text() }};
          server.close(); return result;
        }}
        // No family.configure: the routes stay an ordinary 404 and the page is untouched.
        const unconfigured = await serve(app => installSpa(app, {json.dumps(str(dist))}, {{ express }}));
        const optedOut = await serve(app => installSpa(app, {json.dumps(str(dist))}, {{ express, copilot: false }}));
        const own = await serve(app => {{ app.get('/api/copilot/info', (req, res) => res.status(418).end()); installSpa(app, {json.dumps(str(dist))}, {{ express }}); }});
        const manual = express();
        const first = installCopilot(manual, {{ appId: 'cook', dataDir: {json.dumps(str(tmp_path / "cook"))}, faustusUrls: ['http://127.0.0.1:9'] }});
        const second = installCopilot(manual, {{ appId: 'cook', dataDir: {json.dumps(str(tmp_path / "cook"))} }});
        console.log(JSON.stringify({{ unconfigured, optedOut, own, same: first === second }}));
        process.exit(0);
    """)
    for case in ("unconfigured", "optedOut"):
        assert out[case]["info"] == 404 and "launcher.js" not in out[case]["page"]
    assert out["own"]["info"] == 418 and "launcher.js" not in out["own"]["page"]
    assert out["same"]
