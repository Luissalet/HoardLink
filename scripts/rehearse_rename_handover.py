"""Run the unchanged migration commands with a real isolated Hub on Windows."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from rename_handover import (
    CONFIG_FILES, LIVE_PORTS, LOOPBACK_ALLOCATOR, NEW_ID, OLD_ID, OTHER_ID, STEPS,
    Migration, cmd, read_json, save_json, seed_voice_fixture_venv, sha256, wait_for,
)
from rename_handover_windows import protect


VIT_PROVENANCE = "vitruvius_hoard/showcase/assets/provenance.json"
VIT_GENERATED = "theseus_tests/fixtures/managed_oracle/obj/"
VIT_LOCAL_FILES = {
    VIT_PROVENANCE: b'{"fixture": "local provenance"}\n',
    VIT_GENERATED + "fixture.txt": b"local generated file\n",
}

SERVER = r'''
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--no-browser", action="store_true", required=True)
parser.add_argument("--receipt-tag", required=True)
args = parser.parse_args()
data_dir = Path(os.environ["REHEARSAL_DATA_DIR"])
port = int(os.environ["REHEARSAL_PORT"])
if port in {5191, 5220, 5232, 5253, 8810, 8815, 59018}:
    raise ValueError("The allocated fixture port is reserved")
app = __package__.removesuffix("_hoard")
voice = app != "vitruvius"
service = "cyrano-hoard" if voice else "vitruvius-hoard"
websockets_version = None
if voice and (expected_websockets := os.environ.get("REHEARSAL_WEBSOCKETS")):
    import websockets
    websockets_version = websockets.__version__
    if (websockets_version != expected_websockets
            or not Path(websockets.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())):
        raise ValueError("Fixture voice did not import the expected local WebSocket runtime")
data_dir.mkdir(parents=True, exist_ok=True)
if voice and (os.environ.get("CYRANO_TTS_ENGINE") != "off" or
              os.environ.get("BOLTAGON_TTS_ENGINE") != "off" or
              os.environ.get("CUDA_VISIBLE_DEVICES") != "-1"):
    raise ValueError("Fixture voice must remain off with CUDA masked")
token = secrets.token_hex(32)
Path(os.environ["REHEARSAL_TOKEN_FILE"]).write_text(token, encoding="ascii")
preview = False

def launch_receipt():
    keys = ("CYRANO_TTS_", "BOLTAGON_TTS_", "BOLTAGON_DATA_DIR", "BOLTAGON_TOKEN_FILE",
            "VITRUVIUS_PROFILE_DIR", "VITRUVIUS_VOICE_CACHE", "PORT_STRICT",
            "HOARD_FAUSTUS_URL", "HOARD_LLM_URL", "HOARD_COMFY_URL", "CUDA_VISIBLE_DEVICES",
            "REHEARSAL_WEBSOCKETS")
    return {"executable": sys.executable, "argv": sys.orig_argv[1:], "cwd": os.getcwd(),
            "env": {key: value for key, value in os.environ.items() if key.startswith(keys)},
            "child": None, "websockets": websockets_version,
            "cache": (Path(os.environ["VITRUVIUS_VOICE_CACHE"]) / "frases.json").read_text() if not voice else None}

class Handler(BaseHTTPRequestHandler):
    def send_json(self, body, status=200):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/health":
            return self.send_json({"ok": True, "id": app, "service": service, "pid": os.getpid(),
                                   "effective_launch": launch_receipt()})
        if self.path == "/test/preview/":
            return self.send_json({"preview": preview}, 200 if preview else 503)
        self.send_json({"ok": False}, 404)

    def do_POST(self):
        global preview
        if self.path != "/api/agent/call":
            return self.send_json({"ok": False}, 404)
        if self.headers.get("Authorization") != "Bearer " + token:
            return self.send_json({"ok": False}, 401)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name, values = request["tool"], request["arguments"]
        if name == "voice_status" and voice:
            return self.send_json({"state": "disabled", "engine_id": "off", "loaded": False, "child": None})
        if name == "speech_transcribe" and voice and Path(values["path"]).is_file() and not values["fallback"]:
            return self.send_json({"text": "veinte euros"})
        if name == "dictation_understand" and voice and values.get("text") == "veinte euros":
            return self.send_json({"fields": {"amount_cents": {"value": 2000}}})
        if name == "app_project_preview" and not voice and values.get("operation") == "start":
            preview = True
            return self.send_json({"state": "running", "open_path": "/test/preview/"})
        self.send_json({"ok": False, "error": "Unexpected test call"}, 400)

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def git(path, *args):
    return cmd(["git", "-C", str(path), *args])


def commit(path, *args, source=None):
    return cmd(["git", "-C", str(path), "-c", "user.name=Luissalet",
                "-c", "user.email=luissalet@users.noreply.github.com", "-c", "commit.gpgsign=false",
                *args], source=source)


def fixture_commit(seed, app):
    module = app + "_hoard"
    path = seed / module
    path.mkdir()
    (path / "__main__.py").write_text(SERVER, encoding="utf-8")
    service = "vitruvius-hoard" if app == OTHER_ID else "cyrano-hoard"
    save_json(seed / "rehearsal-manifest.json", {
        "schema": 1, "id": app, "name": app,
        "app": {
            "url_default": "http://127.0.0.1:9",
            "health": {"path": "/api/health", "expect": {"service": service}},
            "launch_hint": {"kind": "process", "executable": "{APP_DIR}/.venv/Scripts/python.exe",
                            "argv": ["-m", module, "--no-browser"], "cwd": "{APP_DIR}"},
        },
        "x-family": {"rename_rehearsal": True, "isolated_instance": {
            "data_env": "REHEARSAL_DATA_DIR", "port_env": "REHEARSAL_PORT",
            "token_env": "REHEARSAL_TOKEN_FILE",
        }},
    })


def seed_app(hub, key, current, candidate, python, *, wheel=None):
    path = hub.parent / "apps" / f"{hub.name.removesuffix('-hub')}-{key}"
    tree = cmd(["git", "-C", str(hub), "mktree"], source="")
    root_commit = commit(hub, "commit-tree", tree, "-m", "Seed isolated test tree")
    branch = f"rehearsal/{uuid.uuid4().hex}/{key}"
    git(hub, "worktree", "add", "-b", branch, str(path), root_commit)
    (path / ".gitignore").write_text(".venv/\ndata/\n__pycache__/\n/faustus-plugin.json\n", encoding="ascii")
    fixture_commit(path, current)
    git(path, "add", "--", ".gitignore", "rehearsal-manifest.json", f"{current}_hoard/__main__.py")
    if current == OTHER_ID:
        provenance = path / VIT_PROVENANCE
        provenance.parent.mkdir(parents=True)
        provenance.write_bytes(b'{"fixture": "tracked provenance"}\n')
        # A tracked sibling keeps porcelain from collapsing the whole parent tree.
        source = Path(VIT_GENERATED).parent / "fixture-source.txt"
        (path / source).parent.mkdir(parents=True)
        (path / source).write_bytes(b"tracked fixture source\n")
        git(path, "add", "--", VIT_PROVENANCE, source.as_posix())
    commit(path, "commit", "-m", "Create fixture")
    before = git(path, "rev-parse", "HEAD")
    if current != candidate:
        git(path, "rm", "-r", current + "_hoard")
        fixture_commit(path, candidate)
        git(path, "add", "--", "rehearsal-manifest.json", f"{candidate}_hoard/__main__.py")
    else:
        (path / "fixture-revision").write_text("candidate\n", encoding="ascii")
        git(path, "add", "--", "fixture-revision")
    commit(path, "commit", "-m", "Create fixture candidate")
    after = git(path, "rev-parse", "HEAD")
    git(path, "reset", "--keep", before)
    seed_voice_fixture_venv(python, path / ".venv", wheel)
    if current == OTHER_ID:
        (path / ".venv" / "Lib" / "site-packages" / "rehearsal-app.pth").write_text(str(path) + "\n")
        for name, content in VIT_LOCAL_FILES.items():
            local_file = path / name
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_bytes(content)
    (path / "data").mkdir()
    (path / "data" / "preserved").write_text(key, encoding="ascii")
    (path / ".venv" / "original").write_text(key, encoding="ascii")
    return {"path": str(path), "before": before, "after": after}


def registry(plan):
    data = Path(plan["hub_data"])
    old = Path(plan["repos"]["voice"]["path"])
    vit = Path(plan["repos"]["vit"]["path"])
    port = plan["port"]
    voice_data = old / "data"
    child = voice_data / "voice-env"
    cmd([plan["python"], "-m", "venv", "--without-pip", str(child)])
    (voice_data / "sample.wav").write_bytes(b"fixture sample")
    (voice_data / "model").mkdir()
    (voice_data / "voice-cache").mkdir()
    (voice_data / "voice-cache" / "frases.json").write_text("[]", encoding="ascii")

    def launch(path, app, env):
        argv = ["-I"] if app == OTHER_ID else []
        return {"executable": str(path / ".venv" / "Scripts" / "python.exe"), "kind": "app",
                "argv": [*argv, "-m", app + "_hoard", "--no-browser", "--receipt-tag", f"{app}-override"],
                "cwd": str(path),
                "readiness_url": "http://127.0.0.1:9/api/health", "env": env}

    save_json(data / "hub.json", {"port": port, "roots": [str(old.parent)],
                                  "launch_overrides": {
        OLD_ID: launch(old, OLD_ID, {
            "CYRANO_TTS_ENGINE": "off", "CYRANO_TTS_PREWARM": "0", "CYRANO_TTS_QWEN_GPU": "1",
            "CYRANO_TTS_PYTHON": str(child / "Scripts" / "python.exe"),
            "CYRANO_TTS_QWEN_MODEL_DIR": str(voice_data / "model"),
            "CYRANO_TTS_QWEN_REFERENCE_WAV": str(voice_data / "sample.wav"),
            **({"REHEARSAL_WEBSOCKETS": plan["websockets_version"]} if "websockets_version" in plan else {}),
        }),
        OTHER_ID: launch(vit, OTHER_ID, {
            "VITRUVIUS_PROFILE_DIR": str(vit / "data"),
            "VITRUVIUS_VOICE_CACHE": str(voice_data / "voice-cache"),
        }),
    }})
    save_json(data / "manual-stops.json", {"stopped": ["example", OLD_ID]})
    save_json(data / "app-ports.json", {OLD_ID: 39455, OTHER_ID: 39456})
    save_json(data / "rules.json", {"example": {"app": OLD_ID, "asset": str(voice_data / "model")}})
    return {name: (data / name).read_bytes() for name in CONFIG_FILES if (data / name).exists()}


def listener_port():
    allocate = runpy.run_path(str(LOOPBACK_ALLOCATOR))["free_loopback_ports"]
    port, = allocate(1, exclude=LIVE_PORTS)
    return port


def check_local_files(run):
    status = run.git_status(run.vit)
    assert status == [f" M {VIT_PROVENANCE}", f"?? {VIT_GENERATED}"], status
    hashes = {}
    for name, expected in VIT_LOCAL_FILES.items():
        local_file = run.vit.path / name
        assert local_file.read_bytes() == expected, name
        hashes[name] = sha256(local_file)
    return {"status": status, "sha256": hashes}


def check_original(run, original):
    assert not run.new.exists()
    assert (run.voice.path / "data" / "preserved").read_text() == "voice"
    assert (run.voice.path / ".venv" / "original").read_text() == "voice"
    for name, repo in run.repos.items():
        assert repo.git("rev-parse", "HEAD") == repo.before, name
    for name in CONFIG_FILES:
        path = run.data / name
        assert (path.read_bytes() if path.is_file() else None) == original.get(name), name
    return check_processes(run, OLD_ID)


def check_processes(run, voice_id):
    overrides = read_json(run.data / "hub.json")["launch_overrides"]
    result = {}
    for app in (voice_id, OTHER_ID):
        origin = run.app_origin(app)
        health = wait_for(lambda: run.http(origin + "/api/health"))
        launch = health["effective_launch"]
        expected = overrides[app]
        assert health["id"] == app
        assert launch["argv"] == expected["argv"]
        assert Path(launch["cwd"]).resolve() == Path(expected["cwd"]).resolve()
        assert Path(launch["executable"]).resolve() == Path(expected["executable"]).resolve()
        assert all(launch["env"].get(key) == value for key, value in expected["env"].items())
        for key in ("HOARD_FAUSTUS_URL", "HOARD_LLM_URL", "HOARD_COMFY_URL"):
            assert launch["env"][key] == "http://127.0.0.1:9"
        if app == voice_id:
            assert launch["child"] is None
            assert launch["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
            assert Path(expected["env"]["CYRANO_TTS_PYTHON"]).is_file()
            assert run.app_call(app, "voice_status", {}) == {
                "state": "disabled", "engine_id": "off", "loaded": False, "child": None,
            }
        else:
            assert launch["cache"] == "[]"
        result[app] = {"pid": health["pid"], "origin": origin, **launch}
    return result


def check_candidate(run):
    assert run.new.is_dir() and not run.voice.path.exists()
    assert (run.new / "data" / "preserved").read_text() == "voice"
    assert (run.new / "data" / "voice-cache" / "frases.json").read_text() == "[]"
    assert (run.snapshot / "voice-runtime-before.json").read_bytes() == (
        run.snapshot / "voice-runtime-after.json"
    ).read_bytes()
    registry = read_json(run.data / "hub.json")["launch_overrides"]
    assert OLD_ID not in registry
    voice = registry[NEW_ID]
    assert voice["argv"] == ["-m", "boltagon_hoard", "--no-browser", "--receipt-tag", "cyrano-override"]
    assert Path(voice["env"]["BOLTAGON_TTS_PYTHON"]).is_file()
    assert voice["env"]["BOLTAGON_TTS_PYTHON"] == voice["env"]["CYRANO_TTS_PYTHON"]
    assert Path(registry[OTHER_ID]["env"]["VITRUVIUS_VOICE_CACHE"]).parent == run.new / "data"
    assert read_json(run.data / "app-ports.json") == {NEW_ID: 39455, OTHER_ID: 39456}
    assert read_json(run.data / "rules.json")["example"]["app"] == NEW_ID
    assert {row["id"] for row in read_json(run.snapshot / "resolved-launchers.json")} == {NEW_ID, OTHER_ID}


def run_command(script, action, config, *flags, expected=0):
    completed = subprocess.run(
        [sys.executable, str(script), action, str(config), *flags],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=420,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    )
    if completed.returncode != expected:
        raise RuntimeError(f"{action} returned {completed.returncode}:\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--window-watch", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--hub-before", required=True)
    parser.add_argument("--hub-after", required=True)
    parser.add_argument("--websockets-wheel", type=Path)
    parser.add_argument("--phase", type=int, choices=range(0, len(STEPS) + 1), action="append")
    args = parser.parse_args()
    secret = b"local test launch record"
    encrypted = protect(secret)
    assert secret not in encrypted and protect(encrypted, decrypt=True) == secret
    python = sys.executable
    scripts = Path(__file__).resolve().parent
    script = scripts / "rename_handover.py"
    hub = args.worktree.resolve()
    root = hub.parent
    temporary = Path(tempfile.gettempdir()).resolve()
    if (root == temporary or not root.is_relative_to(temporary) or not (hub / ".git").is_file()
            or not hub.name.endswith("-hub")):
        parser.error("Use a registered linked Hub worktree under a dedicated temporary directory")
    if git(hub, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
            scripts.parent, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        parser.error("The rehearsal needs a registered worktree of the same Hub repository")
    if git(hub, "rev-parse", "HEAD") != args.hub_before:
        parser.error("Start the rehearsal worktree at the exact baseline")
    print(root, flush=True)
    for ref in (args.hub_before, args.hub_after):
        git(hub, "cat-file", "-t", ref)
    data = root / "hub-data"
    data.mkdir()
    (root / "apps").mkdir()
    wheel = None
    if args.websockets_wheel:
        websockets_version = version("websockets")
        source = args.websockets_wheel.resolve()
        if not source.is_file() or not source.name.startswith(f"websockets-{websockets_version}-") or source.suffix != ".whl":
            parser.error("Supply a local wheel matching websockets in the selected interpreter")
        wheel = root / source.name
        shutil.copyfile(source, wheel)
    old = seed_app(hub, "voice", OLD_ID, NEW_ID, python, wheel=wheel)
    vit = seed_app(hub, "vit", OTHER_ID, OTHER_ID, python)
    new = Path(old["path"]).with_name(f"{hub.name.removesuffix('-hub')}-boltagon")
    plan = {"root": str(root), "mode": "isolated", "port": listener_port(),
            "python": python, "launcher": str(args.launcher), "hub_data": str(data),
            "window_watch": str(args.window_watch),
            "new_voice_root": str(new), "project": "fixture", "wav": str(root / "twenty.wav"),
            "repos": {"hub": {"path": str(hub), "before": args.hub_before, "after": args.hub_after},
                      "voice": old, "vit": vit}}
    if wheel is not None:
        plan.update(fixture_websockets=str(wheel), websockets_version=websockets_version)
        save_json(root / "websockets-runtime.json", {"interpreter": python, "version": websockets_version,
                                                    "wheel": wheel.name, "sha256": sha256(wheel)})
    original = registry(plan)
    Path(plan["wav"]).write_bytes(b"fixture transcription input")
    initial = root / "boot.json"
    plan["snapshot"] = str(root / "boot")
    save_json(initial, plan)
    receipts = []
    try:
        run_command(script, "fixture-boot", initial)
        for phase in args.phase or [0, *range(1, len(STEPS) + 1)]:
            started = time.monotonic()
            plan["snapshot"] = str(root / f"phase-{phase}")
            config = root / f"phase-{phase}.json"
            save_json(config, plan)
            run = Migration(config)
            before = check_original(run, original)
            local_before = check_local_files(run)
            flags = ("--confirm", "INSTALL")
            if phase:
                flags += ("--stop-after", str(phase))
            output = run_command(script, "apply", config, *flags, expected=76 if phase else 0)
            if not phase or phase >= 7:
                check_candidate(run)
            candidate = check_processes(run, NEW_ID) if not phase or phase >= 8 else None
            local_migrated = check_local_files(run) if not phase or phase >= 5 else None
            run_command(script, "rollback", config, "--confirm", "ROLLBACK")
            restored = check_original(run, original)
            save_json(run.snapshot / "protected-local-files.json",
                      {"original": local_before, "migrated": local_migrated, "restored": check_local_files(run)})
            save_json(run.snapshot / "observed-launches.json",
                      {"original": before, "candidate": candidate, "restored": restored})
            receipt = {"phase": phase, "steps": output.splitlines(), "rollback": "passed",
                       "seconds": round(time.monotonic() - started, 2)}
            receipts.append(receipt)
            save_json(root / "receipts.json", receipts)
            print(json.dumps(receipt), flush=True)
    finally:
        run_command(script, "fixture-stop", initial)
        assert Migration(initial).listener() is None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
