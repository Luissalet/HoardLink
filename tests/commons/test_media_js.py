"""js/hoard-commons/media.js beyond the shared vectors: tool resolution with fake `node:` specs, processes, the queue, progress, updates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hoard_link import proc
from tests.commons.jsrun import JS_DIR, node

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="shell-script fakes")


def js(tmp_path: Path, body: str, *, timeout: float = 90.0):
    """Run an async JS snippet that has ``m`` (the media.js module) and must ``return`` a JSON value; returns it parsed."""
    mod = (JS_DIR / "media.js").resolve().as_uri()
    script = tmp_path / "snippet.mjs"
    script.write_text(
        f"import * as m from {json.dumps(mod)};\nimport fs from 'node:fs'; import path from 'node:path'; import os from 'node:os';\n"
        f"const result = await (async () => {{\n{textwrap.dedent(body)}\n}})();\nprocess.stdout.write(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )
    done = subprocess.run([node(), str(script)], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout or "null")


@pytest.fixture
def sandbox(tmp_path):
    """An environment with nothing installed: PATH points at an empty folder, HOARD_HOME is empty."""
    (tmp_path / "empty").mkdir()
    (tmp_path / "home").mkdir()
    return {"PATH": str(tmp_path / "empty"), "HOARD_HOME": str(tmp_path / "home")}


def fake_tool(tmp_path: Path, name: str, version: str = "2026.01.01", *, code: int = 0) -> Path:
    """A fake program written in JS: ``--version`` prints the version, ``-U`` bumps it (state kept next to the script)."""
    script = tmp_path / f"{name}.js"
    state = tmp_path / f"{name}.version"
    state.write_text(version)
    script.write_text(textwrap.dedent(f"""
        const fs = require('fs');
        const args = process.argv.slice(2);
        if (args[0] === '--version' || args[0] === '-version') {{ console.log(fs.readFileSync({json.dumps(str(state))}, 'utf8')); process.exit({code}); }}
        if (args[0] === '-U') {{ fs.writeFileSync({json.dumps(str(state))}, '2026.03.01'); console.log('Updated to 2026.03.01'); process.exit(0); }}
        process.exit(0);
    """).replace("const fs = require('fs');", "const fs = require('node:fs');"), encoding="utf-8")
    script.rename(script.with_suffix(".cjs"))
    return script.with_suffix(".cjs")


# -- command specs / small helpers ---------------------------------------------------------------------------

def test_parse_command_spec(tmp_path):
    out = js(tmp_path, """
        return [m.parseCommandSpec(''), m.parseCommandSpec('node:/x/fake.js'), m.parseCommandSpec('/x/fake.mjs'),
                m.parseCommandSpec('"python -m yt_dlp"'), m.parseCommandSpec('/usr/bin/yt-dlp'), m.parseCommandSpec(null)];
    """)
    assert out[0] is None and out[5] is None
    assert out[1]["args"] == ["/x/fake.js"] and out[2]["args"] == ["/x/fake.mjs"] and out[1]["cmd"] == out[2]["cmd"]
    assert out[3] == {"cmd": "python", "args": ["-m", "yt_dlp"]}
    assert out[4] == {"cmd": "/usr/bin/yt-dlp", "args": []}


@posix_only
def test_which_skips_non_executables(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "tool").write_text("x")
    (a / "tool").chmod(0o644)
    (b / "tool").write_text("#!/bin/sh\n")
    (b / "tool").chmod(0o755)
    out = js(tmp_path, f"return m.which('tool', {{ pathDirs: [{json.dumps(str(a))}, {json.dumps(str(b))}] }});")
    assert out == str(b / "tool")
    assert js(tmp_path, "return m.which('tool', { pathDirs: [] });") is None


def test_hoard_home_and_bin_dir(tmp_path):
    out = js(tmp_path, """
        return [m.hoardHome({ HOARD_HOME: '/data/hoard' }), m.binDir({ HOARD_HOME: '/data/hoard' }), m.hoardHome({ HOARD_HOME: '~/h' }) === path.join(os.homedir(), 'h'),
                m.hoardHome({}) === path.join(os.homedir(), '.hoard')];
    """)
    assert out[0].replace("\\", "/").endswith("/data/hoard") and out[1].replace("\\", "/").endswith("/data/hoard/bin") and out[2] and out[3]


def test_install_hints(tmp_path):
    out = js(tmp_path, "return [m.installHint('ffmpeg', 'win32'), m.installHint('ffmpeg', 'darwin'), m.installHint('ytdlp'), m.installHint('node', 'linux')];")
    assert "winget install Gyan.FFmpeg" in out[0] and "brew install ffmpeg" in out[1] and "HOARD_YTDLP" in out[2] and "Node.js" in out[3]


def test_codecs_and_eta_helpers(tmp_path):
    out = js(tmp_path, """
        const log = `Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv), 1920x1080\\n  Stream #0:1: Audio: opus, 48000 Hz, stereo`;
        const ok = `Stream #0:0[0x1](und): Video: h264 (High), yuv420p(progressive), 1280x720\\n Stream #0:1: Audio: aac (LC), 44100 Hz`;
        const cover = `Stream #0:0: Audio: mp3, 44100 Hz\\n Stream #0:1: Video: mjpeg (Baseline), yuvj420p, 300x300 (attached pic)`;
        return [m.parseCodecs(log), m.needsTranscode(m.parseCodecs(log)), m.needsTranscode(m.parseCodecs(ok)), m.needsTranscode(m.parseCodecs(cover)),
                m.etaSeconds('01:05'), m.etaSeconds('1:01:05'), m.etaSeconds('x'), m.etaSeconds(null), m.vttText('WEBVTT\\n\\n00:00:00.000 --> 00:00:01.000\\nhi\\n')];
    """)
    assert out[0]["video"]["codec"] == "hevc" and out[0]["audio"] == "opus"
    assert out[1] is True and out[2] is False and out[3] is False
    assert out[4:8] == [65, 3665, None, None] and out[8] == "hi"


def test_messages_can_be_spanish(tmp_path):
    out = js(tmp_path, """
        const en = m.explainFailure('yt-dlp', 'ERROR: Unsupported URL: https://x.test/').message;
        const es = m.explainFailure('yt-dlp', 'ERROR: Unsupported URL: https://x.test/', { lang: 'es' }).message;
        m.setLanguage('es');
        const sticky = m.explainFailure('yt-dlp', 'boom').message;
        m.setLanguage('en');
        return [en, es, sticky, m.getLanguage()];
    """)
    assert out[0] != out[1] and "no es compatible" in out[1].lower() or "no admitida" in out[1].lower()
    assert "falló" in out[2] and out[3] == "en"


def test_explain_failure_flags_and_generic(tmp_path):
    out = js(tmp_path, """
        const pick = (e) => ({ kind: e.kind, authLike: !!e.authLike, outdatedLike: !!e.outdatedLike, noVideo: !!e.noVideo, fatal: !!e.fatal, status: e.status, code: e.code, detail: e.detail });
        return [
          pick(m.explainFailure('yt-dlp', 'ERROR: [twitter] 1: No video could be found in this tweet')),
          pick(m.explainFailure('yt-dlp', 'ffprobe and ffmpeg not found')),
          pick(m.explainFailure('yt-dlp', 'yt-dlp: error: no such option: --js-runtimes')),
          pick(m.explainFailure('yt-dlp', 'weird failure\\nERROR: the real reason')),
          m.explainFailure('yt-dlp', '', { code: 7 }).message,
        ];
    """)
    assert out[0]["noVideo"] and out[0]["kind"] == "no_video"
    assert out[1]["fatal"] and out[1]["code"] == "NO_FFMPEG"
    assert out[2]["outdatedLike"] and out[2]["kind"] == "outdated"
    assert out[3]["kind"] == "unknown" and out[3]["detail"] == "the real reason"
    assert "code 7" in out[4]


# -- tool resolution ------------------------------------------------------------------------------------------

def test_resolve_tool_with_a_fake_node_spec(tmp_path, sandbox):
    fake = fake_tool(tmp_path, "fake_ytdlp", "2026.02.02")
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, HOARD_YTDLP: 'node:{fake.as_posix()}' }};
        const t = await m.resolveTool('ytdlp', {{ env }});
        return {{ found: t.found, how: t.how, source: t.source, version: t.version, args: t.command.args, isNode: t.command.cmd === process.execPath, tried: t.tried }};
    """)
    assert out["found"] and out["how"] == "env" and out["source"] == "HOARD_YTDLP" and out["version"] == "2026.02.02"
    assert out["isNode"] and out["args"] == [fake.as_posix()] and out["tried"] == []


def test_resolve_tool_order_and_verification(tmp_path, sandbox):
    broken = fake_tool(tmp_path, "broken", "1.0", code=3)
    good = fake_tool(tmp_path, "good", "2026.05.05")
    out = js(tmp_path, f"""
        const base = {json.dumps(sandbox)};
        const both = await m.resolveTool('ytdlp', {{ env: {{ ...base, HOARD_YTDLP: 'node:{broken.as_posix()}', LINKS_YTDLP: 'node:{good.as_posix()}' }}, refresh: true }});
        const hoardWins = await m.resolveTool('ytdlp', {{ env: {{ ...base, HOARD_YTDLP: 'node:{good.as_posix()}', LINKS_YTDLP: 'node:{broken.as_posix()}' }}, refresh: true }});
        const custom = await m.resolveTool('ytdlp', {{ env: {{ ...base, MYAPP_YTDLP: 'node:{good.as_posix()}' }}, legacyEnv: ['MYAPP_YTDLP'], refresh: true }});
        return [both, hoardWins, custom].map((t) => ({{ found: t.found, source: t.source, version: t.version, tried: t.tried.map((x) => x.how) }}));
    """)
    assert out[0] == {"found": True, "source": "LINKS_YTDLP", "version": "2026.05.05", "tried": ["env"]}  # HOARD_YTDLP failed its version check
    assert out[1]["source"] == "HOARD_YTDLP" and out[1]["tried"] == []
    assert out[2]["source"] == "MYAPP_YTDLP"


@posix_only
def test_resolve_tool_hoard_bin_then_path(tmp_path, sandbox):
    home_bin = tmp_path / "home" / "bin"
    home_bin.mkdir()
    exe = home_bin / "yt-dlp"
    exe.write_text("#!/bin/sh\necho 2026.07.07\n")
    exe.chmod(0o755)
    other = tmp_path / "pathdir"
    other.mkdir()
    (other / "yt-dlp").write_text("#!/bin/sh\necho 1999.01.01\n")
    (other / "yt-dlp").chmod(0o755)
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, PATH: {json.dumps(str(other))} }};
        const t = await m.resolveTool('ytdlp', {{ env, refresh: true }});
        fs.rmSync({json.dumps(str(exe))});
        const u = await m.resolveTool('ytdlp', {{ env, refresh: true }});
        return [t, u].map((x) => ({{ how: x.how, version: x.version }}));
    """)
    assert out == [{"how": "hoard-bin", "version": "2026.07.07"}, {"how": "path", "version": "1999.01.01"}]


def test_resolve_tool_missing_has_a_hint_and_never_throws(tmp_path, sandbox):
    out = js(tmp_path, f"""
        const t = await m.resolveTool('ytdlp', {{ env: {json.dumps(sandbox)}, refresh: true }});
        const bad = await m.resolveTool('nope', {{ env: {json.dumps(sandbox)} }}).catch((e) => e.message);
        return {{ found: t.found, hint: t.hint, command: t.command, bad }};
    """)
    assert out["found"] is False and "HOARD_YTDLP" in out["hint"] and out["command"] is None and "nope" in out["bad"]


def test_resolve_tool_cache(tmp_path, sandbox):
    fake = fake_tool(tmp_path, "c", "2026.01.01")
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, HOARD_YTDLP: 'node:{fake.as_posix()}' }};
        const a = await m.resolveTool('ytdlp', {{ env }});
        const b = await m.resolveTool('ytdlp', {{ env }});
        const c = await m.resolveTool('ytdlp', {{ env, refresh: true }});
        const d = await m.resolveTool('ytdlp', {{ env: {{ ...env, HOARD_YTDLP: '' }} }});
        m.resetToolsCache();
        const e = await m.resolveTool('ytdlp', {{ env }});
        return [a === b, a === c, a === e, d.found];
    """)
    assert out == [True, False, False, False]  # cached; refresh skips it; resetToolsCache clears it; a changed environment is a new key


def test_ffprobe_is_found_next_to_ffmpeg(tmp_path, sandbox):
    if sys.platform.startswith("win"):
        pytest.skip("shell fakes")
    d = tmp_path / "sdk"
    d.mkdir()
    for n in ("ffmpeg", "ffprobe"):
        (d / n).write_text(f"#!/bin/sh\necho '{n} version 7.0'\n")
        (d / n).chmod(0o755)
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, HOARD_FFMPEG: {json.dumps(str(d / 'ffmpeg'))} }};
        const t = await m.resolveTool('ffprobe', {{ env, refresh: true }});
        return [t.found, t.how, t.path, t.version];
    """)
    assert out[0] and out[1] == "sibling" and out[2] == str(d / "ffprobe") and out[3] == "7.0"


def test_tools_status_shape(tmp_path, sandbox):
    fake = fake_tool(tmp_path, "s", "2026.01.01")
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, HOARD_YTDLP: 'node:{fake.as_posix()}' }};
        return await m.toolsStatus({{ env, tools: ['ytdlp', 'gallerydl'], refresh: true }});
    """)
    assert out["ytdlp"]["found"] and out["ytdlp"]["version"] == "2026.01.01" and out["gallerydl"]["found"] is False and "hint" in out["gallerydl"]
    assert out["install_command"].startswith("python -m pip") and out["bin_dir"].endswith("bin") and out["platform"]


# -- updates --------------------------------------------------------------------------------------------------

def test_update_tools_self_update(tmp_path, sandbox):
    fake = fake_tool(tmp_path, "u", "2025.01.01")
    out = js(tmp_path, f"""
        const env = {{ ...{json.dumps(sandbox)}, HOARD_YTDLP: 'node:{fake.as_posix()}' }};
        return await m.updateTools({{ tools: ['ytdlp', 'ffmpeg'], env }});
    """)
    r = {x["tool"]: x for x in out["results"]}
    assert r["ytdlp"]["ok"] and r["ytdlp"]["updated"] and (r["ytdlp"]["before"], r["ytdlp"]["after"]) == ("2025.01.01", "2026.03.01")
    assert r["ytdlp"]["method"].startswith("self-update")
    assert r["ffmpeg"]["ok"] is False and "not updated" in r["ffmpeg"]["error"]


@posix_only
def test_download_release_checks_sha256(tmp_path, sandbox):
    payload = b"#!/bin/sh\necho 2026.08.08\n"
    digest = hashlib.sha256(payload).hexdigest()
    sums = f"{digest}  yt-dlp_linux\n{'0' * 64}  yt-dlp\n"
    out = js(tmp_path, f"""
        const payload = Buffer.from({json.dumps(payload.decode())});
        const mk = (sums) => async (url) => url.endsWith('SHA2-256SUMS')
          ? {{ ok: true, status: 200, text: async () => sums }}
          : {{ ok: true, status: 200, arrayBuffer: async () => payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.length) }};
        const env = {json.dumps(sandbox)};
        const bad = await m.downloadRelease('ytdlp', {{ env, fetchImpl: mk({json.dumps('a' * 64 + '  yt-dlp_linux')}) }});
        const existsAfterBad = fs.existsSync(path.join(m.binDir(env), 'yt-dlp'));
        const good = await m.downloadRelease('ytdlp', {{ env, fetchImpl: mk({json.dumps(sums)}) }});
        const target = path.join(m.binDir(env), 'yt-dlp');
        const t = await m.resolveTool('ytdlp', {{ env, refresh: true }});
        const offline = await m.downloadRelease('ytdlp', {{ env, fetchImpl: async () => {{ throw new Error('offline'); }} }});
        const nosha = await m.downloadRelease('ytdlp', {{ env, fetchImpl: async (url) => url.endsWith('SHA2-256SUMS') ? {{ ok: false, status: 404 }} : mk('')(url) }});
        return {{ bad, existsAfterBad, good, executable: (fs.statSync(target).mode & 0o111) !== 0, found: t.version, offline, nosha, parts: fs.readdirSync(m.binDir(env)).filter((f) => f.endsWith('.part')) }};
    """)
    assert out["bad"]["ok"] is False and "checksum mismatch" in out["bad"]["output"] and out["existsAfterBad"] is False
    assert out["good"]["ok"] and out["executable"] and out["found"] == "2026.08.08" and out["parts"] == []
    assert out["offline"]["ok"] is False and "offline" in out["offline"]["output"]
    assert out["nosha"]["ok"] is False and "checksum" in out["nosha"]["output"]


# -- processes ------------------------------------------------------------------------------------------------

def test_run_process_lines_and_exit_code(tmp_path):
    out = js(tmp_path, """
        const lines = []; const errs = [];
        const r = await m.runProcess({ cmd: process.execPath, args: [] }, ['-e', "console.log('a'); process.stdout.write('b\\\\rc\\\\n'); console.error('oops'); process.exit(3)"],
          { onStdoutLine: (l) => lines.push(l), onStderrLine: (l) => errs.push(l), lowPriority: false });
        return { code: r.code, stdout: r.stdout, lines, errs };
    """)
    assert out["code"] == 3 and out["lines"] == ["a", "b", "c"] and out["errs"] == ["oops"] and "a" in out["stdout"]


def test_run_process_missing_binary(tmp_path):
    out = js(tmp_path, """
        const e = await m.runProcess({ cmd: '/no/such/program', args: [] }, []).catch((x) => x);
        const c = await m.runCapture({ cmd: '/no/such/program', args: [] }, []);
        return { code: e.code, name: e.name, captured: c.code, hasError: !!c.error };
    """)
    assert out == {"code": "BINARY_MISSING", "name": "Error", "captured": None, "hasError": True}


def test_run_process_abort_kills_the_tree_and_waits(tmp_path):
    marker = tmp_path / "grandchild.pid"
    out = js(tmp_path, f"""
        const code = `
          const {{ spawn }} = require('node:child_process');
          const c = spawn(process.execPath, ['-e', 'setTimeout(()=>{{}}, 60000)'], {{ stdio: 'ignore' }});
          require('node:fs').writeFileSync({json.dumps(str(marker))}, String(c.pid));
          setTimeout(() => {{}}, 60000);`;
        const controller = new AbortController();
        setTimeout(() => controller.abort(), 700);
        const t0 = Date.now();
        const e = await m.runProcess({{ cmd: process.execPath, args: [] }}, ['-e', code], {{ signal: controller.signal, lowPriority: false }}).catch((x) => x);
        const gpid = Number(fs.readFileSync({json.dumps(str(marker))}, 'utf8'));
        const pre = await m.runProcess({{ cmd: process.execPath, args: [] }}, ['-e', '1'], {{ signal: AbortSignal.abort() }}).catch((x) => x);
        return {{ code: e.code, status: e.status, ms: Date.now() - t0, gpid, pre: pre.code }};
    """)
    assert out["code"] == "CANCELLED" and out["status"] == 409 and out["pre"] == "CANCELLED" and out["ms"] < 15000
    deadline = time.monotonic() + 5
    while proc.pid_alive(out["gpid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not proc.pid_alive(out["gpid"])  # the grandchild died with its parent


def test_run_process_timeout_kills(tmp_path):
    out = js(tmp_path, """
        const t0 = Date.now();
        const r = await m.runProcess({ cmd: process.execPath, args: [] }, ['-e', 'setTimeout(()=>{}, 60000)'], { timeoutMs: 600, lowPriority: false });
        return { ms: Date.now() - t0, code: r.code };
    """)
    assert out["ms"] < 15000 and out["code"] != 0


def test_kill_tree(tmp_path):
    out = js(tmp_path, """
        const { spawn } = await import('node:child_process');
        const c = spawn(process.execPath, ['-e', 'setTimeout(()=>{}, 60000)'], { detached: process.platform !== 'win32', stdio: 'ignore' });
        const gone = await m.killTree(c.pid);
        const again = await m.killTree(c.pid);
        const none = await m.killTree(0);
        return { gone, again, none };
    """)
    assert out == {"gone": True, "again": True, "none": True}


# -- queue ----------------------------------------------------------------------------------------------------

def test_media_queue_is_fifo_with_concurrency_one(tmp_path):
    out = js(tmp_path, """
        const q = new m.MediaQueue(); const log = []; let running = 0; let maxRunning = 0;
        const job = (id, ms) => async () => { running++; maxRunning = Math.max(maxRunning, running); log.push('start ' + id); await new Promise((r) => setTimeout(r, ms)); log.push('end ' + id); running--; };
        const added = [q.enqueue('a', job('a', 40)), q.enqueue('b', job('b', 10)), q.enqueue('c', job('c', 10)), q.enqueue('a', job('a2', 10))];
        const meta = [q.size, q.activeId, q.pendingIds, q.has('b'), q.has('zzz')];
        await q.idle();
        return { added, meta, log, maxRunning, size: q.size };
    """)
    assert out["added"] == [True, True, True, False] and out["meta"] == [3, "a", ["b", "c"], True, False]
    assert out["log"] == ["start a", "end a", "start b", "end b", "start c", "end c"] and out["maxRunning"] == 1 and out["size"] == 0


def test_media_queue_cancel_pending_and_active(tmp_path):
    out = js(tmp_path, """
        const q = new m.MediaQueue(); const log = [];
        q.enqueue('run', (signal) => new Promise((resolve) => {
          log.push('run started');
          signal.addEventListener('abort', () => setTimeout(() => { log.push('run settled'); resolve(); }, 60));
        }));
        q.enqueue('wait', async () => { log.push('wait ran'); });
        q.enqueue('later', async () => { log.push('later ran'); });
        await new Promise((r) => setTimeout(r, 20));
        const results = [q.cancel('wait'), q.cancel('run'), q.cancel('nobody')];
        const stillActive = q.activeId;           // the entry keeps the lane until its runner settles
        await q.idle();
        return { results, stillActive, log };
    """)
    assert out["results"] == ["pending", "active", False] and out["stillActive"] == "run"
    assert out["log"] == ["run started", "run settled", "later ran"]  # 'wait' never ran; 'later' started only after 'run' settled


def test_media_queue_cancel_all_and_failures_do_not_block(tmp_path):
    out = js(tmp_path, """
        const q = new m.MediaQueue(); const log = [];
        q.enqueue('boom', async () => { throw new Error('x'); });
        q.enqueue('next', async () => { log.push('next ran'); });
        await q.idle();
        const q2 = new m.MediaQueue();
        q2.enqueue('active', (signal) => new Promise((r) => signal.addEventListener('abort', () => { log.push('aborted'); r(); })));
        q2.enqueue('w1', async () => {}); q2.enqueue('w2', async () => {});
        await new Promise((r) => setTimeout(r, 20));   // let the runner start and attach its listener
        const removed = q2.cancelAll();
        await q2.idle(2000);
        return { log, removed, size: q2.size };
    """)
    assert out["log"] == ["next ran", "aborted"] and out["removed"] == ["w1", "w2"] and out["size"] == 0


# -- progress -------------------------------------------------------------------------------------------------

def test_progress_tracker_merged_streams_and_playlists(tmp_path):
    out = js(tmp_path, """
        const t = new m.ProgressTracker();
        t.select({ formatId: '137+140', index: 1, count: 1 });
        const bar = [];
        for (const ev of [
          { type: 'progress', downloaded: 50, total: 100, speed: 2048, eta: 5, status: 'downloading' },
          { type: 'progress', downloaded: 100, total: 100, status: 'finished' },
          { type: 'progress', downloaded: 25, total: 50, speed: 1024, eta: 3, status: 'downloading' },
          { type: 'progress', downloaded: 50, total: 50, status: 'finished' },
        ]) bar.push(t.update(ev).progress);
        const pp = t.update({ type: 'pp', name: 'Merger', status: 'started' });
        const none = t.update({ type: 'sel' });
        const list = new m.ProgressTracker();
        list.select({ formatId: '18', index: 2, count: 4 });
        const second = list.update({ type: 'progress', downloaded: 50, total: 100, speed: 1536, eta: 61, status: 'downloading' });
        return { bar, pp, none, second, label: list.label };
    """)
    assert out["bar"] == sorted(out["bar"]) and out["bar"][0] == 25.0 and out["bar"][1] == 50.0 and out["bar"][-1] == 99
    assert out["pp"]["status"] == "processing" and "Merging" in out["pp"]["detail"] and out["none"] is None
    assert out["second"]["progress"] == 37.5 and out["second"]["speed"] == "1.5 KB/s" and out["second"]["eta"] == "01:01" and out["label"] == "Item 2 of 4…"
