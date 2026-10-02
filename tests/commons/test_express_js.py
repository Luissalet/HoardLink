"""js/hoard-commons/express.js: guard middleware, agent routes, SPA, error envelope, runServer and the MCP bridge.

Most tests use tiny fakes (an express-like router, an MCP server class) so they run with node alone. Set HOARD_TEST_NODE_MODULES to a folder
whose node_modules has `express` (4 or 5), `zod` and `@modelcontextprotocol/sdk` to also run the tests against the real packages."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from tests.commons.jsrun import JS_DIR, node

MODULE = (JS_DIR / "express.js").resolve().as_uri()
REAL = os.environ.get("HOARD_TEST_NODE_MODULES", "")
needs_real = pytest.mark.skipif(not REAL, reason="set HOARD_TEST_NODE_MODULES to a folder with express, zod and @modelcontextprotocol/sdk")

PRELUDE = r"""
import * as e from %(module)s;
import fs from "node:fs"; import path from "node:path"; import http from "node:http"; import os from "node:os";
import { createRequire } from "node:module";
const args = JSON.parse(process.argv[2]);

// --- an express-like fake: routes by method + string/regexp, use(), error middleware, enough for the commons
export class FakeApp {
  constructor() { this.stack = []; this.listening = null; }
  _add(method, pattern, handlers) { this.stack.push({ method, pattern, handlers }); return this; }
  get(p, ...h) { return this._add("GET", p, h); }
  post(p, ...h) { return this._add("POST", p, h); }
  all(p, ...h) { return this._add("ALL", p, h); }
  use(...h) { return this._add("USE", null, h); }
  _match(p, url) { return p === null || (p instanceof RegExp ? p.test(url) : p === url); }
  async handle(method, url, { headers = {}, body } = {}) {
    const res = { statusCode: 200, headers: {}, body: undefined, sent: false, headersSent: false,
      status(c) { this.statusCode = c; return this; }, setHeader(k, v) { this.headers[k.toLowerCase()] = v; return this; },
      json(b) { this.body = b; this.sent = true; this.headersSent = true; return this; },
      sendFile(f) { this.body = { file: f }; this.sent = true; this.headersSent = true; return this; } };
    const req = { method, url, path: url.split("?")[0], headers, body };
    let err = null;
    const chain = this.stack.filter((l) => (l.method === "USE" || l.method === "ALL" || l.method === method) && this._match(l.pattern, req.path)).flatMap((l) => l.handlers);
    for (const h of chain) {
      if (res.sent) break;
      if (err ? h.length === 4 : h.length < 4) {
        let nextCalled = false; let nextErr = null;
        const next = (e2) => { nextCalled = true; nextErr = e2 || null; };
        try { await (err ? h(err, req, res, next) : h(req, res, next)); } catch (x) { err = x; continue; }
        if (!res.sent && nextCalled) err = nextErr;
        else if (res.sent) err = null;
      }
    }
    if (err && !res.sent) { res.statusCode = 500; res.body = { unhandled: String(err.message) }; }
    return { status: res.statusCode, headers: res.headers, body: res.body };
  }
  listen(port, host, cb) {
    const self = this;
    const server = http.createServer(async (rq, rs) => { const r = await self.handle(rq.method, rq.url, { headers: rq.headers }); rs.writeHead(r.status, { "content-type": "application/json" }); rs.end(JSON.stringify(r.body)); });
    server.listen(port, host, () => cb && cb());
    this.listening = server;
    return server;
  }
}
async function main(...args) {
%(body)s
}
const out = await main(...args);
process.stdout.write(JSON.stringify(out === undefined ? null : out), () => process.exit(0));
"""


def run_node(body: str, *args, timeout: float = 60.0, env: dict | None = None):
    script = PRELUDE % {"module": json.dumps(MODULE), "body": textwrap.indent(textwrap.dedent(body), "  ")}
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "run.mjs"
        file.write_text(script, encoding="utf-8")
        done = subprocess.run([node(), str(file), json.dumps(list(args))], capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                              env={**os.environ, "NODE_NO_WARNINGS": "1", **(env or {})})
    assert done.returncode == 0, f"node failed:\n{done.stderr[-3000:]}"
    return json.loads(done.stdout)


# ------------------------------------------------------------------------------------------------ guard

def test_create_guard_middleware():
    out = run_node("""
        const calls = [];
        const mk = (opts) => e.createGuard(opts);
        const run = (guard, req) => { let status = null, body = null, nexted = false;
          guard({ method: "GET", ...req }, { status(c) { status = c; return this; }, json(b) { body = b; } }, () => { nexted = true; });
          return { status, body, nexted }; };
        const g = mk({ port: 5190 });
        const r1 = run(g, { headers: { host: "localhost:5190" } });
        const r2 = run(g, { headers: { host: "evil.com" } });
        const r3 = run(g, { method: "POST", headers: { host: "localhost:5190", origin: "https://evil.com" } });
        let port = 5190; const strict = mk({ portGetter: () => port, strictPorts: true });
        const r4 = run(strict, { headers: { host: "localhost:5190" } });
        port = 5191;
        const r5 = run(strict, { headers: { host: "localhost:5190" } });
        const lan = mk({ port: 5190, allowedHosts: "nas.local, *.ts.net" });
        const r6 = run(lan, { headers: { host: "pc.x.ts.net" } });
        const r7 = run(mk({ port: 1, allowedHosts: ["nas.local"] }), { headers: { host: "nas.local:1" } });
        const r8 = run(mk(), { headers: { host: "127.0.0.1:9" } });
        return [r1, r2, r3, r4, r5, r6, r7, r8];
    """)
    assert out[0] == {"status": None, "body": None, "nexted": True}
    assert out[1] == {"status": 403, "body": {"error": "Only local access is allowed."}, "nexted": False}
    assert out[2]["status"] == 403 and out[2]["body"] == {"error": "Origin not allowed."}
    assert out[3]["nexted"] is True and out[4]["status"] == 403
    assert out[5]["nexted"] and out[6]["nexted"] and out[7]["nexted"]


# ------------------------------------------------------------------------------------------------ agent routes

AGENT = r"""
const fakeZ = { toJSONSchema: (schema) => ({ type: "object", properties: Object.fromEntries(schema.keys.map((k) => [k, { type: "string" }])) }) };
const zodLike = (keys) => ({ keys, parse(v) {
  const missing = keys.filter((k) => !(k in v)); if (missing.length) throw Object.assign(new Error("bad"), { issues: missing.map((k) => ({ path: [k], message: "Required" })) }); return v; } });
const tools = [
  { name: "echo", description: "Echo.", annotations: { readOnlyHint: true }, schema: zodLike(["text"]), run: async (a) => ({ echo: a.text }) },
  { name: "big", description: "Big.", annotations: { readOnlyHint: true }, schema: zodLike([]), timeoutMs: 120000, run: () => ({ rows: Array.from({ length: 1000 }, (_, i) => "row-" + i + "-" + "x".repeat(90)) }) },
  { name: "list", description: "List.", annotations: {}, schema: null, run: () => [1, 2, 3] },
  { name: "nothing", description: "Nothing.", annotations: {}, schema: null, run: () => undefined },
  { name: "denied", description: "Denied.", annotations: {}, schema: null, run: () => { throw Object.assign(new Error("Not yours."), { status: 403, code: "forbidden", hint: "Ask.", candidates: ["a"] }); } },
  { name: "broken", description: "Broken.", annotations: {}, schema: null, run: () => { throw new TypeError("internal secret"); } },
  { name: "visible", description: "Visible.", annotations: {}, schema: null, run: () => { throw Object.assign(new Error("Shown as is."), { status: 500, expose: true }); } },
];
const events = [];
const post = (app, name, a, headers) => app.handle("POST", "/api/agent/call", { headers: { authorization: "Bearer " + "k".repeat(40), ...(headers || {}) }, body: { name, arguments: a } });
"""


def test_agent_routes_catalog_and_calls():
    out = run_node(AGENT + """
        const routes = e.makeAgentRoutes({ app: "demo", tools, z: fakeZ, token: "k".repeat(40), instructions: "Use me.", recordCall: (...a) => events.push(a) });
        const app = new FakeApp(); routes.install(app);
        const catalog = await app.handle("GET", "/api/agent/tools");
        const results = {};
        for (const [name, a] of [["echo", { text: "hi" }], ["big", {}], ["list", {}], ["nothing", {}], ["denied", {}], ["broken", {}], ["visible", {}], ["echo", {}], ["nope", {}]])
          results[name + (a.text ? "" : "-" + Object.keys(a).length)] = await post(app, name, a);
        results.noName = await app.handle("POST", "/api/agent/call", { headers: { authorization: "Bearer " + "k".repeat(40) }, body: {} });
        results.noBody = await app.handle("POST", "/api/agent/call", { headers: { authorization: "Bearer " + "k".repeat(40) } });
        results.lower = await post(app, "echo", { text: "x" }, { authorization: "bearer " + "k".repeat(40) });
        results.bad = await post(app, "echo", { text: "x" }, { authorization: "Bearer wrong" });
        results.none = await post(app, "echo", { text: "x" }, { authorization: "" });
        return { catalog, results, events: events.map(([n, ok, ms, o]) => [n, ok, typeof ms, o.caller, o.error]) };
    """)
    cat = out["catalog"]["body"]
    assert cat["instructions"] == "Use me." and cat["app"] == "demo"
    assert [t["name"] for t in cat["tools"]][:2] == ["echo", "big"]
    assert cat["tools"][0]["inputSchema"] == {"type": "object", "properties": {"text": {"type": "string"}}} and cat["tools"][1]["x-timeout-s"] == 120
    assert "x-timeout-s" not in cat["tools"][0] and cat["tools"][2]["inputSchema"] == {"type": "object", "properties": {}}
    r = out["results"]
    assert r["echo"]["body"] == {"echo": "hi"} and r["lower"]["status"] == 200
    assert len(r["big-0"]["body"]["rows"]) < 1000 and r["big-0"]["body"]["truncated"]["original_lengths"] == {"rows": 1000}
    assert r["list-0"]["body"] == {"result": [1, 2, 3]} and r["nothing-0"]["body"] == {"result": None}
    assert r["denied-0"]["status"] == 403 and r["denied-0"]["body"] == {"error": "Not yours.", "code": "forbidden", "hint": "Ask.", "candidates": ["a"]}
    assert r["broken-0"]["status"] == 500 and r["broken-0"]["body"]["code"] == "internal" and "TypeError: internal secret" in r["broken-0"]["body"]["error"]
    assert r["visible-0"]["status"] == 500 and r["visible-0"]["body"] == {"error": "Shown as is."}
    assert r["echo-0"]["status"] == 400 and r["echo-0"]["body"] == {"error": "text: Required", "code": "invalid_arguments", "issues": [{"loc": "text", "msg": "Required"}]}
    assert r["nope-0"]["status"] == 404 and r["nope-0"]["body"]["code"] == "unknown_tool"
    assert r["noName"]["status"] == 400 and r["noBody"]["status"] == 400
    for key in ("bad", "none"):
        assert r[key]["status"] == 401 and r[key]["body"] == {"error": "Invalid MCP token.", "code": "unauthorized"}
    names = [e[0] for e in out["events"]]
    assert names.count("echo") == 3 and "nope" in names                                  # unauthorised calls are not audited
    assert ["echo", False, "number", "", "text: Required"] in out["events"]


def test_agent_routes_options():
    out = run_node(AGENT + """
        const seen = [];
        const routes = e.makeAgentRoutes({ tools: [], callTool: async (name, a) => { seen.push([name, a]); return { ok: true }; }, tokenGetter: () => "k".repeat(40), capLimit: 100,
          recordCall: () => { throw new Error("audit down"); } });
        const app = new FakeApp(); routes.install(app);
        const a = await post(app, "anything", { v: 1 });
        const caller = await app.handle("POST", "/api/agent/call", { headers: { authorization: "Bearer " + "k".repeat(40) }, body: { name: "x", arguments: null, caller: "faustus" } });
        const tiny = e.makeAgentRoutes({ tools: [], callTool: () => ({ rows: Array.from({ length: 50 }, () => "y".repeat(40)) }), token: "k".repeat(40), capLimit: 500 });
        const app2 = new FakeApp(); tiny.install(app2);
        const capped = await post(app2, "any", {});
        const none = e.makeAgentRoutes({ tools: [{ name: "t", description: "d" }] });
        return { a, caller, seen, capped: capped.body.rows.length, cat: none.catalog() };
    """)
    assert out["a"]["body"] == {"ok": True} and out["caller"]["status"] == 200           # a failing audit trail never fails the call
    assert out["seen"] == [["anything", {"v": 1}], ["x", {}]]
    assert 0 < out["capped"] < 50
    assert out["cat"] == [{"name": "t", "description": "d", "annotations": {}, "inputSchema": {"type": "object", "properties": {}}}]


def test_a_missing_token_never_authorises():
    out = run_node(AGENT + """
        const routes = e.makeAgentRoutes({ tools });
        const app = new FakeApp(); routes.install(app);
        return await app.handle("POST", "/api/agent/call", { headers: { authorization: "Bearer " }, body: { name: "echo", arguments: { text: "x" } } });
    """)
    assert out["status"] == 401


# ------------------------------------------------------------------------------------------------ SPA and errors

SPA = r"""
const dist = fs.mkdtempSync(path.join(os.tmpdir(), "spa-"));
fs.mkdirSync(path.join(dist, "assets"));
fs.writeFileSync(path.join(dist, "index.html"), "<html>app</html>");
fs.writeFileSync(path.join(dist, "assets", "app-AbCd1234.js"), "x");
const staticCalls = [];
const express = { static: (dir, opts) => { staticCalls.push(opts); return function serveStatic(req, res, next) {
  const f = path.join(dir, req.path); if (fs.existsSync(f) && fs.statSync(f).isFile()) { opts.setHeaders(res, f); res.sent = true; res.body = { served: path.basename(f) }; return; } next(); }; } };
"""


def test_install_spa_with_a_fake_express():
    out = run_node(SPA + """
        const app = new FakeApp();
        app.get("/api/ping", (req, res) => res.json({ pong: true }));
        e.installSpa(app, dist, { express });
        const get = (p, m = "GET") => app.handle(m, p);
        const built = { root: await get("/"), route: await get("/a/b"), api: await get("/api/ping"), apiMissing: await get("/api/nothing"), apiPost: await get("/api/x", "POST"),
          asset: await get("/assets/app-AbCd1234.js"), missingAsset: await get("/assets/gone.js"), missingCss: await get("/x/gone.css"), docs: await get("/docs/v1") };
        fs.rmSync(path.join(dist, "index.html"));
        const unbuilt = { root: await get("/"), api: await get("/api/zzz") };
        let thrown = ""; try { e.installSpa(new FakeApp(), dist, {}); } catch (x) { thrown = x.message; }
        return { built, unbuilt, thrown, index: path.join(dist, "index.html"), opts: staticCalls[0].dotfiles };
    """)
    b = out["built"]
    assert b["root"]["body"] == {"file": out["index"]} and b["root"]["headers"]["cache-control"] == "no-cache"
    assert b["route"]["body"] == {"file": out["index"]} and b["docs"]["body"] == {"file": out["index"]}
    assert b["api"]["body"] == {"pong": True}
    assert b["apiMissing"] == {"status": 404, "headers": {}, "body": {"error": "Not found.", "code": "not_found"}} and b["apiPost"]["status"] == 404
    assert b["asset"]["headers"]["cache-control"] == "public, max-age=31536000, immutable"
    assert b["missingAsset"]["status"] == 404 and b["missingCss"]["status"] == 404
    assert out["unbuilt"]["root"]["status"] == 503 and out["unbuilt"]["root"]["body"]["code"] == "not_built" and out["unbuilt"]["api"]["status"] == 404
    assert "express" in out["thrown"] and out["opts"] == "ignore"


def test_install_error_handlers_envelope():
    out = run_node("""
        const app = new FakeApp();
        const boom = (err) => (req, res, next) => { next(err); };
        const make = (path, err) => app.get(path, boom(err));
        make("/json", Object.assign(new Error("Unexpected token"), { type: "entity.parse.failed" }));
        make("/large", Object.assign(new Error("big"), { type: "entity.too.large" }));
        make("/zod", Object.assign(new Error("z"), { issues: [{ path: ["a", 0, "b"], message: "Required" }, { path: [], message: "Bad" }] }));
        make("/status", Object.assign(new Error("Nope."), { status: 409, code: "conflict", hint: "Retry", details: { id: 1 } }));
        make("/crash", new RangeError("oops"));
        const logged = [];
        e.installErrorHandlers(app, { log: { error: (...a) => logged.push(a.length) } });
        const r = {}; for (const p of ["json", "large", "zod", "status", "crash"]) r[p] = await app.handle("GET", "/" + p);
        return { r, logged };
    """)
    r = out["r"]
    assert r["json"]["status"] == 400 and r["json"]["body"] == {"error": "Invalid JSON.", "code": "invalid_json"}
    assert r["large"]["status"] == 413 and r["large"]["body"]["code"] == "too_large"
    assert r["zod"]["status"] == 400 and r["zod"]["body"] == {"error": "a.0.b: Required; input: Bad", "code": "invalid_arguments",
                                                               "issues": [{"loc": "a.0.b", "msg": "Required"}, {"loc": "input", "msg": "Bad"}]}
    assert r["status"]["status"] == 409 and r["status"]["body"] == {"error": "Nope.", "code": "conflict", "hint": "Retry", "details": {"id": 1}}
    assert r["crash"]["status"] == 500 and r["crash"]["body"]["code"] == "internal" and "RangeError: oops" in r["crash"]["body"]["error"]
    assert out["logged"] == [2]                                                          # only the 500 is logged


# ------------------------------------------------------------------------------------------------ runServer

def test_run_server_shutdown_sequence_and_force_exit():
    out = run_node("""
        const order = []; const exits = [];
        const app = new FakeApp();
        const handle = await e.runServer({ service: "demo", createApp: () => ({ app }), port: 0, onShutdown: async (s) => { order.push("hook:" + s); }, exit: (c) => exits.push(c), signals: false,
          log: { log: (m) => order.push(m), error: () => {} } });
        const reply = await new Promise((resolve) => http.get({ port: handle.port, host: "127.0.0.1", path: "/x" }, (r) => { let d = ""; r.on("data", (c) => (d += c)); r.on("end", () => resolve(d)); }));
        const first = handle.shutdown("SIGTERM"); const second = handle.shutdown("SIGINT");
        await first;
        return { port: handle.port > 0, reply, order, exits, same: first === second, closed: !handle.server.listening };
    """)
    assert out["port"] and out["same"] and out["closed"] and out["exits"] == [0]
    assert out["order"] == ["Closing demo (SIGTERM)…", "hook:SIGTERM"]


def test_run_server_force_exits_when_the_hook_hangs():
    out = run_node("""
        const exits = [];
        const handle = await e.runServer({ createApp: () => new FakeApp(), port: 0, onShutdown: () => new Promise(() => {}), forceMs: 100, exit: (c) => exits.push(c), signals: false, log: { log() {}, error() {} } });
        handle.shutdown("SIGTERM");
        await new Promise((r) => setTimeout(r, 400));
        return exits;
    """)
    assert out == [0]


def test_run_server_rejects_when_the_port_is_taken():
    out = run_node("""
        const first = await e.runServer({ createApp: () => new FakeApp(), port: 0, signals: false });
        let message = "";
        try { await e.runServer({ createApp: () => new FakeApp(), port: first.port, signals: false }); } catch (x) { message = x.code; }
        first.server.close();
        return message;
    """)
    assert out == "EADDRINUSE"


def test_run_server_handles_signals():
    script = textwrap.dedent(f"""
        import * as e from {json.dumps(MODULE)};
        import http from "node:http";
        const app = {{ listen: (port, host, cb) => {{ const s = http.createServer((q, r) => r.end("ok")); s.listen(port, host, cb); return s; }} }};
        const h = await e.runServer({{ service: "sig", createApp: () => app, port: 0, onShutdown: () => console.log("hook ran"), forceMs: 5000 }});
        console.log("ready");
        setInterval(() => {{}}, 1000);
    """)
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "sig.mjs"
        file.write_text(script, encoding="utf-8")
        child = subprocess.Popen([node(), str(file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ready"
            if os.name == "nt":
                pytest.skip("POSIX signals")
            child.terminate()
            out, err = child.communicate(timeout=20)
            assert child.returncode == 0 and "hook ran" in out and "Closing sig (SIGTERM)" in out, (out, err)
        finally:
            child.kill()


# ------------------------------------------------------------------------------------------------ bridge

BRIDGE = r"""
class FakeMcpServer {
  constructor(info, options) { this.info = info; this.options = options; this.tools = new Map(); this.connected = null; }
  registerTool(name, config, handler) { this.tools.set(name, { config, handler }); }
  async connect(t) { this.connected = t; }
}
class FakeTransport {}
const tokenFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "br-")), "mcp-token"); fs.writeFileSync(tokenFile, "k".repeat(40));
const hits = [];
const fake = http.createServer((req, res) => {
  let data = ""; req.on("data", (c) => (data += c)); req.on("end", () => {
    const body = JSON.parse(data); hits.push({ auth: req.headers.authorization, body });
    const reply = (status, o, raw) => { res.writeHead(status, { "content-type": "application/json" }); res.end(raw ?? JSON.stringify(o)); };
    if (req.headers.authorization !== "Bearer " + "k".repeat(40)) return reply(401, { error: "Invalid MCP token." });
    switch (body.name) {
      case "echo": return reply(200, { echo: body.arguments });
      case "slow": case "slow_write": return setTimeout(() => reply(200, { late: true }), Number(body.arguments.ms || 1500));
      case "fail": return reply(400, { error: "Bad.", code: "invalid", hint: "Fix.", issues: [1], details: { d: 1 }, candidates: ["c"], secret: "no" });
      case "html": return reply(500, null, "<html>oops</html>");
      case "hang": return req.socket.destroy();
      default: return reply(404, { error: "Unknown tool.", code: "unknown_tool" });
    }
  });
});
await new Promise((r) => fake.listen(0, "127.0.0.1", r));
const base = "http://127.0.0.1:" + fake.address().port;
const tools = ["echo", "slow", "slow_write", "fail", "html", "hang"].map((n) => ({ name: n, description: n, schema: { shape: n }, annotations: { readOnlyHint: !["slow_write", "hang"].includes(n) }, ...(n === "slow" ? { timeoutMs: 300 } : {}) }));
const make = (extra = {}) => e.createBridge({ app: "fake", service: "fake-hoard", version: "1.2.3", McpServer: FakeMcpServer, StdioServerTransport: FakeTransport, tools, instructions: "Hello.",
  baseUrl: base, tokenFile, defaultTimeoutMs: 300, heartbeatMs: 50, messages: { title: "Fake's Hoard" }, ...extra });
const body = (r) => JSON.parse(r.content[0].text);
"""


def test_bridge_registers_tools_and_proxies_calls():
    out = run_node(BRIDGE + """
        const b = make();
        const reg = [...b.server.tools].map(([n, t]) => [n, t.config.description, t.config.inputSchema, t.config.annotations]);
        await b.start();
        const results = {};
        results.echo = await b.handlers.get("echo")({ a: 1 }, {});
        results.fail = await b.handlers.get("fail")({}, {});
        results.html = await b.handlers.get("html")({}, {});
        results.slow = await b.handlers.get("slow")({ ms: 1500 }, {});
        results.slowWrite = await b.handlers.get("slow_write")({ ms: 1500 }, {});
        results.hang = await b.handlers.get("hang")({}, {});
        results.viaCall = await b.call("echo", { z: 2 });
        const wrongToken = make({ tokenFile: "", token: "w".repeat(40) });
        results.refused = await wrongToken.call("echo", {});
        const noToken = make({ tokenFile: path.join(os.tmpdir(), "does-not-exist-" + Date.now()) });
        results.noToken = await noToken.call("echo", {});
        const down = make({ baseUrl: "http://127.0.0.1:1" });
        results.down = await down.call("hang", {});
        fake.close();
        return { reg, info: b.server.info, options: b.server.options, connected: b.server.connected !== null, results, auth: hits[0].auth, first: hits[0].body };
    """)
    assert out["info"] == {"name": "fake-hoard", "version": "1.2.3"} and out["options"] == {"instructions": "Hello."} and out["connected"] is True
    assert [r[0] for r in out["reg"]] == ["echo", "slow", "slow_write", "fail", "html", "hang"]
    assert out["reg"][0][1:3] == ["echo", {"shape": "echo"}] and out["reg"][0][3] == {"readOnlyHint": True}
    r = out["results"]
    parse = lambda x: json.loads(x["content"][0]["text"])  # noqa: E731
    assert parse(r["echo"]) == {"echo": {"a": 1}} and "isError" not in r["echo"] and parse(r["viaCall"]) == {"echo": {"z": 2}}
    assert r["fail"]["isError"] is True and parse(r["fail"]) == {"error": "Bad.", "code": "invalid", "hint": "Fix.", "issues": [1], "details": {"d": 1}, "candidates": ["c"]}
    assert "oops" in parse(r["html"])["error"] and r["html"]["isError"]
    assert r["slow"]["isError"] and "did not answer" in parse(r["slow"])["error"] and "outcome_unknown" not in parse(r["slow"])
    assert parse(r["slowWrite"])["outcome_unknown"] is True and parse(r["slowWrite"])["status"] == "outcome_unknown"
    assert parse(r["slowWrite"])["reconcile_action"] == "read_current_state_before_retry" and parse(r["slowWrite"])["code"] == "outcome_unknown"
    assert parse(r["hang"])["outcome_unknown"] is True
    assert parse(r["refused"])["code"] == "token_refused" and "Fake's Hoard" in parse(r["refused"])["error"]
    assert parse(r["noToken"])["code"] == "no_token"
    assert parse(r["down"])["code"] == "not_running" and "outcome_unknown" not in parse(r["down"])          # never reached the app: safe to retry
    assert out["auth"] == "Bearer " + "k" * 40 and out["first"] == {"name": "echo", "arguments": {"a": 1}}


def test_bridge_heartbeats_and_timeouts():
    out = run_node(BRIDGE + """
        const notes = [];
        const extra = { _meta: { progressToken: "tok" }, sendNotification: async (n) => { notes.push(n); } };
        const b = make({ callTimeoutMs: (name, args) => (name === "slow" ? 5000 : 0) });
        const slow = await b.handlers.get("slow")({ ms: 330 }, extra);
        const quick = notes.length;
        notes.length = 0;
        await b.handlers.get("echo")({}, extra);
        const none = notes.length;
        const noToken = []; await b.handlers.get("slow")({ ms: 200 }, { sendNotification: async (n) => noToken.push(n) });
        const failing = await b.handlers.get("slow")({ ms: 200 }, { _meta: { progressToken: 1 }, sendNotification: async () => { throw new Error("client gone"); } });
        fake.close();
        return { slow, notes: quick, none, noToken: noToken.length, failing };
    """)
    assert json.loads(out["slow"]["content"][0]["text"]) == {"late": True}                 # callTimeoutMs raised the 300 ms tool timeout
    assert out["notes"] >= 3 and out["none"] == 0 and out["noToken"] == 0
    assert json.loads(out["failing"]["content"][0]["text"]) == {"late": True}


def test_bridge_url_and_token_sources():
    out = run_node(BRIDGE + """
        const urlFile = path.join(path.dirname(tokenFile), "url"); const portFile = path.join(path.dirname(tokenFile), "port");
        fs.writeFileSync(urlFile, base + "\\n"); fs.writeFileSync(portFile, String(fake.address().port));
        const viaUrl = await make({ baseUrl: "", urlFile }).call("echo", { a: 1 });
        const viaPort = await make({ baseUrl: "", portFile }).call("echo", { a: 2 });
        const viaFn = await make({ baseUrl: () => base, tokenFile: "", token: () => "k".repeat(40) }).call("echo", { a: 3 });
        const viaDefault = await make({ baseUrl: "", defaultPort: fake.address().port }).call("echo", { a: 4 });
        let thrown = ""; try { make({ baseUrl: "http://example.com:80" }); } catch (x) { thrown = x.message; }
        const thrownLater = body(await make({ baseUrl: () => "https://localhost:1" }).call("echo", {})).error;
        let nosdk = ""; try { e.createBridge({ tools: [] }); } catch (x) { nosdk = x.message; }
        const nobase = await make({ baseUrl: "" }).call("echo", {});
        fake.close();
        return { viaUrl, viaPort, viaFn, viaDefault, thrown, thrownLater, nosdk, nobase };
    """)
    for key, n in (("viaUrl", 1), ("viaPort", 2), ("viaFn", 3), ("viaDefault", 4)):
        assert json.loads(out[key]["content"][0]["text"]) == {"echo": {"a": n}}, key
    assert "only connects to the local server" in out["thrown"] and "McpServer" in out["nosdk"]
    assert "only connects to the local server" in out["thrownLater"]       # a function URL is checked per call, and reported as the call's error
    assert json.loads(out["nobase"]["content"][0]["text"])["code"] == "not_running"


def test_post_json_reports_whether_the_connection_was_made():
    out = run_node("""
        const server = http.createServer((req, res) => { if (req.url === "/drop") return req.socket.destroy(); res.writeHead(200); res.end("not json"); });
        await new Promise((r) => server.listen(0, "127.0.0.1", r));
        const base = "http://127.0.0.1:" + server.address().port;
        const ok = await e.postJson(base, "/x", { a: 1 });
        const drop = await e.postJson(base, "/drop", {}).catch((x) => ({ code: x.code, connected: x.connected }));
        const refused = await e.postJson("http://127.0.0.1:1", "/x", {}).catch((x) => ({ code: x.code, connected: x.connected }));
        server.close();
        return { ok, drop, refused };
    """)
    assert out["ok"] == {"status": 200, "ok": True, "body": None, "raw": "not json"}
    assert out["drop"]["connected"] is True and out["refused"] == {"code": "ECONNREFUSED", "connected": False}


# ------------------------------------------------------------------------------------------------ the real packages (optional)

REAL_PRELUDE = r"""
const req = createRequire(path.join(%(real)s, "x.js"));
const express = req("express"); const zod = req("zod"); const z = zod.z ?? zod;
"""


@needs_real
def test_real_express_end_to_end():
    out = run_node(REAL_PRELUDE % {"real": json.dumps(REAL)} + r"""
        const dist = fs.mkdtempSync(path.join(os.tmpdir(), "real-"));
        fs.mkdirSync(path.join(dist, "assets"));
        fs.writeFileSync(path.join(dist, "index.html"), "<html>real app</html>");
        fs.writeFileSync(path.join(dist, "assets", "app-AbCd1234.js"), "export {}");
        fs.writeFileSync(path.join(dist, ".hidden"), "nope");
        fs.writeFileSync(path.join(path.dirname(dist), "outside-secret.txt"), "TOP SECRET");
        const tools = [
          { name: "echo", description: "Echo.", annotations: { readOnlyHint: true }, schema: z.object({ text: z.string() }), run: (a) => ({ echo: a.text }) },
          { name: "big", description: "Big.", annotations: { readOnlyHint: true }, schema: z.object({}), run: () => ({ rows: Array.from({ length: 1000 }, (_, i) => "r" + i + "x".repeat(90)) }) },
        ];
        const token = "t".repeat(40);
        const build = () => {
          const app = express();
          app.disable("x-powered-by");
          app.use(e.createGuard({ portGetter: () => app.port }));
          app.use(express.json({ limit: "1mb" }));
          e.makeAgentRoutes({ app: "real", tools, z, token, instructions: "i" }).install(app);
          app.get("/api/ping", (req, res) => res.json({ pong: true }));
          e.installSpa(app, dist, { express });
          e.installErrorHandlers(app, { log: { error() {} } });
          return app;
        };
        const app = build();
        const handle = await e.runServer({ createApp: () => app, port: 0, signals: false, exit: () => {}, log: { log() {}, error() {} } });
        app.port = handle.port;
        const call = (method, p, { headers = {}, body, host } = {}) => new Promise((resolve, reject) => {
          const data = body === undefined ? null : (typeof body === "string" ? body : JSON.stringify(body));
          const rq = http.request({ host: "127.0.0.1", port: handle.port, path: p, method, headers: { ...(data ? { "content-type": "application/json", "content-length": Buffer.byteLength(data) } : {}), ...(host ? { host } : {}), ...headers } }, (rs) => {
            let d = ""; rs.on("data", (c) => (d += c)); rs.on("end", () => { let j = null; try { j = JSON.parse(d); } catch {} resolve({ status: rs.statusCode, headers: rs.headers, text: d, json: j }); });
          });
          rq.on("error", reject); if (data) rq.write(data); rq.end();
        });
        const auth = { authorization: "Bearer " + token };
        const r = {};
        r.index = await call("GET", "/"); r.route = await call("GET", "/deep/link"); r.asset = await call("GET", "/assets/app-AbCd1234.js");
        r.missing = await call("GET", "/assets/gone-12345678.js"); r.api404 = await call("GET", "/api/none"); r.ping = await call("GET", "/api/ping");
        r.hidden = await call("GET", "/.hidden"); r.trav = await call("GET", "/..%2foutside-secret.txt"); r.trav2 = await call("GET", "/%2e%2e/outside-secret.txt");
        r.host = await call("GET", "/api/ping", { host: "evil.com" });
        r.tools = await call("GET", "/api/agent/tools");
        r.echo = await call("POST", "/api/agent/call", { headers: auth, body: { name: "echo", arguments: { text: "hi" } } });
        r.invalid = await call("POST", "/api/agent/call", { headers: auth, body: { name: "echo", arguments: { text: 5 } } });
        r.big = await call("POST", "/api/agent/call", { headers: auth, body: { name: "big", arguments: {} } });
        r.unauth = await call("POST", "/api/agent/call", { body: { name: "echo", arguments: {} } });
        r.badjson = await call("POST", "/api/agent/call", { headers: auth, body: "{not json" });
        await handle.shutdown("test");
        return { r, srv: handle.server.listening, xp: express.application ? "ok" : "ok" };
    """)
    r = out["r"]
    assert r["index"]["text"] == "<html>real app</html>" and r["index"]["headers"]["cache-control"] == "no-cache" and r["route"]["text"] == r["index"]["text"]
    assert r["asset"]["headers"]["cache-control"] == "public, max-age=31536000, immutable" and r["asset"]["text"] == "export {}"
    assert r["missing"]["status"] == 404 and r["api404"]["json"]["code"] == "not_found" and r["ping"]["json"] == {"pong": True}
    for key in ("hidden", "trav", "trav2"):
        assert "nope" not in r[key]["text"] and "TOP SECRET" not in r[key]["text"], key
    assert r["host"]["status"] == 403 and r["host"]["json"]["error"] == "Only local access is allowed."
    assert [t["name"] for t in r["tools"]["json"]["tools"]] == ["echo", "big"] and r["tools"]["json"]["tools"][0]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert r["echo"]["json"] == {"echo": "hi"} and r["invalid"]["status"] == 400 and r["invalid"]["json"]["code"] == "invalid_arguments"
    assert "text" in r["invalid"]["json"]["error"] and len(r["big"]["json"]["rows"]) < 1000 and r["unauth"]["status"] == 401
    assert r["badjson"]["status"] == 400 and r["badjson"]["json"] == {"error": "Invalid JSON.", "code": "invalid_json"}


@needs_real
def test_real_mcp_sdk_bridge_over_stdio(tmp_path):
    """The injected SDK classes really register the tools, list them over stdio and proxy a call (with a progress heartbeat)."""
    app = tmp_path / "bridge.mjs"
    app.write_text(textwrap.dedent(f"""
        import http from "node:http"; import fs from "node:fs"; import path from "node:path"; import {{ createRequire }} from "node:module";
        import * as e from {json.dumps(MODULE)};
        const req = createRequire(path.join({json.dumps(REAL)}, "x.js"));
        const {{ McpServer }} = req("@modelcontextprotocol/sdk/server/mcp.js");
        const {{ StdioServerTransport }} = req("@modelcontextprotocol/sdk/server/stdio.js");
        const {{ z }} = req("zod");
        const fake = http.createServer((rq, rs) => {{ let d = ""; rq.on("data", (c) => (d += c)); rq.on("end", () => {{ const b = JSON.parse(d);
          setTimeout(() => {{ rs.writeHead(200, {{ "content-type": "application/json" }}); rs.end(JSON.stringify({{ echo: b.arguments, auth: rq.headers.authorization }})); }}, 350); }}); }});
        await new Promise((r) => fake.listen(0, "127.0.0.1", r));
        const tools = [{{ name: "echo", description: "Echo.", annotations: {{ readOnlyHint: true }}, schema: {{ text: z.string() }} }}];
        const bridge = e.createBridge({{ app: "real", service: "real-hoard", McpServer, StdioServerTransport, tools, instructions: "hi", baseUrl: "http://127.0.0.1:" + fake.address().port,
          token: "s".repeat(40), heartbeatMs: 100 }});
        await bridge.start();
    """), encoding="utf-8")
    child = subprocess.Popen([node(), str(app)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = []

    def send(obj):
        child.stdin.write(json.dumps(obj) + "\n")
        child.stdin.flush()

    def read_until(pred, limit=30):
        for _ in range(limit):
            line = child.stdout.readline()
            if not line:
                raise AssertionError(child.stderr.read())
            msg = json.loads(line)
            lines.append(msg)
            if pred(msg):
                return msg
        raise AssertionError(lines)

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        init = read_until(lambda m: m.get("id") == 1)
        assert init["result"]["serverInfo"]["name"] == "real-hoard" and init["result"]["instructions"] == "hi"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = read_until(lambda m: m.get("id") == 2)["result"]["tools"]
        assert listed[0]["name"] == "echo" and listed[0]["inputSchema"]["properties"]["text"]["type"] == "string" and listed[0]["annotations"]["readOnlyHint"] is True
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello"}, "_meta": {"progressToken": "p"}}})
        done = read_until(lambda m: m.get("id") == 3)
        assert json.loads(done["result"]["content"][0]["text"]) == {"echo": {"text": "hello"}, "auth": "Bearer " + "s" * 40}
        assert any(m.get("method") == "notifications/progress" for m in lines)
    finally:
        child.kill()
