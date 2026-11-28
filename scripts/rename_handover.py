"""Non-interactive, reversible application-identity migration for the Hub."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from rename_handover_voice import validate_voice_benchmark
from rename_handover_windows import package_runtime_probe, protect, read_owner, reject_redirected

STEPS = (
    "inventory", "snapshot_configs", "check_recovery", "stop_services", "advance_repositories",
    "rebuild_environment", "migrate_registry", "start_services", "accept",
)
LIVE_PORTS = {5191, 5220, 5232, 5253, 8810, 8815, 59018}
LOOPBACK_ALLOCATOR = Path(__file__).resolve().parents[1] / "hoard_link" / "hub" / "loopback_ports.py"
CONFIG_FILES = ("hub.json", "app-ports.json", "manual-stops.json", "rules.json", "jobs.json")
OLD_ID, NEW_ID, OTHER_ID = "cyrano", "boltagon", "vitruvius"
DEAD_ORIGIN = "http://127.0.0.1:9"
CLOSED_ENV = {
    "HOARD_HUB_URL": DEAD_ORIGIN, "HOARD_HUB_TOKEN_FILE": r"E:\nonexistent\hub-token",
    "HOARD_HUB_AUTOSTART": "0", "HOARD_FAUSTUS_URL": DEAD_ORIGIN, "HOARD_FAUSTUS_TOKEN": "",
    "HOARD_LLM_URL": DEAD_ORIGIN, "HOARD_COMFY_URL": DEAD_ORIGIN,
    **{f"{app}_URL": DEAD_ORIGIN for app in ("CYRANO", "BOLTAGON", "VITRUVIUS")},
    **{f"{app}_TOKEN_FILE": r"E:\nonexistent\token" for app in ("CYRANO", "BOLTAGON", "VITRUVIUS")},
    "CUDA_VISIBLE_DEVICES": "-1",
    **{f"{app}_TTS_ENGINE": "off" for app in ("CYRANO", "BOLTAGON")},
    **{f"{app}_STT_ENABLED": "0" for app in ("CYRANO", "BOLTAGON")},
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def commit_file(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value):
    commit_file(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def migration_lock(path):
    import msvcrt

    with path.open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another rename or rollback is running for this Hub") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def cmd(argv, *, cwd=None, env=None, source=None, strip_output=True):
    if Path(argv[0]).name.lower() in {"cmd", "cmd.exe"} or Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError("Use the underlying executable directly")
    completed = subprocess.run(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL if source is None else None,
        input=source, capture_output=True,
        encoding="utf-8", errors="replace", timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    )
    if completed.returncode:
        message = completed.stderr.strip()
        if completed.stdout:
            message += "\n" + completed.stdout.strip()
        raise RuntimeError(message or f"Command exited {completed.returncode}: {argv[0]}")
    return completed.stdout.strip() if strip_output else completed.stdout


def powershell(source):
    command = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    return cmd(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command])


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def envelope(response, tool):
    """The CLI prints the transport object even when it has an inner result."""
    if (not isinstance(response, dict) or response.get("ok") is not True
            or response.get("tool") != tool or not isinstance(response.get("result"), dict)):
        raise ValueError(f"Invalid reply for {tool}: {response}")
    result = response["result"]
    if result.get("ok") is False:
        raise ValueError(f"{tool} failed: {result}")
    return result


def wait_for(action, seconds=35):
    until = time.monotonic() + seconds
    error = None
    while time.monotonic() < until:
        try:
            if value := action():
                return value
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            error = exc
        time.sleep(.2)
    raise RuntimeError("Readiness timed out") from error


def seed_voice_fixture_venv(python, target, wheel=None):
    cmd([python, "-m", "venv", "--without-pip", str(target)])
    if wheel is not None:
        site = target / "Lib" / "site-packages"
        cmd([python, "-m", "pip", "--disable-pip-version-check", "install", "--no-cache-dir",
             "--no-deps", "--no-index", "--target", str(site), str(wheel)])


@dataclass(frozen=True)
class Checkout:
    path: Path
    before: str
    after: str

    def git(self, *args, path=None, strip_output=True):
        return cmd(["git", "--no-optional-locks", "-C", str(path or self.path), *args],
                   strip_output=strip_output)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Migration:
    def __init__(self, file):
        self.plan = read_json(file)
        self.digest = sha256(file)
        self.isolated = self.plan["mode"] == "isolated"
        self.root = Path(self.plan["root"]).resolve()
        self.snapshot = Path(self.plan["snapshot"]).resolve()
        self.data = Path(self.plan["hub_data"]).resolve()
        self.new = Path(self.plan["new_voice_root"]).resolve()
        self.port = self.plan["port"]
        self.url = f"http://127.0.0.1:{self.port}"
        self.token = self.data / "mcp-token"
        self.repos = {
            key: Checkout(Path(item["path"]).resolve(), item["before"], item["after"])
            for key, item in self.plan["repos"].items()
        }
        self.hub, self.voice, self.vit = (self.repos[key] for key in ("hub", "voice", "vit"))
        self.flags = dict(os.environ)
        if self.isolated:
            self.flags.update(CLOSED_ENV, FAUSTUS_DIR=str(self.root / "faustus"))
        self.flags.update(PYTHONUTF8="1", HOARD_HUB_AUTOSTART="0",
                          HOARD_HUB_URL=self.url, HOARD_HUB_TOKEN_FILE=str(self.token),
                          PYTHONPATH=str(self.hub.path))
        self.journal_file = self.snapshot / "journal.json"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.state = {}
        self._validate_paths()

    def _validate_paths(self):
        if not self.root.is_dir() or self.snapshot == self.root or not self.snapshot.is_relative_to(self.root):
            raise ValueError("The snapshot must have its own directory under the plan root")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("Invalid Hub port")
        if self.isolated:
            temporary = Path(tempfile.gettempdir()).resolve()
            if (self.root == temporary or not self.root.is_relative_to(temporary)
                    or self.port in LIVE_PORTS
                    or any(not path.is_relative_to(self.root) for path in (
                        self.data, self.new, *(r.path for r in self.repos.values())
                    )) or self.data.parent != self.root):
                raise ValueError("Isolated Hub paths and port must belong to this temporary run")
            if wheel := self.plan.get("fixture_websockets"):
                source = Path(wheel).resolve()
                if (not source.is_relative_to(self.root) or not source.is_file()
                        or not source.name.startswith("websockets-") or source.suffix != ".whl"):
                    raise ValueError("The fixture WebSocket wheel must belong to this temporary run")
        elif (self.plan["mode"] != "installed" or self.port != 8810 or self.data != self.hub.path / "data"
              or urlsplit(os.environ.get("HOARD_HUB_URL", "")).port == 9):
            raise ValueError("Installed execution needs the approved Hub root and port")
        if self.new.parent != self.voice.path.parent or self.new == self.voice.path:
            raise ValueError("Voice roots must be different folders with the same parent")
        if any(not re.fullmatch(r"[0-9a-f]{40}", ref)
               for repo in self.repos.values() for ref in (repo.before, repo.after)):
            raise ValueError("Use full, lowercase commit IDs for all repositories")
        if self.voice.path.drive.lower() != self.snapshot.drive.lower():
            raise ValueError("The backup and voice checkout must be on the same volume")
        wav = Path(self.plan["wav"]).resolve()
        if not wav.is_file() or (self.isolated and not wav.is_relative_to(self.root)):
            raise ValueError("The WAV must be a verified local test input")

    def http(self, url, *, token=False, payload=None, raw=False):
        headers = {"Authorization": "Bearer " + self.token.read_text().strip()} if token else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        with self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=12) as response:
            return response.status if raw else json.load(response)

    def configuration(self):
        cfg = self.http(self.url + "/api/config")
        if (cfg["isolated"] is not self.isolated or cfg["port"] != self.port
                or Path(cfg["data_dir"]).resolve() != self.data):
            raise RuntimeError("The listener does not belong to this migration")
        if self.isolated and any(not Path(root).resolve().is_relative_to(self.root) for root in cfg["roots"]):
            raise RuntimeError("The isolated catalogue points outside the fixture")
        return cfg

    def cli(self, *args, payload=None):
        self.configuration()
        argv = [self.plan["python"], "-B", "-P", "-m", "hoard_link.hub.cli",
                "--json", "--hub-url", self.url, "--token-file", str(self.token), *args]
        if payload is None:
            return json.loads(cmd(argv, cwd=self.hub.path, env=self.flags))
        args_file = self.root / f"cli-{uuid.uuid4().hex}.json"
        try:
            save_json(args_file, payload)
            argv.extend(("--args-file", str(args_file)))
            return json.loads(cmd(argv, cwd=self.hub.path, env=self.flags))
        finally:
            args_file.unlink(missing_ok=True)

    def tool(self, name, args=None):
        return envelope(self.cli("tool", name, payload=args), name)

    def app_call(self, app, tool, args):
        if self.isolated:
            ident = self.instances()[app]["id"]
            response = self.tool("hub_isolated_app", {"action": "call", "instance_id": ident,
                                                      "tool": tool, "arguments": args,
                                                      "reason": "Verify the installation"})
            return envelope(response, tool)
        return envelope(self.cli("call", app, tool, "--reason", "Verify installation", payload=args), tool)

    def instances(self):
        rows = self.tool("hub_isolated_app", {"action": "list"})["instances"]
        return {row["app"]: row for row in rows if row["state"] == "running"}

    def start_app(self, app, path):
        if self.isolated:
            existing = self.instances().get(app)
            if existing:
                return existing
            before = self.windows()
            instance = self.tool("hub_isolated_app", {
                "action": "start", "app": app, "worktree": str(path),
            })["instance"]
            self.record_windows(before, f"app-{app}")
            return instance
        origin = self.app_origin(app)
        if self.listener(urlsplit(origin).port) is None:
            reply = self.http(f"{self.url}/api/apps/{app}/start", token=True, payload={"wait": False})
            if reply.get("ok") is not True:
                raise ValueError(f"Start failed for {app}: {reply}")
        return wait_for(lambda: self.app_ready(app), seconds=90)

    def stop_app(self, app):
        if self.isolated:
            if row := self.instances().get(app):
                self.tool("hub_isolated_app", {"action": "stop", "instance_id": row["id"]})
        else:
            port = urlsplit(self.app_origin(app)).port
            if self.listener(port):
                reply = self.http(f"{self.url}/api/apps/{app}/stop", token=True, payload={"wait": False})
                if reply.get("ok") is not True:
                    raise ValueError(f"Stop failed for {app}: {reply}")
                wait_for(lambda: self.listener(port) is None, seconds=45)

    def app_origin(self, app):
        if self.isolated:
            origin = self.instances()[app]["url"]
        else:
            origin = self.cli("state", app)["url"]
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/?", origin)
        if not match or (self.isolated and int(match[1]) in LIVE_PORTS):
            raise RuntimeError("Unexpected application listener")
        return origin.rstrip("/")

    def app_ready(self, app):
        required = "app_project_get" if app == OTHER_ID else "voice_status"
        health = self.http(self.app_origin(app) + "/api/health")
        tools = self.cli("tools", app).get("tools", [])
        return health if any(tool["name"] == required for tool in tools) else None

    def mark(self, key):
        self.state[key] = True
        save_json(self.journal_file, self.state)

    def listener(self, port=None):
        port = self.port if port is None else port
        response = powershell(
            "$p = @(Get-NetTCPConnection -LocalPort " + str(port)
            + " -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique);"
            "if ($p.Count -gt 1) {exit 7}; if ($p.Count -eq 1) {$p[0]}"
        )
        return int(response) if response else None

    def stop_hub(self):
        pid = self.listener()
        if pid is None:
            self.cancel_pending_hub()
            return
        self.configuration()
        if not self.isolated:
            read_owner(pid, self.hub.path)
        prefix = "$p=Get-CimInstance Win32_Process -Filter " + ps_literal(f"ProcessId={pid}") + ";"
        data_guard = (" -or $p.CommandLine.IndexOf(" + ps_literal(self.data)
                      + ",[StringComparison]::OrdinalIgnoreCase) -lt 0") if self.isolated else ""
        powershell(prefix + "if (!$p -or $p.CommandLine -notmatch '-m\\s+hoard_link\\.hub\\s'"
                   + data_guard + ")"
                   " {exit 8}; Stop-Process -Id " + str(pid))
        wait_for(lambda: self.listener() is None)

    def sealed_launch(self):
        return json.loads(protect((self.snapshot / "hub-launch.dpapi").read_bytes(), decrypt=True))

    def launch_files(self, attempt):
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            raise ValueError("Invalid Hub launch receipt")
        return tuple(self.snapshot / f"hub-{attempt}.{name}" for name in ("log", "cancel", "exited"))

    def cancel_pending_hub(self):
        active = self.snapshot / "hub-attempt.json"
        if active.is_file():
            _, cancel, exited = self.launch_files(read_json(active)["attempt"])
            cancel.touch()
            wait_for(exited.is_file, seconds=20)

    def start_hub(self):
        if self.listener() is not None:
            self.configuration()
            return
        if self.isolated:
            voice_root, voice_app = (self.new, NEW_ID) if self.new.is_dir() else (self.voice.path, OLD_ID)
            self.materialize_fixture_launcher(voice_app, voice_root)
            self.materialize_fixture_launcher(OTHER_ID, self.vit.path)
        launcher = Path(self.plan["launcher"]).resolve()
        attempt = uuid.uuid4().hex
        log, cancel, exited = self.launch_files(attempt)
        argv = [self.plan["python"], "-B", str(Path(__file__).resolve()), "hub-child",
                str(self.plan_file), "--attempt", attempt]
        bootstrap = self.snapshot / f"hub-{attempt}.ps1"
        lines = ["$ErrorActionPreference = 'Stop'"]
        if self.isolated:
            lines.extend(f"$env:{key} = {ps_literal(value)}" for key, value in CLOSED_ENV.items())
            lines.append(f"$env:FAUSTUS_DIR = {ps_literal(self.root / 'faustus')}")
        lines.extend(("& " + " ".join(map(ps_literal, argv)), "exit $LASTEXITCODE"))
        commit_file(bootstrap, ("\n".join(lines) + "\n").encode("utf-8-sig"))
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(bootstrap)]
        source = ("& " + ps_literal(launcher) + " -Log " + ps_literal(log) + " -Cwd "
                  + ps_literal(self.hub.path) + " -Cmd @(" + ",".join(map(ps_literal, argv)) + ")")
        before = self.windows()
        receipt = json.loads(powershell(source))
        if receipt["rc"] != 0:
            raise RuntimeError(f"The approved launcher failed: {receipt}")
        save_json(self.snapshot / "hub-attempt.json", {"attempt": attempt, "launcher_pid": receipt["pid"]})
        try:
            wait_for(self.configuration, seconds=55)
            self.record_windows(before, f"hub-{attempt}")
        except BaseException:
            cancel.touch()
            wait_for(exited.is_file, seconds=20)
            raise
        if not self.isolated:
            expected = self.sealed_launch()
            live = read_owner(self.listener(), self.hub.path, with_environment=True)
            if any(live[key] != expected[key] for key in ("argv", "cwd", "env")):
                raise ValueError("Hub startup did not preserve the captured command and environment")

    def windows(self):
        log = Path(self.plan["window_watch"])
        source = (
            "$p=@(Get-Process -Name powershell,cmd,python,pythonw,node,WindowsTerminal,OpenConsole,conhost "
            "-ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -ExpandProperty Id);"
            "$n=@(Select-String -LiteralPath " + ps_literal(log) + " -Pattern 'VENTANA' -ErrorAction Stop).Count;"
            "@{visible=$p;vigia=$n} | ConvertTo-Json -Compress"
        )
        return json.loads(powershell(source))

    def record_windows(self, before, label):
        after = self.windows()
        change = sorted(set(after["visible"]) - set(before["visible"]))
        save_json(self.snapshot / f"desktop-{label}-{uuid.uuid4().hex[:8]}.json",
                  {"before": before, "after": after, "new_visible": change})

    def linked_apps(self):
        if self.isolated:
            active = self.instances()
            return {name: name in active for name in (OLD_ID, OTHER_ID)}
        return {name: bool(self.app_ready(name)) for name in (OLD_ID, OTHER_ID)}

    def git_status(self, repo, path=None):
        # Porcelain reserves two columns for the index and worktree status.
        output = repo.git("status", "--porcelain=v1", "--untracked-files=normal", path=path, strip_output=False)
        return sorted(output.splitlines())

    def inventory(self):
        layout = self.configuration()
        if self.new.exists():
            raise ValueError("The destination folder already exists")
        states = {}
        for name, repo in self.repos.items():
            reject_redirected(repo.path)
            if repo.git("rev-parse", "HEAD") != repo.before:
                raise ValueError(f"Unexpected installed ref in {name}")
            status = self.git_status(repo)
            protected = {
                " M vitruvius_hoard/showcase/assets/provenance.json",
                "?? theseus_tests/fixtures/managed_oracle/obj/",
            } if name == "vit" else set()
            if name == "voice":
                protected.update(entry for entry in status if re.fullmatch(
                    r"\?\? \.venv-(?:antes-voz|voz-fallida)-[A-Za-z0-9_-]+/", entry))
            if set(status) - protected:
                raise ValueError(f"Unaccounted local changes in {name}; preserve them before migration")
            if name == "vit" and repo.git("diff", "--name-only", repo.before, repo.after, "--",
                                          *(entry[3:] for entry in protected), "vitruvius_hoard/static"):
                raise ValueError("The candidate overlaps protected local paths or the staged client")
            repo.git("merge-base", "--is-ancestor", repo.before, repo.after)
            markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")
            paths = repo.git("rev-parse", "--path-format=absolute",
                             *(arg for marker in markers for arg in ("--git-path", marker))).splitlines()
            if any(Path(path).exists() for path in paths):
                raise ValueError(f"Another Git operation is active in {name}")
            states[name] = {"branch": repo.git("symbolic-ref", "--short", "HEAD"), "status": status}
            if not self.isolated and states[name]["branch"] != "main":
                raise ValueError(f"Unexpected installed branch in {name}")
            if not self.isolated and states[name]["branch"] != "main":
                raise ValueError(f"Unexpected installed branch in {name}")
        if self.isolated:
            if not (self.voice.path / ".venv" / "Scripts" / "python.exe").is_file():
                raise ValueError("The isolated rehearsal needs a real fixture venv")
        else:
            if set(self.plan.get("approved_commits", [])) != {repo.after for repo in self.repos.values()}:
                raise ValueError("The installation plan needs all three exact reviewed commits")
            if self.vit.git("diff", "--name-only", self.vit.before, self.vit.after, "--",
                            "client", "package.json", "package-lock.json", "vite.config.js"):
                raise ValueError("The prepared plan requires the already verified Vitruvius client")
        active = self.linked_apps()
        if not all(active.values()):
            raise ValueError("Both original applications must be ready before migration")
        save_json(self.snapshot / "inventory.json", {
            "repositories": states, "roots": layout["roots"], "active": active,
            "venv": sha256(self.voice.path / ".venv" / "pyvenv.cfg"), "hub_pid": self.listener(),
            "vit_files": self.vit_files(),
        })

    def vit_files(self):
        tracked = ("vitruvius_hoard/showcase/assets/provenance.json", "vitruvius_hoard/static/index.html")
        return {name: sha256(self.vit.path / name) for name in tracked if (self.vit.path / name).is_file()}

    def snapshot_configs(self):
        entries = {}
        for name in CONFIG_FILES:
            path = self.data / name
            if path.is_file():
                backup = self.snapshot / f"before-{name}"
                commit_file(backup, path.read_bytes())
                entries[name] = sha256(backup)
        if "hub.json" not in entries:
            raise ValueError("The registry is required for recovery")
        entries["inventory.json"] = sha256(self.snapshot / "inventory.json")
        self.prepare_registry(entries)
        entries["candidate-config.json"] = sha256(self.snapshot / "candidate-config.json")
        entries["voice-runtime-before.json"] = sha256(self.snapshot / "voice-runtime-before.json")
        if not self.isolated:
            launch = read_owner(read_json(self.snapshot / "inventory.json")["hub_pid"], self.hub.path,
                                with_environment=True)
            commit_file(self.snapshot / "hub-launch.dpapi", protect(json.dumps(launch).encode()))
            entries["hub-launch.dpapi"] = sha256(self.snapshot / "hub-launch.dpapi")
            self.snapshot_dependencies()
            for name in ("python.json", "voice-requirements.txt"):
                entries[name] = sha256(self.snapshot / name)
        save_json(self.snapshot / "recovery.json", entries)

    def check_recovery(self):
        entries = read_json(self.snapshot / "recovery.json")
        metadata = {"inventory.json", "candidate-config.json", "voice-runtime-before.json"}
        if not self.isolated:
            metadata.update(("hub-launch.dpapi", "python.json", "voice-requirements.txt"))
        if not {"hub.json", *metadata} <= entries.keys() or set(entries) - {*CONFIG_FILES, *metadata}:
            raise ValueError("Incomplete recovery inventory")
        for name, expected in entries.items():
            backup = self.snapshot / (name if name in metadata else f"before-{name}")
            if sha256(backup) != expected:
                raise ValueError(f"The recovery asset changed: {name}")
        return {name: entries[name] for name in CONFIG_FILES if name in entries}

    def stop_services(self):
        backups = self.check_recovery()
        if self.listener() != read_json(self.snapshot / "inventory.json")["hub_pid"]:
            raise ValueError("The Hub listener changed after the inventory")
        for name, expected in read_json(self.snapshot / "candidate-config.json").items():
            if sha256(self.snapshot / f"candidate-{name}") != expected:
                raise ValueError(f"The staged configuration changed: {name}")
        for name in CONFIG_FILES:
            original = self.data / name
            if (sha256(original) if original.is_file() else None) != backups.get(name):
                raise ValueError(f"Configuration changed after the inventory: {name}")
        self.mark("stop_started")
        for app, active in read_json(self.snapshot / "inventory.json")["active"].items():
            if active:
                self.stop_app(app)
        self.stop_hub()

    def advance_repositories(self):
        self.mark("git_started")
        for repo in self.repos.values():
            if repo.git("rev-parse", "HEAD") != repo.before:
                raise ValueError("A repository changed after inventory")
            repo.git("merge", "--ff-only", repo.after)
            if repo.git("rev-parse", "HEAD") != repo.after:
                raise ValueError(f"The checkout did not reach {repo.after}")
        self.voice.path.rename(self.new)
        self.voice.git("worktree", "repair", path=self.new)
        if self.voice.git("rev-parse", "HEAD", path=self.new) != self.voice.after:
            raise ValueError("Git failed to open the moved repository")

    def moved(self, value):
        if not isinstance(value, str):
            return value
        legacy = str(self.voice.path).replace("\\", "/")
        portable = value.replace("\\", "/")
        if portable.lower() == legacy.lower() or portable.lower().startswith(legacy.lower() + "/"):
            return str(self.new) + value[len(legacy):]
        return value

    def voice_runtime(self, root, profile):
        prefix = "BOLTAGON" if root == self.new else "CYRANO"
        executable = Path(profile[f"{prefix}_TTS_PYTHON"])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("The voice subprocess must use its own absolute Python path")
        reject_redirected(executable)
        argv = [str(executable), "-I", "-B", "-c", package_runtime_probe()]
        if not self.isolated:
            module = "boltagon_hoard" if root == self.new else "cyrano_hoard"
            argv.append(str(root / module / "voice_worker.py"))
        result = json.loads(cmd(argv, cwd=root))
        if Path(result.pop("prefix")).resolve() != executable.parent.parent.resolve():
            raise ValueError("The voice subprocess Python resolved to a different environment")
        if not self.isolated and profile[f"{prefix}_TTS_ENGINE"] == "qwen3-tts" and len(result["packages"]) != 4:
            raise ValueError("The separate Qwen environment is incomplete")
        return result

    def snapshot_dependencies(self):
        executable = str(self.voice.path / ".venv" / "Scripts" / "python.exe")
        links = self.voice_wheel_links()
        editable = json.loads(cmd([executable, "-m", "pip", "list", "--editable", "--format=json"]))
        all_packages = json.loads(cmd([executable, "-m", "pip", "list", "--format=json"]))
        editable_names = {row["name"].lower() for row in editable}
        locked = self.snapshot / "voice-requirements.txt"
        pins = [f"{row['name']}=={row['version']}" for row in all_packages
                if row["name"].lower() not in editable_names | {"pip"}]
        commit_file(locked, ("\n".join(pins) + "\n").encode("utf-8"))
        cmd([executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
             "--no-deps", "--no-index", "--only-binary=:all:", *links, "-r", str(locked)])
        base = cmd([executable, "-I", "-c", "import sys; print(sys._base_executable)"])
        if not Path(base).is_file():
            raise ValueError("The main voice environment has no reusable base interpreter")
        save_json(self.snapshot / "python.json", {"base": base, "editables": editable, "pins": sha256(locked)})

    def voice_wheel_links(self, *, moved=False):
        wheelhouses = [self.plan["voice_wheels"]]
        if extra := self.plan.get("voice_extra_wheels"):
            wheelhouses.append(extra)
        links = []
        for entry in wheelhouses:
            path = Path(self.moved(entry) if moved else entry)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"Supply the verified local voice wheelhouse: {path}")
            reject_redirected(path)
            links.extend(("--find-links", str(path)))
        return links

    def rebuild_environment(self):
        source = self.new / ".venv"
        reject_redirected(source)
        self.mark("environment_started")
        target = source
        target.rename(self.snapshot / "voice-venv-original")
        if self.isolated:
            seed_voice_fixture_venv(self.plan["python"], target, self.plan.get("fixture_websockets"))
        else:
            details = read_json(self.snapshot / "python.json")
            locked = self.snapshot / "voice-requirements.txt"
            if sha256(locked) != details["pins"]:
                raise ValueError("The dependency inventory changed after preflight")
            cmd([details["base"], "-m", "venv", str(target)])
            python = str(target / "Scripts" / "python.exe")
            install = [python, "-m", "pip", "install", "--no-deps", "--no-index"]
            cmd([*install, "--only-binary=:all:", *self.voice_wheel_links(moved=True),
                 "-r", str(locked)])
            for pkg in details["editables"]:
                if pkg["name"].lower().replace("_", "-") not in ("cyrano-hoard", "boltagon-hoard"):
                    cmd([*install, "--no-build-isolation", "-e", self.moved(pkg["editable_project_location"])])
            cmd([*install, "--no-build-isolation", "-e", str(self.new)])
            cmd([python, "-m", "pip", "check"])
            cmd([python, "-I", "-B", "-c", "import websockets"])
            imported = cmd([python, "-I", "-c", "import boltagon_hoard; print(boltagon_hoard.__file__)"])
            if Path(imported).resolve() != self.new / "boltagon_hoard" / "__init__.py":
                raise ValueError("The rebuilt environment imports a different checkout")
            cmd([python, "-I", "-m", "boltagon_hoard", "--help"])
        candidate = read_json(self.snapshot / "candidate-hub.json")["launch_overrides"][NEW_ID]
        result = self.voice_runtime(self.new, candidate["env"])
        if result != read_json(self.snapshot / "voice-runtime-before.json"):
            raise ValueError("The moved child interpreter changed version or dependency metadata")
        save_json(self.snapshot / "voice-runtime-after.json", result)

    def switch(self, value):
        if isinstance(value, str):
            if value.casefold() == OLD_ID:
                return NEW_ID
            if value == "cyrano_hoard":
                return "boltagon_hoard"
            return self.moved(value)
        if isinstance(value, list):
            return [self.switch(item) for item in value]
        if isinstance(value, dict):
            rewritten = {}
            for key, item in value.items():
                target = self.switch(key)
                converted = item if key.endswith("_REFERENCE_TEXT") else self.switch(item)
                if target in rewritten and converted != rewritten[target]:
                    raise ValueError(f"Colliding configuration entry: {target}")
                rewritten[target] = converted
            return rewritten
        return value

    def prepare_registry(self, backups):
        registry = read_json(self.snapshot / "before-hub.json")
        if "launch_overrides" not in registry:
            raise ValueError("The installed launch profiles are missing")
        overrides = registry["launch_overrides"]
        if NEW_ID in overrides or OLD_ID not in overrides or OTHER_ID not in overrides:
            raise ValueError("Missing or ambiguous launch overrides")
        original = overrides.pop(OLD_ID)
        save_json(self.snapshot / "voice-runtime-before.json", self.voice_runtime(self.voice.path, original["env"]))
        voice = self.switch(original)
        voice["executable"] = str(self.new / ".venv" / "Scripts" / "python.exe")
        voice["cwd"] = str(self.new)
        if "boltagon_hoard" not in voice["argv"] or "--no-browser" not in voice["argv"]:
            raise ValueError("The voice command is not a headless package launcher")
        env = voice["env"]
        for key, value in tuple(env.items()):
            if key.startswith("CYRANO_"):
                canonical = "BOLTAGON_" + key.removeprefix("CYRANO_")
                if canonical in env and env[canonical] != value:
                    raise ValueError(f"Conflicting settings: {key}")
                env[canonical] = value
                if not key.startswith("CYRANO_TTS_"):
                    env.pop(key)
        data = env.setdefault("BOLTAGON_DATA_DIR", str(self.new / "data"))
        env.setdefault("BOLTAGON_TOKEN_FILE", str(Path(data) / "mcp-token"))
        env["PORT_STRICT"] = "1"
        for suffix in ("TTS_QWEN_MODEL_DIR", "TTS_QWEN_REFERENCE_WAV"):
            if not Path(original["env"]["CYRANO_" + suffix]).exists():
                raise ValueError(f"The original voice asset is missing: {suffix}")
        overrides[NEW_ID] = voice
        vit = self.switch(overrides[OTHER_ID])
        if any(not vit.get(key) for key in ("executable", "argv", "cwd", "readiness_url", "env")):
            raise ValueError("The Vitruvius override needs its full launcher")
        overrides[OTHER_ID] = vit
        for key in ("roots", "repos", "exclude_ids", "profiles", "fleet", "keepalive"):
            if key in registry:
                registry[key] = self.switch(registry[key])
        save_json(self.snapshot / "candidate-hub.json", registry)
        for filename in CONFIG_FILES[1:]:
            target = self.snapshot / f"candidate-{filename}"
            if filename in backups:
                old = read_json(self.snapshot / f"before-{filename}")
                output = self.switch(old)
                if filename == "manual-stops.json":
                    stops = set(output.get("stopped", ()))
                    output["stopped"] = sorted(stops | {NEW_ID, OTHER_ID})
                if old != output or filename == "manual-stops.json":
                    save_json(target, output)
            elif filename == "manual-stops.json":
                save_json(target, {"stopped": [NEW_ID, OTHER_ID]})
        expected = {name: sha256(self.snapshot / f"candidate-{name}") for name in CONFIG_FILES
                    if (self.snapshot / f"candidate-{name}").is_file()}
        save_json(self.snapshot / "candidate-config.json", expected)

    def migrate_registry(self):
        files = read_json(self.snapshot / "candidate-config.json")
        if not {"hub.json", "manual-stops.json"} <= files.keys() or set(files) - set(CONFIG_FILES):
            raise ValueError("The staged registry is incomplete")
        for name, expected in files.items():
            if sha256(self.snapshot / f"candidate-{name}") != expected:
                raise ValueError(f"The staged configuration changed: {name}")
        self.mark("registry_started")
        for name in files:
            path = self.snapshot / f"candidate-{name}"
            commit_file(self.data / name, path.read_bytes())
        self.verify_launchers()

    def resolve_launchers(self, folders):
        source = """
import json
from pathlib import Path
import sys
from hoard_link.hub.registry import apply_launch_override, read_manifest

config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
for folder in sys.argv[3:]:
    folder = Path(folder)
    app = read_manifest(folder / sys.argv[2])
    app = apply_launch_override(app, config['launch_overrides'][app.id])
    if not app.launchable or Path(app.launch.cwd).resolve() != folder:
        raise ValueError('Invalid launcher for ' + app.id)
    print(json.dumps({'id': app.id, 'executable': app.launch.executable,
                      'cwd': app.launch.cwd, 'argv': app.launch.argv, 'env': app.launch.env}))
"""
        manifest = "rehearsal-manifest.json" if self.isolated else "faustus-plugin.json"
        argv = [self.plan["python"], "-B", "-P", "-c", source, str(self.data / "hub.json"), manifest,
                *map(str, folders)]
        return [json.loads(line) for line in cmd(argv, cwd=self.hub.path, env=self.flags).splitlines()]

    def materialize_fixture_launcher(self, app, path):
        # The isolated executor rereads the manifest. Materialize the registry's
        # resolved command in this synthetic fixture before asking it to start.
        template_path = path / "rehearsal-manifest.json"
        manifest = path / "faustus-plugin.json"
        template = read_json(template_path)
        if (not self.isolated or not template_path.resolve().is_relative_to(self.root)
                or not manifest.resolve().is_relative_to(self.root)
                or template.get("x-family", {}).get("rename_rehearsal") is not True):
            raise ValueError("A registered temporary rehearsal fixture is required")
        launch, = self.resolve_launchers([path])
        if launch["id"] != app or not Path(launch["executable"]).is_relative_to(self.root):
            raise ValueError("The override is outside the rehearsal")
        for key in ("CYRANO_TTS_PYTHON", "BOLTAGON_TTS_PYTHON", "VITRUVIUS_VOICE_CACHE"):
            if key in launch["env"] and not Path(launch["env"][key]).resolve().is_relative_to(self.root):
                raise ValueError(f"{key} must stay inside the rehearsal")
        values = {key: value for key, value in CLOSED_ENV.items() if key != "HOARD_HUB_TOKEN_FILE"}
        for key in values:
            if key.endswith("_TOKEN_FILE"):
                values[key] = str(self.root / "unavailable-token")
        template["app"]["launch_hint"].update(
            executable=launch["executable"], cwd=launch["cwd"], argv=launch["argv"],
            env={**values, **launch["env"]},
        )
        save_json(manifest, template)

    def verify_launchers(self):
        reply = self.resolve_launchers([self.new, self.vit.path])
        if [row["id"] for row in reply] != [NEW_ID, OTHER_ID]:
            raise ValueError("The candidate launchers were not resolved")
        for row in reply:
            row["env_keys"] = sorted(row.pop("env"))
        save_json(self.snapshot / "resolved-launchers.json", reply)


    def start_services(self):
        self.mark("candidate_started")
        self.start_hub()
        apps = self.tool("hub_rescan")["apps"]
        if NEW_ID not in apps or OLD_ID in apps or OTHER_ID not in apps:
            raise ValueError("The candidate catalogue does not contain the expected app identities")
        for app, path in ((NEW_ID, self.new), (OTHER_ID, self.vit.path)):
            self.start_app(app, path)
            origin = self.app_origin(app)
            wait_for(lambda: self.http(origin + "/api/health"))

    def accept(self):
        inventory = read_json(self.snapshot / "inventory.json")
        if self.vit_files() != inventory["vit_files"] or self.git_status(self.vit) != inventory["repositories"]["vit"]["status"]:
            raise ValueError("The installed client or a protected local file changed")
        voice = self.http(self.app_origin(NEW_ID) + "/api/health")
        vit = self.http(self.app_origin(OTHER_ID) + "/api/health")
        if voice.get("id") != NEW_ID or vit.get("service") != "vitruvius-hoard":
            raise ValueError("Unexpected identity in the candidate health receipt")
        profile = read_json(self.data / "hub.json")["launch_overrides"][NEW_ID]["env"]
        engine = profile["BOLTAGON_TTS_ENGINE"]
        def ready_voice():
            status = self.app_call(NEW_ID, "voice_status", {})
            child = status.get("child") or {}
            if status.get("engine_id") != engine:
                return None
            if engine == "off" or (status.get("state") == "ready" and status.get("loaded")
                    and child.get("state") == "ready" and type(child.get("pid")) is int
                    and child["pid"] > 0 and child["pid"] != voice.get("pid")):
                return status
            return None

        status = wait_for(ready_voice, seconds=255)
        voice_clip = None
        if engine != "off":
            child = status["child"]
            powershell(f"Get-Process -Id {child['pid']} -ErrorAction Stop | Out-Null")
            voice_clip = self.app_call(NEW_ID, "voice_speak", {
                "text": f"La comprobación de voz lleva la clave {uuid.uuid4().hex[:8]}.",
                "lang": "es-ES", "wait": True,
            })
            audio = voice_clip["timings_ms"]["first_audio"]
            if voice_clip.get("state") != "succeeded" or voice_clip.get("cached") is not False or not 0 <= audio <= 1200:
                raise ValueError("The uncached voice probe exceeded 1200 ms or did not finish")
        wav = Path(self.plan["wav"])
        transcript = self.app_call(NEW_ID, "speech_transcribe", {
            "path": str(wav), "language": "es-ES", "fallback": False, "timeout_s": 30,
        })
        if not re.search(r"(?:veinte|20)\s+euros", transcript.get("text", ""), re.IGNORECASE):
            raise ValueError("The candidate did not transcribe the WAV")
        drawn = self.app_call(NEW_ID, "dictation_understand", {
            "text": transcript["text"], "language": "es-ES",
            "schema": {"type": "object", "properties": {"amount_cents": {"type": "integer", "format": "amount_cents"}}},
        })
        if drawn["fields"]["amount_cents"]["value"] != 2000:
            raise ValueError("The candidate did not return 2000 in the dictation probe")
        preview = self.app_call(OTHER_ID, "app_project_preview", {
            "id": self.plan["project"], "operation": "start",
        })
        if preview["state"] != "running":
            raise ValueError("The test preview did not start")
        open_path = preview["open_path"]
        if not open_path.startswith("/") or open_path.startswith("//") or "\\" in open_path:
            raise ValueError("The preview did not return an app-relative stable URL")
        preview_result = self.app_origin(OTHER_ID) + open_path
        try:
            self.http(preview_result, raw=True)
        except urllib.error.HTTPError as exc:
            destination = urlsplit(exc.headers.get("Location", ""))
            if (exc.code not in {301, 302, 303, 307, 308} or destination.scheme != "http"
                    or destination.netloc != f"127.0.0.1:{destination.port}"
                    or not destination.port or (self.isolated and destination.port in LIVE_PORTS)):
                raise
            self.http(destination.geturl(), raw=True)
        save_json(self.snapshot / "accepted.json", {"voice": voice, "vitruvius": vit, "child": status,
                  "clip": voice_clip, "transcription": transcript, "dictation": drawn,
                  "preview": preview, "preview_url": preview_result})

    def apply(self, stop_after=None):
        benchmark = None if self.isolated else validate_voice_benchmark(
            self.plan.get("voice_benchmark"), self.voice.after,
        )
        self.snapshot.mkdir(exist_ok=True)
        if self.journal_file.exists():
            raise ValueError("Use a new snapshot for apply, or roll back the existing journal")
        if stop_after is not None and not self.isolated:
            raise ValueError("Injected checkpoints are confined to isolated rehearsals")
        self.state = {"plan": self.digest, "completed": []}
        if benchmark is not None:
            save_json(self.snapshot / "voice-benchmark-receipt.json", benchmark)
        save_json(self.journal_file, self.state)
        for index, name in enumerate(STEPS, 1):
            getattr(self, name)()
            self.state["completed"].append(name)
            save_json(self.journal_file, self.state)
            print(f"{index} {name}", flush=True)
            if index == stop_after:
                raise Checkpoint(f"Isolated interruption after phase {index}")

    def restore_apps(self, inventory):
        for app, path in ((OLD_ID, self.voice.path), (OTHER_ID, self.vit.path)):
            if inventory["active"][app]:
                self.start_app(app, path)
                origin = self.app_origin(app)
                wait_for(lambda: self.http(origin + "/api/health"))

    def rollback(self):
        self.state = read_json(self.journal_file)
        if self.state["plan"] != self.digest:
            raise ValueError("The plan does not own this snapshot")
        if self.state.get("rolled_back"):
            return
        if not self.state.get("stop_started"):
            self.mark("rolled_back")
            return
        inventory = read_json(self.snapshot / "inventory.json")
        backups = self.check_recovery()
        old_exists = self.voice.path.is_dir()
        new_exists = self.new.is_dir()
        if old_exists == new_exists:
            raise ValueError("The voice folder cannot be proven before rolling back")
        located = self.new if new_exists else self.voice.path
        choices = {"voice": located, "hub": self.hub.path, "vit": self.vit.path}
        for name, repo in self.repos.items():
            path = choices[name]
            if repo.git("symbolic-ref", "--short", "HEAD", path=path) != inventory["repositories"][name]["branch"]:
                raise ValueError(f"The branch changed in {name}")
            if repo.git("rev-parse", "HEAD", path=path) not in {repo.before, repo.after}:
                raise ValueError(f"Unexpected revision in {name}")
        saved = self.snapshot / "voice-venv-original"
        if not ((saved if saved.is_dir() else located / ".venv") / "Scripts" / "python.exe").is_file():
            raise ValueError("The original voice interpreter is missing")
        if sha256((saved if saved.is_dir() else located / ".venv") / "pyvenv.cfg") != inventory["venv"]:
            raise ValueError("The original voice interpreter configuration changed")
        if self.listener():
            if not self.state.get("git_started"):
                self.restore_apps(inventory)
                self.mark("rolled_back")
                return
            for app in ((NEW_ID if new_exists else OLD_ID), OTHER_ID):
                self.stop_app(app)
            self.stop_hub()
        else:
            self.cancel_pending_hub()
        if saved.is_dir():
            target = located / ".venv"
            if target.exists():
                target.rename(self.snapshot / f"voice-venv-rejected-{time.time_ns()}")
            saved.rename(target)
        for name, repo in self.repos.items():
            if repo.git("rev-parse", "HEAD", path=choices[name]) != repo.before:
                repo.git("reset", "--keep", repo.before, path=choices[name])
        if new_exists:
            self.new.rename(self.voice.path)
            self.voice.git("worktree", "repair")
        for name, repo in self.repos.items():
            if repo.git("rev-parse", "HEAD") != repo.before or self.git_status(repo) != inventory["repositories"][name]["status"]:
                raise ValueError(f"Recovery did not restore checkout and local files in {name}")
        if self.vit_files() != inventory["vit_files"]:
            raise ValueError("Recovery did not preserve the Vitruvius client")
        for name in CONFIG_FILES:
            original = self.data / name
            if name in backups:
                commit_file(original, (self.snapshot / f"before-{name}").read_bytes())
            else:
                original.unlink(missing_ok=True)
        self.start_hub()
        apps = self.tool("hub_rescan")["apps"]
        if OLD_ID not in apps or NEW_ID in apps or OTHER_ID not in apps:
            raise ValueError("Recovery did not restore the original registry")
        self.restore_apps(inventory)
        self.mark("rolled_back")

    def hub_child(self, attempt):
        # This is invoked by nw.ps1's approved, hidden pythonw wrapper.
        _, cancel, exited = self.launch_files(attempt)
        try:
            if cancel.exists():
                return 1
            if self.isolated and any(os.environ.get(key, "") != value for key, value in CLOSED_ENV.items()):
                raise ValueError("The hidden bootstrap must set the closed test environment")
            argv, cwd, env = self.hub_command()
            with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) as hub:
                while hub.poll() is None:
                    if cancel.exists() and hub.poll() is None:
                        powershell(f"Stop-Process -Id {hub.pid} -ErrorAction Stop")
                    time.sleep(.2)
                return hub.returncode
        finally:
            exited.touch()

    def hub_command(self):
        if not self.isolated:
            original = self.sealed_launch()
            return original["argv"], original["cwd"], original["env"]
        env = dict(self.flags)
        if self.isolated:
            for name in list(env):
                if name.startswith(("HOARD_HUB_", "HOARD_FAUSTUS_")):
                    del env[name]
            env.update(CLOSED_ENV)
            env.update(HOARD_HUB_URL=self.url, HOARD_HUB_TOKEN_FILE=str(self.token), HOARD_HUB_JOBS="0",
                       FAUSTUS_DIR=str(self.root / "faustus"))
        env["PYTHONPATH"] = str(self.hub.path)
        argv = [
            self.plan["python"], "-P", "-m", "hoard_link.hub", "--no-window", "--port", str(self.port),
            "--data-dir", str(self.data),
        ]
        if self.hub.git("rev-parse", "HEAD") == self.hub.before and not (self.hub.path / "hoard_link"
                / "hub" / "loopback_ports.py").is_file():
            # The old Hub is otherwise executed from its exact Git checkout.
            # Replace only its bind-to-zero allocator in the fixture process.
            bootstrap = (
                "import runpy, sys; from hoard_link.hub import isolated_apps; "
                "isolated_apps._free_ports = runpy.run_path(sys.argv[1])['free_loopback_ports']; "
                "sys.argv = sys.argv[3:]; runpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)"
            )
            argv[2:2] = ["-c", bootstrap, str(LOOPBACK_ALLOCATOR)]
        return argv, self.hub.path, env


class Checkpoint(RuntimeError):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "rollback", "fixture-boot", "fixture-stop", "hub-child"))
    parser.add_argument("plan", type=Path)
    parser.add_argument("--stop-after", type=int, choices=range(1, len(STEPS) + 1))
    parser.add_argument("--confirm", choices=("INSTALL", "ROLLBACK"))
    parser.add_argument("--attempt")
    args = parser.parse_args()
    run = Migration(args.plan)
    run.plan_file = args.plan.resolve()
    if args.action == "hub-child":
        return run.hub_child(args.attempt or "")
    if args.action in ("fixture-boot", "fixture-stop") and not run.isolated:
        raise ValueError("Fixture operations require an isolated plan")
    if args.action == "fixture-boot":
        run.snapshot.mkdir(exist_ok=True)
        run.start_hub()
        run.start_app(OLD_ID, run.voice.path)
        run.start_app(OTHER_ID, run.vit.path)
    elif args.action == "fixture-stop":
        run.stop_hub()
    else:
        expected = "INSTALL" if args.action == "apply" else "ROLLBACK"
        if args.confirm != expected:
            parser.error(f"{args.action} requires --confirm {expected}")
        with migration_lock(run.data / "rename-migration.lock"):
            if args.action == "rollback":
                run.rollback()
            else:
                try:
                    run.apply(args.stop_after)
                except Checkpoint as exc:
                    print(exc, file=sys.stderr)
                    return 76
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
