"""Where Faustus, Hermes and Prometheus run: on this PC or on a Spark, never on both at once.

The switch is a job that stops the three apps on one side, copies their code and data to the other side, fixes the
settings that hold machine paths, and starts them there. While they live on the Spark the Hub refuses to start them on
this PC (by hand, automatically or from "start all"), and Faustus's own launcher on the PC refuses too (a marker in
``<faustus>/data/runtime/placement.json``), so the PC can be switched off and the stack keeps working on the Spark,
reachable from the tailnet. Coming back reverses every step.

State lives in ``<hub data>/placement.json``. Transfers are incremental (size + modification time) and stream over SSH
as tar, so after the first move only what changed travels and nothing is staged on disk. Files the PC would lose when
the data comes back are moved to ``<hub data>/placement-trash/<when>/`` instead of being deleted. Code goes as
``git archive`` of each repository's HEAD (plus the built UI of Faustus, which is not in git); the Spark keeps one
runtime per app under ``~/faustus-stack``, run by systemd user services. ``placement_remote.py`` is the half that runs
on the Spark (standard library only).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import placement_remote as pr

APPS = ("faustus", "hermes", "prometheus")
DEFAULT_NODE = {"ssh": "Spark3", "name": "Spark3", "host": "192.168.0.185"}
STACK = "~/faustus-stack"
SPARK_HOME = "/home/luissalet"
SPARK_STACK = f"{SPARK_HOME}/faustus-stack"
SPARK_KEY = "~/.ssh/id_ed25519_dsv41_fabric"       # the key every Spark of the cluster accepts
REMOTE_HELPER = f"{STACK}/placement_remote.py"
THREAD_NAME = "placement-switch"
MIN_FREE_GB = 4.0
PORTS = {"faustus": 7000, "hermes": 5221, "prometheus": 5205}
HEALTH = {"faustus": "/", "hermes": "/api/health", "prometheus": "/api/health"}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15"]
PAUSED_ON_SPARK = [
    "Hermes: la vigilancia de ventanas abiertas de tu navegador («Mi ventana abierta»); necesita Windows y tu navegador",
    "Faustus: el control de tu navegador real y del escritorio de Windows",
    "Faustus: los modelos de Ollama de este PC y los servidores MCP que solo existen en Windows",
]
# Caches and per-machine browser profiles stay on each side; everything else is the app's state and moves with it.
COMMON_EXCLUDES = ["*.placement-tmp", "*/browser-profile", "browser-profile", "placement-elsewhere.json",
                   "*.tmp", "*.tmp.*", "*.lock"]           # atomic-write leftovers and locks are never state
DATA = {
    "faustus": ("data", ["fastembed_cache", "desktop-profile", "desktop-profile-apps", "reach-browser", "logs", "runtime",
                         "whatsapp", "voice-comparison-test"]),
    "hermes": ("data", ["legacy-material", "logs", "cache", "*/cache"]),
    "prometheus": ("data", ["cache", "logs"]),
}
EXTRA_UNTRACKED = {"faustus": ["static/studio"]}       # built UI, not in git
# Hermes settings rewritten while it runs on the Spark (database, key, value there; "{faustus}" = the Faustus folder there),
# saved beforehand and restored on the way back. Off Windows the toast and the Hub's bus reach nobody, so the parcel
# notices of Shipments also go by e-mail there (from "medium": on the way, out for delivery, delivered, problems).
HERMES_KEYS = [("data/native/products/data/tantalus.db", "notify.email.faustus_dir", "{faustus}"),
               ("data/native/shipments/data/phileas.db", "mail.faustus_dir", "{faustus}"),
               ("data/native/shipments/data/phileas.db", "notify.email.enabled", "1"),
               ("data/native/shipments/data/phileas.db", "notify.email.min_severity", "medium")]
HERMES_PATH_KEYS = [(db, key) for db, key, _ in HERMES_KEYS]
PROMETHEUS_PATH_KEYS = ["recipes_dir"]
MARKER = "placement-elsewhere.json"


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class PlacementError(RuntimeError):
    pass


class Placement:
    def __init__(self, hub: Any, *, runner: Optional[Callable[..., subprocess.CompletedProcess]] = None):
        self.hub = hub
        self.path = Path(hub.config.data_dir) / "placement.json"
        self.trash_root = Path(hub.config.data_dir) / "placement-trash"
        self.recipes_pc = Path(os.path.expanduser("~")) / "Desktop" / "Proyectos independientes" / "Sparks cluster" / "recipes"
        self._run = runner or subprocess.run
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        data = self.state()
        if (data.get("job") or {}).get("running"):        # the Hub stopped mid-switch: say so instead of looking busy forever
            data["job"].update(running=False, ok=False, step="error",
                               error="el Hub se cerró a mitad del traslado; vuelve a pulsar el botón para terminarlo")
            self._save(data)

    # ================================================================================= state
    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("location", "pc")
        data.setdefault("node", dict(DEFAULT_NODE))
        data.setdefault("job", {"running": False})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            data = self.state()
            data.update(changes)
            self._save(data)
            return data

    def _log(self, line: str, step: Optional[str] = None) -> None:
        with self._lock:
            data = self.state()
            job = data.get("job") or {}
            job.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {line}")
            job["log"] = job["log"][-80:]
            if step:
                job["step"] = step
            data["job"] = job
            self._save(data)

    def status(self) -> dict[str, Any]:
        data = self.state()
        if data["location"] == "sparks" and not data.get("urls") and not (data.get("job") or {}).get("running"):
            self._refresh_urls_later()
        return {"ok": True, "location": data["location"], "node": data["node"]["name"], "since": data.get("since"),
                "job": data.get("job"), "apps": list(APPS), "urls": data.get("urls") or {},
                "paused_on_spark": PAUSED_ON_SPARK, "nodes": ["Spark3"]}

    def local_block(self, app_id: str) -> Optional[str]:
        """Why ``app_id`` must not start on this PC now (None = it may). The switch itself is always allowed."""
        if app_id not in APPS or threading.current_thread().name == THREAD_NAME:
            return None
        data = self.state()
        if data["location"] == "sparks":
            return f"{app_id} se está ejecutando en {data['node']['name']}; cámbialo desde el Hub (Ejecutar en: PC)."
        if (data.get("job") or {}).get("running"):
            return f"{app_id} se está trasladando ahora mismo; espera a que termine el cambio de ubicación."
        return None

    # ================================================================================= ssh plumbing
    def _node(self) -> str:
        return self.state()["node"]["ssh"]

    def _ssh(self, command: str, timeout: float = 120, input: Optional[str] = None) -> str:
        # Bytes both ways: text mode on Windows would turn every "\n" sent into "\r\n" and read the Spark's UTF-8 as cp1252.
        r = self._run(["ssh", *SSH_OPTS, self._node(), command], capture_output=True, timeout=timeout,
                      input=input.encode("utf-8") if input is not None else None, creationflags=_flags())
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace")
            raise PlacementError(f"ssh {self._node()}: {(err or out).strip()[-400:]}")
        return out

    def _put_text(self, remote: str, text: str) -> None:
        self._ssh(f"cat > {remote}", input=text, timeout=60)

    def _remote(self, args: str, timeout: float = 600) -> Any:
        out = self._ssh(f"python3 {REMOTE_HELPER} {args}", timeout=timeout)
        return json.loads(out.strip().splitlines()[-1]) if out.strip() else None

    def _upload_helper(self) -> None:
        self._ssh(f"mkdir -p {STACK}")
        self._put_text(REMOTE_HELPER, Path(pr.__file__).read_text(encoding="utf-8"))
        mail = Path(__file__).resolve().parent.parent / "mail_helper.py"
        if mail.is_file():
            self._put_text(f"{STACK}/mail_helper.py", mail.read_text(encoding="utf-8"))

    def _stream(self, command: str) -> subprocess.Popen:
        return subprocess.Popen(["ssh", *SSH_OPTS, self._node(), command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=_flags())

    # ================================================================================= what lives where on the PC
    def local_dirs(self) -> dict[str, Path]:
        root, _python = self.hub.faustus_runtime.installation()
        out: dict[str, Optional[Path]] = {"faustus": Path(root) if root else None}
        for app_id in ("hermes", "prometheus"):
            app = self.hub.get(app_id)
            out[app_id] = Path(app.folder) if app is not None and getattr(app, "folder", None) else None
        missing = [k for k, v in out.items() if v is None or not v.is_dir()]
        if missing:
            raise PlacementError("no encuentro en este PC: " + ", ".join(missing))
        return out  # type: ignore[return-value]

    # ================================================================================= code
    def _deploy_code(self, app_id: str, folder: Path, deployed: dict[str, Any]) -> None:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True,
                              creationflags=_flags()).stdout.strip()
        extra = [p for p in EXTRA_UNTRACKED.get(app_id, []) if (folder / p).exists()]
        stamp = head + "|" + "|".join(f"{p}:{max((f.stat().st_mtime_ns for f in (folder / p).rglob('*') if f.is_file()), default=0)}"
                                      for p in extra)
        if deployed.get(app_id) == stamp and self._ssh(f"test -d {STACK}/{app_id} && echo ok || true").strip() == "ok":
            self._log(f"{app_id}: el código ya está al día en la Spark ({head[:8]})")
            return
        self._log(f"{app_id}: enviando el código {head[:8]}", step=f"código de {app_id}")
        # A clean tree each time (files removed from git must not linger); the venv and the data stay.
        proc = self._stream(f"mkdir -p {STACK}/{app_id} && find {STACK}/{app_id} -mindepth 1 -maxdepth 1 ! -name venv ! -name data"
                            f" -exec rm -rf {{}} + && tar -xf - -C {STACK}/{app_id}")
        archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=folder, capture_output=True, creationflags=_flags())
        if archive.returncode != 0:
            proc.kill()
            raise PlacementError(f"git archive {app_id}: {archive.stderr.decode(errors='replace').strip()[-300:]}")
        _out, err = proc.communicate(archive.stdout, timeout=1800)
        if proc.returncode != 0:
            raise PlacementError(f"código de {app_id}: {err.decode(errors='replace').strip()[-300:]}")
        for rel in extra:
            files = [p.relative_to(folder).as_posix() for p in (folder / rel).rglob("*") if p.is_file()]
            proc = self._stream(f"rm -rf {STACK}/{app_id}/{shlex.quote(rel)} && tar -xf - -C {STACK}/{app_id}")
            pr.pack_to(str(folder), files, proc.stdin)
            proc.stdin.close()
            _out, err = proc.communicate(timeout=1800)
            if proc.returncode != 0:
                raise PlacementError(f"{rel} de {app_id}: {err.decode(errors='replace').strip()[-300:]}")
        deployed[app_id] = stamp

    def _ensure_runtime(self, app_id: str, folder: Path, deployed: dict[str, Any]) -> None:
        # The committed requirements: that is what git archive sent (uncommitted edits stay on the PC).
        req = subprocess.run(["git", "show", "HEAD:requirements.txt"], cwd=folder, capture_output=True, creationflags=_flags())
        digest = hashlib.sha256(req.stdout).hexdigest()[:16] if req.returncode == 0 else ""
        key = app_id + ":requirements"
        if deployed.get(key) == digest and self._ssh(f"test -x {STACK}/{app_id}/venv/bin/python && echo ok || true").strip() == "ok":
            return
        self._log(f"{app_id}: instalando dependencias en la Spark (la primera vez tarda unos minutos)", step=f"dependencias de {app_id}")
        self._ssh(f"cd {STACK}/{app_id} && (test -x venv/bin/python || python3 -m venv venv) && venv/bin/pip install -q --upgrade pip"
                  f" && venv/bin/pip install -q -r requirements.txt", timeout=2400)
        if app_id == "hermes":
            self._ssh(f"cd {STACK}/hermes && venv/bin/python -m playwright install chromium >/dev/null 2>&1;"
                      f" sudo -n venv/bin/python -m playwright install-deps chromium >/dev/null 2>&1; true", timeout=2400)
        deployed[key] = digest

    def _ensure_node(self) -> list[str]:
        """Services survive logout (linger), Prometheus reaches every Spark (itself included) with the key the Sparks share,
        units written. Returns the services whose unit file changed."""
        self._ssh(f"sudo -n loginctl enable-linger $USER >/dev/null 2>&1; mkdir -p ~/.ssh {STACK}/prometheus-ssh; touch ~/.ssh/authorized_keys;"
                  f" if [ -f {SPARK_KEY}.pub ]; then grep -qF \"$(cat {SPARK_KEY}.pub)\" ~/.ssh/authorized_keys"
                  f" || cat {SPARK_KEY}.pub >> ~/.ssh/authorized_keys; fi")
        self._put_text(f"{STACK}/prometheus-ssh/config", self._prometheus_ssh_config())
        return list(self._remote(f"units {STACK}") or [])

    def _prometheus_ssh_config(self) -> str:
        """Prometheus's own SSH config on the Spark (``PROMETHEUS_SSH_DIR``): the same node aliases as on this PC, each with
        its address from Prometheus's settings and the key every Spark accepts. The Spark's own ~/.ssh/config is not touched."""
        try:
            nodes = json.loads((self.local_dirs()["prometheus"] / "data" / "settings.json").read_text(encoding="utf-8")).get("nodes") or []
        except (OSError, ValueError, PlacementError):
            nodes = []
        lines = ["# written by the Hoard Hub (placement): how Prometheus reaches the Sparks from here", ""]
        for node in nodes:
            alias, host = str(node.get("ssh") or "").strip(), str(node.get("host") or "").strip()
            if alias and host and " " not in alias:
                lines += [f"Host {alias}", f"  Hostname {host}", ""]
        lines += ["Host *", f"  User {SPARK_HOME.rsplit('/', 1)[-1]}", f"  IdentityFile {SPARK_KEY.replace('~', SPARK_HOME)}",
                  "  IdentitiesOnly yes", ""]
        return "\n".join(lines)

    # ================================================================================= data
    def _excludes_file(self, excludes: list[str]) -> str:
        path = f"{STACK}/excludes.json"
        self._put_text(path, json.dumps(excludes))
        return path

    def _push_dir(self, local: Path, remote: str, excludes: list[str]) -> dict[str, int]:
        mine = pr.manifest(str(local), excludes)
        if not mine:
            raise PlacementError(f"{local} está vacío en el PC; no lo copio")
        theirs = self._remote(f"manifest {remote} {self._excludes_file(excludes)}") or {}
        changed, deleted = pr.diff(mine, theirs)
        big = [r for r in changed if r in theirs and mine[r][0] >= pr.BIG_FILE and theirs[r][0] >= pr.BIG_FILE]
        sent = sum(self._push_blocks(local / r, f"{remote}/{shlex.quote(r)}", mine[r]) for r in big)
        changed = [r for r in changed if r not in big]
        self._put_text(f"{STACK}/deleted.json", json.dumps(deleted))
        proc = self._stream(f"python3 {REMOTE_HELPER} apply {remote} - {STACK}/deleted.json")
        try:
            pr.pack_to(str(local), changed, proc.stdin)
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            raise PlacementError(f"copiando {local.name}: {exc}") from exc
        out, err = proc.communicate(timeout=3600)
        if proc.returncode != 0:
            raise PlacementError(f"copiando {local.name}: {err.decode(errors='replace').strip()[-300:]}")
        return {"copied": len(changed) + len(big), "removed": len(deleted), "bytes": sum(mine[r][0] for r in changed) + sent}

    def _push_blocks(self, local: Path, remote_file: str, meta: list[int]) -> int:
        """Send only the 4 MB blocks of a big file that differ from the Spark's copy. Returns the bytes sent."""
        theirs = self._remote(f"hashes {remote_file}") or []
        indices = pr.changed_blocks(pr.block_hashes(str(local)), theirs)
        proc = self._stream(f"python3 {REMOTE_HELPER} writeblocks {remote_file} {meta[0]} {meta[1]}")
        try:
            sent = pr.send_blocks(str(local), indices, proc.stdin)
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            raise PlacementError(f"copiando {local.name}: {exc}") from exc
        _out, err = proc.communicate(timeout=3600)
        if proc.returncode != 0:
            raise PlacementError(f"copiando {local.name}: {err.decode(errors='replace').strip()[-300:]}")
        return sent

    def _pull_blocks(self, local: Path, remote_file: str, meta: list[int]) -> int:
        """Bring only the blocks of a big file that differ from this PC's copy. Returns the bytes received."""
        theirs = self._remote(f"hashes {remote_file}") or []
        indices = pr.changed_blocks(theirs, pr.block_hashes(str(local)))
        self._put_text(f"{STACK}/blocks.json", json.dumps(indices))
        proc = subprocess.Popen(["ssh", *SSH_OPTS, self._node(), f"python3 {REMOTE_HELPER} readblocks {remote_file} {STACK}/blocks.json"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_flags())
        try:
            got = pr.receive_blocks(str(local), proc.stdout, meta[0], meta[1])
        finally:
            _out, err = proc.communicate(timeout=3600)
        if proc.returncode != 0:
            raise PlacementError(f"trayendo {local.name}: {err.decode(errors='replace').strip()[-300:]}")
        return got

    def _pull_dir(self, name: str, local: Path, remote: str, excludes: list[str], trash: Path) -> dict[str, int]:
        theirs = self._remote(f"manifest {remote} {self._excludes_file(excludes)}") or {}
        mine = pr.manifest(str(local), excludes)
        if mine and len(theirs) < max(3, len(mine) // 3):
            raise PlacementError(f"en la Spark solo hay {len(theirs)} ficheros de {name} (aquí {len(mine)}); no traigo nada para no perder datos")
        changed, deleted = pr.diff(theirs, mine)
        big = [r for r in changed if r in mine and mine[r][0] >= pr.BIG_FILE and theirs[r][0] >= pr.BIG_FILE]
        got = sum(self._pull_blocks(local / r, f"{remote}/{shlex.quote(r)}", theirs[r]) for r in big)
        changed = [r for r in changed if r not in big]
        self._put_text(f"{STACK}/files.json", json.dumps(changed))
        proc = subprocess.Popen(["ssh", *SSH_OPTS, self._node(), f"python3 {REMOTE_HELPER} pack {remote} {STACK}/files.json -"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_flags())
        try:
            res = pr.apply_from(str(local), proc.stdout, deleted, str(trash / name))
        finally:
            _out, err = proc.communicate(timeout=3600)
        if proc.returncode != 0:
            raise PlacementError(f"trayendo {name}: {err.decode(errors='replace').strip()[-300:]}")
        return {"copied": len(changed) + len(big), "removed": len(deleted), "bytes": sum(theirs[r][0] for r in changed) + got, **res}

    def _pairs(self, dirs: dict[str, Path]) -> list[tuple[str, Path, str, list[str]]]:
        pairs = []
        for app_id in APPS:
            sub, excludes = DATA[app_id]
            pairs.append((app_id, dirs[app_id] / sub, f"{STACK}/{app_id}/{sub}", COMMON_EXCLUDES + excludes))
        if self.recipes_pc.is_dir():
            pairs.append(("recetas", self.recipes_pc, f"{STACK}/recipes", list(COMMON_EXCLUDES)))
        return pairs

    # ================================================================================= settings that hold paths
    @staticmethod
    def _read_local_paths(dirs: dict[str, Path]) -> dict[str, Any]:
        import sqlite3
        saved: dict[str, Any] = {"hermes": {}, "prometheus": {}}
        for db, key in HERMES_PATH_KEYS:
            path = dirs["hermes"] / db
            value = None
            if path.is_file():
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                    value = row[0] if row else None
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
            if value and key.endswith("_dir") and not Path(value).is_dir():
                value = None                     # a folder that no longer exists here: come back to "not set" (auto)
            saved["hermes"][f"{db}|{key}"] = value
        try:
            data = json.loads((dirs["prometheus"] / "data" / "settings.json").read_text(encoding="utf-8"))
            saved["prometheus"] = {k: data.get(k) for k in PROMETHEUS_PATH_KEYS}
        except (OSError, ValueError):
            pass
        return saved

    @staticmethod
    def _patches(dirs: dict[str, Path], side: str, saved: dict[str, Any]) -> dict[str, Any]:
        """The settings to write on ``side`` ("sparks": Spark paths; "pc": the values saved before leaving)."""
        out: dict[str, Any] = {"sqlite": [], "json": []}
        for db, key, there in HERMES_KEYS:
            if side == "sparks":
                out["sqlite"].append({"db": f"{SPARK_STACK}/hermes/{db}", "table": "settings", "key": key,
                                      "value": there.replace("{faustus}", f"{SPARK_STACK}/faustus")})
            elif f"{db}|{key}" in (saved.get("hermes") or {}):
                out["sqlite"].append({"db": str(dirs["hermes"] / db), "table": "settings", "key": key, "value": saved["hermes"][f"{db}|{key}"]})
        prom = "data/settings.json"
        if side == "sparks":
            out["json"].append({"file": f"{SPARK_STACK}/prometheus/{prom}", "path": ["recipes_dir"], "value": f"{SPARK_STACK}/recipes"})
        else:
            for key in PROMETHEUS_PATH_KEYS:
                if key in (saved.get("prometheus") or {}):
                    out["json"].append({"file": str(dirs["prometheus"] / prom), "path": [key], "value": saved["prometheus"][key]})
        return out

    def _remote_patch(self, spec: dict[str, Any]) -> None:
        self._put_text(f"{STACK}/patch.json", json.dumps(spec))
        self._log("ajustes de la Spark: " + ", ".join(self._remote(f"patch {STACK}/patch.json") or []))

    # ================================================================================= markers the apps' own launchers read
    @staticmethod
    def _markers(dirs: dict[str, Path], where: Optional[dict[str, Any]]) -> None:
        paths = [dirs["faustus"] / "data" / "runtime" / "placement.json",
                 dirs["hermes"] / "data" / MARKER, dirs["prometheus"] / "data" / MARKER]
        for marker in paths:
            if where is None:
                marker.unlink(missing_ok=True)
            else:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(where, ensure_ascii=False), encoding="utf-8")

    # ================================================================================= notices
    def _mail(self, subject: str, text: str, *, on_spark: bool) -> None:
        payload = json.dumps({"action": "send", "subject": subject, "text": text, "from_name": "Hoard Hub"})
        try:
            if on_spark:
                out = self._ssh(f"cd {STACK}/faustus && venv/bin/python {STACK}/mail_helper.py {STACK}/faustus", input=payload, timeout=90)
            else:
                root, python = self.hub.faustus_runtime.installation()
                helper = Path(__file__).resolve().parent.parent / "mail_helper.py"
                r = subprocess.run([str(python), str(helper), str(root)], input=payload, capture_output=True, text=True,
                                   timeout=90, cwd=str(root), creationflags=_flags())
                out = r.stdout
            answer = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            self._log("aviso por correo enviado" if answer.get("ok") else f"no he podido enviar el aviso: {answer.get('error')}")
        except Exception as exc:  # noqa: BLE001 - a notice never breaks the switch
            self._log(f"no he podido enviar el aviso: {exc}")

    # ================================================================================= the switch
    def switch(self, to: str) -> dict[str, Any]:
        if to not in ("pc", "sparks"):
            return {"ok": False, "error": "destino desconocido (pc | sparks)"}
        with self._lock:
            data = self.state()
            if (data.get("job") or {}).get("running"):
                return {"ok": False, "error": "ya hay un traslado en marcha", "job": data["job"]}
            if data["location"] == to == "pc" and (data.get("job") or {}).get("ok", True):
                return {"ok": True, "already": True, "location": to}
            # Already on the Spark: "move there" again brings the PC's newer code (and dependencies) over and restarts
            # only the apps whose code changed. Their data stays where it is.
            data["job"] = {"running": True, "to": to, "started": time.time(), "step": "preparando", "log": [], "error": ""}
            self._save(data)
        self._thread = threading.Thread(target=self._run_switch, args=(to,), name=THREAD_NAME, daemon=True)
        self._thread.start()
        return {"ok": True, "started": True, "to": to}

    def _run_switch(self, to: str) -> None:
        try:
            if to == "sparks" and self.state()["location"] == "sparks":
                self._refresh_sparks()
            else:
                (self._to_sparks if to == "sparks" else self._to_pc)()
            self._finish(ok=True)
        except Exception as exc:  # noqa: BLE001 - reported in the job; the side that was running is restarted
            self._log(f"ERROR: {exc}")
            self._finish(ok=False, error=str(exc)[:500])
        try:
            self.hub.invalidate_snapshot()
            self.hub.emit("hub.placement.changed", {"location": self.state()["location"], "ok": self.state()["job"].get("ok")})
        except Exception:  # noqa: BLE001
            pass

    def _finish(self, ok: bool, error: str = "") -> None:
        with self._lock:
            data = self.state()
            job = data.get("job") or {}
            job.update(running=False, ok=ok, error=error, finished=time.time(), step="hecho" if ok else "error")
            data["job"] = job
            self._save(data)

    def _stop_local(self) -> None:
        self._log("parando Faustus, Hermes y Prometheus en el PC", step="parando en el PC")
        for app_id in ("hermes", "prometheus"):
            res = self.hub.stop(app_id)
            if not res.get("ok"):
                self._log(f"{app_id}: {res.get('error') or res.get('detail')}")
        res = self.hub.faustus_stop()
        if not res.get("ok"):
            raise PlacementError(f"no he podido parar Faustus en el PC: {res.get('error')}")
        time.sleep(2)
        still = [a for a in ("hermes", "prometheus") if self.hub.get(a) is not None and self._listening(PORTS[a])]
        if self._listening(PORTS["faustus"]):
            still.append("faustus")
        if still:
            raise PlacementError("siguen en marcha en el PC: " + ", ".join(still))

    @staticmethod
    def _listening(port: int) -> bool:
        import socket
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _start_local(self) -> None:
        self._log("arrancando Faustus, Hermes y Prometheus en el PC", step="arrancando en el PC")
        res = self.hub.faustus_start()
        if not res.get("ok"):
            self._log(f"Faustus: {res.get('error')}")
        for app_id in ("prometheus", "hermes"):
            res = self.hub.start(app_id, wait=True)
            if not res.get("ok"):
                self._log(f"{app_id}: {res.get('error')}")

    def _check_node(self) -> None:
        free = self._ssh("awk '/MemAvailable/ {print $2}' /proc/meminfo").strip()
        gb = int(free or 0) / 1024 / 1024
        self._log(f"{self.state()['node']['name']}: {gb:.1f} GB de memoria libre")
        if gb < MIN_FREE_GB:
            raise PlacementError(f"la Spark solo tiene {gb:.1f} GB libres (hacen falta {MIN_FREE_GB:.0f}); no muevo nada")

    def _wait_remote_health(self, timeout: float = 300) -> dict[str, bool]:
        deadline = time.time() + timeout
        ok = {a: False for a in APPS}
        while time.time() < deadline and not all(ok.values()):
            for app_id, port in PORTS.items():
                if not ok[app_id]:
                    code = self._ssh(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{port}{HEALTH[app_id]} || true").strip()
                    ok[app_id] = code.startswith(("2", "3")) or code in ("401", "403")
            if not all(ok.values()):
                time.sleep(5)
        return ok

    def _to_sparks(self) -> None:
        dirs = self.local_dirs()
        self._upload_helper()
        self._check_node()
        deployed = dict(self.state().get("deployed") or {})
        self._log("preparando la Spark", step="preparando la Spark")
        self._ensure_node()
        for app_id in APPS:                                      # code and venvs first: the PC keeps running meanwhile
            self._deploy_code(app_id, dirs[app_id], deployed)
            self._ensure_runtime(app_id, dirs[app_id], deployed)
            self._update(deployed=deployed)
        saved = self._read_local_paths(dirs)
        self._update(saved_paths=saved)
        self._stop_local()
        try:
            for name, local, remote, excludes in self._pairs(dirs):
                self._log(f"copiando datos de {name} a la Spark", step=f"datos de {name}")
                res = self._push_dir(local, remote, excludes)
                self._log(f"{name}: {res['copied']} ficheros ({res['bytes'] / 1e6:.0f} MB), {res['removed']} borrados")
            self._remote_patch(self._patches(dirs, "sparks", saved))
            self._log("arrancando en la Spark", step="arrancando en la Spark")
            self._remote(f"svc enable {' '.join(APPS)}")
            self._remote(f"svc restart {' '.join(APPS)}")
            health = self._wait_remote_health()
            if not all(health.values()):
                raise PlacementError("no responden en la Spark: " + ", ".join(a for a, v in health.items() if not v))
        except Exception:
            self._log("vuelvo a arrancar en el PC: el traslado no terminó")
            try:
                self._remote(f"svc stop {' '.join(APPS)}")
                self._remote(f"svc disable {' '.join(APPS)}")
            except Exception:  # noqa: BLE001
                pass
            self._start_local()
            raise
        urls = self._tailnet_urls()
        where = {"location": "sparks", "node": self.state()["node"]["name"], "since": time.time(), "urls": urls}
        self._markers(dirs, where)
        self._update(location="sparks", since=time.time(), urls=urls)
        link = urls.get("faustus") or "(Tailscale aún no está activo en la Spark)"
        self._mail("Faustus, Hermes y Prometheus corren ahora en " + self.state()["node"]["name"],
                   "Ya puedes apagar el PC: los tres siguen funcionando en la Spark.\n\n"
                   f"Faustus: {link}\nHermes: {urls.get('hermes', '-')}\nPrometheus: {urls.get('prometheus', '-')}\n\n"
                   "En pausa hasta que vuelvan al PC:\n- " + "\n- ".join(PAUSED_ON_SPARK) +
                   "\n\nPara volver: Hoard Hub, Ejecutar en: PC.", on_spark=True)
        self._log("listo: Faustus, Hermes y Prometheus corren en la Spark; ya puedes apagar el PC")

    def _refresh_sparks(self) -> None:
        """On the Spark already: send the code that changed on the PC since, install new dependencies, restart those apps."""
        dirs = self.local_dirs()
        self._upload_helper()
        deployed = dict(self.state().get("deployed") or {})
        before = dict(deployed)
        units_changed = self._ensure_node()
        changed = []
        for app_id in APPS:
            self._deploy_code(app_id, dirs[app_id], deployed)
            self._ensure_runtime(app_id, dirs[app_id], deployed)
            if (deployed.get(app_id) != before.get(app_id) or app_id in units_changed
                    or deployed.get(app_id + ":requirements") != before.get(app_id + ":requirements")):
                changed.append(app_id)
            self._update(deployed=deployed)
        # Settings the Spark needs may have grown since the move (a newer Hub): write them again; Hermes reads them at start.
        saved = self.state().get("saved_paths") or {}
        self._remote_patch(self._patches(dirs, "sparks", saved))
        if "hermes" not in changed:
            changed.append("hermes")
        if changed:
            self._log("reiniciando en la Spark: " + ", ".join(changed), step="reiniciando en la Spark")
            self._remote(f"svc restart {' '.join(changed)}")
            health = self._wait_remote_health()
            if not all(health.values()):
                raise PlacementError("no responden en la Spark: " + ", ".join(a for a, v in health.items() if not v))
        urls = self._tailnet_urls() or self.state().get("urls") or {}
        self._update(urls=urls)
        self._log("listo: el código de la Spark está al día" + ("" if changed else " (no había cambios)"))

    def _to_pc(self) -> None:
        dirs = self.local_dirs()
        self._upload_helper()
        self._log("parando en la Spark", step="parando en la Spark")
        self._remote(f"svc stop {' '.join(APPS)}")
        self._remote(f"svc disable {' '.join(APPS)}")
        trash = self.trash_root / time.strftime("%Y%m%d-%H%M%S")
        try:
            for name, local, remote, excludes in self._pairs(dirs):
                self._log(f"trayendo los datos de {name}", step=f"datos de {name}")
                res = self._pull_dir(name, local, remote, excludes, trash)
                self._log(f"{name}: {res['copied']} ficheros ({res['bytes'] / 1e6:.0f} MB), {res['removed']} apartados"
                          + (f"; sin permiso para tocar {res['skipped']}: {', '.join(res['skipped_files'][:5])}" if res.get("skipped") else ""))
        except Exception:
            self._log("la Spark sigue siendo la buena: vuelvo a arrancarlos allí")
            self._remote(f"svc enable {' '.join(APPS)}")
            self._remote(f"svc start {' '.join(APPS)}")
            raise
        pr.patch(self._patches(dirs, "pc", self.state().get("saved_paths") or {}))
        self._markers(dirs, None)
        self._update(location="pc", since=time.time(), urls={})
        self._start_local()
        self._prune_trash()
        self._mail("Faustus, Hermes y Prometheus vuelven a correr en el PC",
                   "Todo lo que pausé mientras corrían en la Spark vuelve a funcionar.", on_spark=False)
        self._log("listo: vuelven a correr en el PC")

    def _prune_trash(self, keep: int = 3) -> None:
        if not self.trash_root.is_dir():
            return
        for old in sorted(p for p in self.trash_root.iterdir() if p.is_dir())[:-keep]:
            shutil.rmtree(old, ignore_errors=True)

    # ================================================================================= remote access
    def _tailnet_urls(self) -> dict[str, str]:
        """Publish the three apps on the Spark's tailnet name (HTTPS, only for the person's own devices) and return the
        links; {} while the Spark is not logged in to Tailscale yet."""
        try:
            raw = self._ssh("tailscale status --json 2>/dev/null || true", timeout=30)
            status = json.loads(raw) if raw.strip() else {}
        except (PlacementError, ValueError):
            return {}
        dns = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
        if status.get("BackendState") != "Running" or not dns:
            return {}
        for https, port in ((443, PORTS["faustus"]), (PORTS["hermes"], PORTS["hermes"]), (PORTS["prometheus"], PORTS["prometheus"])):
            self._ssh(f"sudo -n tailscale serve --bg --https={https} http://127.0.0.1:{port} >/dev/null 2>&1 || true", timeout=60)
        return {"faustus": f"https://{dns}", "hermes": f"https://{dns}:{PORTS['hermes']}/products/",
                "prometheus": f"https://{dns}:{PORTS['prometheus']}/"}

    def _refresh_urls_later(self) -> None:
        """On the Spark without links yet (Tailscale was approved after the move): look again, at most once a minute."""
        now = time.time()
        if now - getattr(self, "_urls_tried", 0.0) < 60:
            return
        self._urls_tried = now

        def run() -> None:
            urls = self._tailnet_urls()
            if urls and self.state()["location"] == "sparks":
                self._update(urls=urls)
        threading.Thread(target=run, name="placement-urls", daemon=True).start()
