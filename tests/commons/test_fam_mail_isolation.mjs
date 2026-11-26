import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import * as family from "../../js/hoard-link.js";

const INSTALLED_HUB = "https://mail-install.example.test";
const TEST_HUB = "https://mail-test.example.test:9999";
const NO_GATEWAY = { mailAvailable: async () => false };

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fam-mail-isolation-"));
  const installed = path.join(root, "installed");
  const testData = path.join(root, "installed-qa");
  const nested = path.join(installed, "modules", "mail");
  for (const folder of [nested, testData]) fs.mkdirSync(folder, { recursive: true });
  const env = process.env;
  const keys = ["HOARD_MAIL_REAL_HUB_URLS", "HOARD_MAIL_REAL_DATA_DIRS", "HOARD_TOKEN_FILE"];
  const previous = Object.fromEntries(keys.map((key) => [key, env[key]]));
  env.HOARD_MAIL_REAL_HUB_URLS = "bad-url," + INSTALLED_HUB + ":443/config";
  env.HOARD_MAIL_REAL_DATA_DIRS = ["relative-root", installed].join(path.delimiter);
  delete env.HOARD_TOKEN_FILE;
  t.after(() => {
    for (const key of keys) {
      if (previous[key] === undefined) delete env[key];
      else env[key] = previous[key];
    }
    family.configure({ hub: TEST_HUB });
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, nested, testData };
}

test("the direct runner blocks every action before helper discovery or spawn", async (t) => {
  const { nested, testData } = fixture(t);
  let discoveries = 0;
  let spawns = 0;
  const run = family.spawnHelperRunner({
    helperPath: "unused",
    setting: () => { discoveries++; },
    spawnFn: () => { spawns++; },
    discovery: { searchParents: false, commonPaths: [] },
  });
  for (const [hub, dataDir, isolated] of [[TEST_HUB, nested, "hub"], [INSTALLED_HUB, testData, "data"]]) {
    family.configure({ hub, dataDir });
    for (const action of ["status", "scan", "fetch", "headers", "read", "send"]) {
      const answer = await run(action);
      assert.equal(answer.ok, false);
      assert.equal(answer.isolated, isolated);
      assert.match(answer.error, /correo desactivado.*mail is off/);
    }
  }
  assert.equal(discoveries, 0);
  assert.equal(spawns, 0);
});

test("auto and explicit helper modes return empty mail when the installation checks fail", async (t) => {
  const { nested, testData } = fixture(t);
  let calls = 0;
  let availabilityChecks = 0;
  for (const [hub, dataDir, isolated] of [[TEST_HUB, nested, "hub"], [INSTALLED_HUB, testData, "data"]]) {
    family.configure({ hub, dataDir });
    for (const mode of ["auto", "faustus"]) {
      const router = family.famMailRouter({
        hub: NO_GATEWAY,
        sourceGetter: () => mode,
        runHelper: async () => { calls++; return { ok: true, messages: [{ id: 1 }] }; },
        helperAvailable: async () => { availabilityChecks++; return true; },
        deepUsesHelper: true,
      });
      const status = await router.status();
      assert.equal(status.helper_available, false);
      assert.equal(status.isolated, isolated);
      const answer = await router.scanEx({ deep: true });
      assert.equal(answer.isolated, isolated);
      assert.equal(answer.ok, false);
      assert.deepEqual(answer.messages, []);
      assert.equal(router.lastError, status.last_error);
    }
  }
  assert.equal(calls, 0);
  assert.equal(availabilityChecks, 0);
});

test("an isolated router uses its gateway and blocks fallback if the gateway fails", async (t) => {
  const { testData } = fixture(t);
  family.configure({ hub: TEST_HUB, dataDir: testData });
  let fail = false;
  let calls = 0;
  const router = family.famMailRouter({
    hub: {
      mailAvailable: async () => true,
      mailMessages: async () => fail ? { ok: false, error: "gateway failed" } : { ok: true, messages: [{ id: 5 }], last_id: 5 },
    },
    runHelper: async () => { calls++; return { ok: true, messages: [{ id: 8 }] }; },
    helperAvailable: () => true,
    deepUsesHelper: true,
  });
  const success = await router.scanEx({ deep: true });
  assert.equal(success.source, "hub");
  assert.deepEqual(success.messages.map((row) => row.hub_id), [5]);
  fail = true;
  const answer = await router.scanEx({});
  assert.equal(answer.isolated, "hub");
  assert.equal(answer.fallback_from, "gateway failed");
  assert.deepEqual(answer.messages, []);
  assert.equal(calls, 0);
});

test("declared origin and descendant data permit the synthetic helper", async (t) => {
  const { root, nested, testData } = fixture(t);
  const fakeFaustus = path.join(root, "helper");
  const python = path.join(fakeFaustus, family.PYTHON_CANDIDATES[0]);
  const marker = path.join(fakeFaustus, "mcp_servers", "email_server.py");
  for (const file of [python, marker]) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, "fixture");
  }
  family.configure({ hub: INSTALLED_HUB, dataDir: nested });
  assert.equal(family.mailIsolation(), null);
  let spawns = 0;
  const run = family.spawnHelperRunner({
    helperPath: "stub",
    setting: fakeFaustus,
    discovery: { searchParents: false, commonPaths: [] },
    spawnFn: () => {
      spawns++;
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stdin = Object.assign(new EventEmitter(), { end() {
        queueMicrotask(() => {
          child.stdout.emit("data", Buffer.from('{"ok": true, "accounts": ["synthetic"]}'));
          child.emit("close", 0);
        });
      } });
      return child;
    },
  });
  assert.deepEqual(await run("status"), { ok: true, accounts: ["synthetic"] });
  family.configure({ hub: INSTALLED_HUB, dataDir: testData, tokenFile: path.join(nested, "mcp-token") });
  assert.equal((await run("status")).isolated, "data");
  family.configure({ hub: INSTALLED_HUB, dataDir: nested, tokenFile: path.join(testData, "mcp-token") });
  assert.equal((await run("status")).isolated, "data");
  for (const hub of ["http://mail-install.example.test", INSTALLED_HUB + ":444", "invalid"]) {
    family.configure({ hub, dataDir: nested });
    assert.equal((await run("status")).isolated, "hub");
  }
  assert.equal(spawns, 1);
});

test("default loopback and exact installation data are accepted without declarations", async (t) => {
  const { root } = fixture(t);
  delete process.env.HOARD_MAIL_REAL_HUB_URLS;
  delete process.env.HOARD_MAIL_REAL_DATA_DIRS;
  const sourceDir = path.join(root, "js");
  const dataDir = path.join(root, "data");
  fs.mkdirSync(sourceDir);
  fs.mkdirSync(path.join(dataDir, "nested"), { recursive: true });
  const modulePath = path.join(sourceDir, "hoard-link.mjs");
  fs.copyFileSync(new URL("../../js/hoard-link.js", import.meta.url), modulePath);
  const installed = await import(pathToFileURL(modulePath).href);
  for (const host of ["127.0.0.1", "localhost", "[::1]"]) {
    installed.configure({ hub: `http://${host}:8810`, dataDir });
    assert.equal(installed.mailIsolation(), null);
  }
  installed.configure({ hub: "http://127.0.0.1:8811", dataDir });
  assert.equal(installed.mailIsolation().isolated, "hub");
  installed.configure({ hub: "http://127.0.0.1:8810", dataDir: path.join(dataDir, "nested") });
  assert.equal(installed.mailIsolation().isolated, "data");
  installed.configure({ hub: "http://127.0.0.1:8810" });
  assert.equal(installed.mailIsolation().isolated, "data");
});
