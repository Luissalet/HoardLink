"""Backend for the independent Agora team console.

The facet keeps provider preferences and child-worker receipts in the Hub's
data directory. Group messages remain ordinary Agora messages in the canonical
work thread; mentions enqueue attention for the existing agents and never
pretend that a queued message woke or received a reply from them.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from .facets import Facet, Reply, Request

logger = logging.getLogger("hoard_hub.agora_console")
THREAD_ID = 32
FAUSTUS_AGORA_SESSION = "cc16a8f3-7eaa-44b1-a427-21f2bfb1ae95"
LEADERS = (
    ("codex", "Codex", "codex-investiga"),
    ("claude", "Claude", "claude"),
    ("cursor", "Cursor", "cursor"),
    ("faustus", "Faustus", "faustus"),
)
PROBE_TTL_S = 60.0
MAX_TASK_CHARS = 12000


class AgoraConsoleFacet(Facet):
    id = "agora_console"

    def __init__(self, hub: Any):
        super().__init__(hub)
        data_dir = Path(hub.config.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = data_dir / "agora-console.db"
        self._db_lock = threading.RLock()
        self._provider_lock = threading.Lock()
        self._provider_at = 0.0
        self._provider_cache: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._leader_locks = {"codex": threading.Lock(), "cursor": threading.Lock()}
        self._leader_threads: dict[str, threading.Thread] = {}
        self._init_db()
        self._reconcile_workers()
        self._start_queued_leaders()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _codex_executable() -> Optional[str]:
        found = shutil.which("codex")
        if found:
            return found
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        install_root = local_app_data / "OpenAI" / "Codex" / "bin"
        try:
            installed = list(install_root.glob("*/codex.exe"))
        except OSError:
            installed = []
        if not installed:
            return None
        return str(max(installed, key=lambda path: path.stat().st_mtime))

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS preferences (
                    leader_id TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT NOT NULL,
                    updated_by TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                    task TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '', pid INTEGER, log_path TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '', model_source TEXT NOT NULL DEFAULT '',
                    process_exit_code INTEGER, outcome TEXT NOT NULL DEFAULT 'unverified',
                    tool_success_count INTEGER NOT NULL DEFAULT 0, tool_failure_count INTEGER NOT NULL DEFAULT 0,
                    provider_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    state TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS console_activity_created ON activity(created_at DESC);
                CREATE TABLE IF NOT EXISTS leader_sessions (
                    leader_id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '', effort TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'idle', detail TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS leader_turns (
                    id TEXT PRIMARY KEY, leader_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    prompt TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '', process_exit_code INTEGER,
                    outcome TEXT NOT NULL DEFAULT 'unverified', tool_success_count INTEGER NOT NULL DEFAULT 0,
                    tool_failure_count INTEGER NOT NULL DEFAULT 0, provider_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS console_leader_turns_queue ON leader_turns(leader_id,state,created_at);
                CREATE TABLE IF NOT EXISTS native_preferences (
                    leader_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, model TEXT NOT NULL,
                    effort TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(workers)")}
            if "session_id" not in columns:
                db.execute("ALTER TABLE workers ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
            if "model_source" not in columns:
                db.execute("ALTER TABLE workers ADD COLUMN model_source TEXT NOT NULL DEFAULT ''")
            for name, declaration in (("process_exit_code", "INTEGER"), ("outcome", "TEXT NOT NULL DEFAULT 'unverified'"),
                                      ("tool_success_count", "INTEGER NOT NULL DEFAULT 0"), ("tool_failure_count", "INTEGER NOT NULL DEFAULT 0"),
                                      ("provider_error", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE workers ADD COLUMN {name} {declaration}")
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(leader_turns)")}
            for name, declaration in (("process_exit_code", "INTEGER"), ("outcome", "TEXT NOT NULL DEFAULT 'unverified'"),
                                      ("tool_success_count", "INTEGER NOT NULL DEFAULT 0"), ("tool_failure_count", "INTEGER NOT NULL DEFAULT 0"),
                                      ("provider_error", "TEXT NOT NULL DEFAULT ''")):
                if name not in turn_columns:
                    db.execute(f"ALTER TABLE leader_turns ADD COLUMN {name} {declaration}")

    def _record(self, kind: str, subject: str, state: str, detail: str = "") -> None:
        with self._db_lock, self._db() as db:
            db.execute("INSERT INTO activity(kind,subject,state,detail,created_at) VALUES(?,?,?,?,?)",
                       (kind, subject, state, detail[:1000], time.time()))

    @staticmethod
    def _safe_efforts(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        values = []
        for row in raw:
            value = row.get("effort") if isinstance(row, dict) else row
            if isinstance(value, str) and value and value not in values:
                values.append(value)
        return values

    def _codex_catalog(self) -> dict[str, Any]:
        executable = self._codex_executable()
        if not executable:
            return {"state": "unavailable", "detail": "Codex CLI is not on the Hub process PATH.", "catalog": []}
        try:
            result = subprocess.run([executable, "debug", "models"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                return {"state": "unavailable", "detail": f"Codex model catalog exited with {result.returncode}.", "catalog": []}
            payload = json.loads(result.stdout)
            rows = payload.get("models") if isinstance(payload, dict) else None
            catalog = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or not row.get("slug"):
                    continue
                if row.get("visibility") not in (None, "list"):
                    continue
                catalog.append({"id": str(row["slug"]), "name": str(row.get("display_name") or row["slug"]),
                                "efforts": self._safe_efforts(row.get("supported_reasoning_levels")),
                                "default_effort": row.get("default_reasoning_level")})
            current_model, current_effort = self._codex_current_settings()
            return {"state": "available" if catalog else "unavailable",
                    "detail": "Discovered from the installed Codex CLI model catalog." if catalog else "Codex CLI returned an empty model catalog.",
                    "catalog": catalog, "current_model": current_model, "current_effort": current_effort}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return {"state": "unavailable", "detail": f"Could not read Codex model catalog: {type(exc).__name__}.", "catalog": []}

    @staticmethod
    def _codex_current_settings() -> tuple[Optional[str], Optional[str]]:
        config_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        path = config_home / "config.toml"
        try:
            with path.open("rb") as stream:
                config = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            return None, None
        model = config.get("model") if isinstance(config, dict) else None
        effort = config.get("model_reasoning_effort") if isinstance(config, dict) else None
        return (model if isinstance(model, str) else None, effort if isinstance(effort, str) else None)

    def _claude_status(self) -> dict[str, Any]:
        executable = shutil.which("claude")
        if not executable:
            return {"state": "unavailable", "detail": "Claude Code CLI is not on the Hub process PATH.", "catalog": []}
        # `auth status` is useful diagnostic data, but an OAuth cache may be
        # expired while still being reported as logged in. Without a safe,
        # non-inference provider catalog/health endpoint, do not claim ready.
        return {"state": "unavailable", "detail": "Claude Code is installed, but its latest authenticated model probe returned HTTP 401 (OAuth access token expired). Refresh Claude authentication before using this provider.", "catalog": []}

    def _cursor_status(self) -> dict[str, Any]:
        runtime = Path(r"D:\LocalAI\cursor\runtime\dist-package")
        node, entry = runtime / "node.exe", runtime / "index.js"
        if not node.is_file() or not entry.is_file():
            return {"state": "unavailable", "detail": "The managed Cursor CLI runtime is not installed at its configured path; the existing managed session is left untouched.", "catalog": []}
        try:
            result = subprocess.run([str(node), str(entry), "models"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=12, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                return {"state": "unavailable", "detail": f"Cursor Agent model catalog exited with {result.returncode}.", "catalog": []}
            catalog = []
            for line in result.stdout.splitlines():
                if " - " not in line:
                    continue
                model_id, name = line.split(" - ", 1)
                model_id, name = model_id.strip(), name.strip()
                if model_id:
                    # Cursor publishes reasoning levels as model variants, e.g.
                    # gpt-5.6-luna-low. Keep the provider's exact selectable ID.
                    variant_id = model_id[:-5] if model_id.endswith("-fast") else model_id
                    level = next((x for x in ("minimal", "low", "medium", "high", "xhigh", "max")
                                  if variant_id.endswith("-" + x)), None)
                    catalog.append({"id": model_id, "name": name, "efforts": [level or "default"],
                                    "default_effort": level or "default", "effort_in_model_id": bool(level)})
            default = next((item for item in catalog if item["id"] == "auto"), None)
            return {"state": "available" if catalog else "unavailable",
                    "detail": "Discovered from the installed managed Cursor Agent `models` command; variants encode reasoning effort in the model ID.",
                    "catalog": catalog, "current_model": default.get("id") if default else None,
                    "current_effort": default.get("default_effort") if default else None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"state": "unavailable", "detail": f"Could not read Cursor Agent model catalog: {type(exc).__name__}.", "catalog": []}

    def _faustus_status(self) -> dict[str, Any]:
        app = self.hub.get("faustus") if hasattr(self.hub, "get") else None
        if app is None:
            return {"state": "unavailable", "detail": "Faustus is not registered in this Hub instance.", "catalog": []}
        try:
            from .contract import app_request
            status, health = app_request(app, "GET", "/api/health", timeout=1.5)
        except Exception as exc:  # noqa: BLE001
            return {"state": "unavailable", "detail": f"Faustus health probe failed: {type(exc).__name__}.", "catalog": []}
        if status != 200 or not isinstance(health, dict) or health.get("service") != "faustus":
            return {"state": "unavailable", "detail": "Faustus health endpoint did not confirm the expected service.", "catalog": []}
        status, models = app_request(app, "GET", "/api/models?background=false", timeout=35.0)
        if status != 200 or not isinstance(models, dict) or not isinstance(models.get("items"), list):
            return {"state": "unverified", "detail": f"Faustus is healthy, but its model catalog returned HTTP {status}.", "catalog": []}
        catalog: list[dict[str, Any]] = []
        for endpoint in models["items"]:
            if not isinstance(endpoint, dict):
                continue
            # The Agora Faustus leader targets its running Sparks GLM
            # endpoint. Keep PC-local model pools outside this catalog.
            if not str(endpoint.get("endpoint_name") or "").strip().lower().startswith("sparks"):
                continue
            endpoint_id = str(endpoint.get("endpoint_id") or "").strip()
            if not endpoint_id:
                continue
            endpoint_name = str(endpoint.get("endpoint_name") or endpoint_id)
            for model_name in [*(endpoint.get("models") or []), *(endpoint.get("models_extra") or [])]:
                model_name = str(model_name).strip()
                if not model_name:
                    continue
                query = urlencode({"endpoint_id": endpoint_id, "model": model_name})
                try:
                    effort_status, levels = app_request(app, "GET", f"/api/models/reasoning-levels?{query}", timeout=4.0)
                except Exception:  # noqa: BLE001
                    effort_status, levels = None, None
                efforts = levels.get("levels", []) if effort_status == 200 and isinstance(levels, dict) else []
                catalog.append({"id": f"{endpoint_id}::{model_name}", "model_id": model_name,
                                "endpoint_id": endpoint_id, "name": f"{endpoint_name}: {model_name}",
                                "efforts": [str(x) for x in efforts if isinstance(x, str)],
                                "default_effort": levels.get("default") if isinstance(levels, dict) else None,
                                "effort_source": levels.get("source") if isinstance(levels, dict) else None})
        return {"state": "available" if catalog else "unverified",
                "detail": "Discovered from Faustus /api/models and /api/models/reasoning-levels." if catalog else "Faustus is healthy, but no selectable models were returned.",
                "catalog": catalog}

    def _provider_data(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._provider_lock:
            if not force and self._provider_cache and now - self._provider_at < PROBE_TTL_S:
                return self._provider_cache
            providers = {
                "codex": self._codex_catalog(),
                "claude": self._claude_status(),
                "cursor": self._cursor_status(),
                "faustus": self._faustus_status(),
            }
            self._provider_cache, self._provider_at = providers, time.monotonic()
            return providers

    def _preferences(self) -> dict[str, dict[str, Any]]:
        with self._db_lock, self._db() as db:
            return {row["leader_id"]: {"model": row["model"], "effort": row["effort"],
                                        "updated_by": row["updated_by"], "updated_at": row["updated_at"]}
                    for row in db.execute("SELECT * FROM preferences")}

    def _worker_rows(self) -> list[dict[str, Any]]:
        with self._db_lock, self._db() as db:
            rows = db.execute("SELECT * FROM workers ORDER BY created_at DESC LIMIT 100").fetchall()
        return [{k: row[k] for k in ("id", "parent_id", "model", "effort", "task", "state", "result", "detail", "session_id", "model_source", "process_exit_code", "outcome", "tool_success_count", "tool_failure_count", "provider_error", "created_at", "updated_at")}
                for row in rows]

    def _console_sessions(self) -> list[dict[str, Any]]:
        with self._db_lock, self._db() as db:
            return [dict(row) for row in db.execute("SELECT leader_id,session_id,model,effort,state,detail,updated_at FROM leader_sessions")]

    def _reconcile_workers(self) -> None:
        with self._db_lock, self._db() as db:
            db.execute("UPDATE workers SET state='unknown', detail='Hub restarted; the child may still be running, but its completion cannot be observed here.', updated_at=? WHERE state IN ('starting','running','paused')",
                       (time.time(),))
            # A CLI request may have reached the provider before the Hub died.
            # Never replay it automatically: that could create duplicate paid work.
            db.execute("UPDATE leader_turns SET state='unknown', detail='Hub restarted during this provider turn; completion is ambiguous and it will not be retried automatically.', updated_at=? WHERE state='working'",
                       (time.time(),))
            db.execute("UPDATE leader_sessions SET state='unknown', detail='Hub restarted; session state needs reconciliation before another turn.', updated_at=? WHERE state='working'",
                       (time.time(),))

    def _start_queued_leaders(self) -> None:
        for leader_id in ("codex", "cursor"):
            with self._db_lock, self._db() as db:
                pending = db.execute("SELECT 1 FROM leader_turns WHERE leader_id=? AND state='queued' LIMIT 1", (leader_id,)).fetchone()
            if pending:
                self._ensure_leader_drain(leader_id)

    def _ensure_leader_drain(self, leader_id: str) -> None:
        current = self._leader_threads.get(leader_id)
        if current and current.is_alive():
            return
        thread = threading.Thread(target=self._drain_leader, args=(leader_id,), daemon=True,
                                  name=f"agora-leader-{leader_id}")
        self._leader_threads[leader_id] = thread
        thread.start()

    def _enqueue_leader_turn(self, leader_id: str, message_id: Any, body: str) -> dict[str, Any]:
        if leader_id not in ("codex", "cursor"):
            return {"leader_id": leader_id, "status": "queued" if leader_id == "faustus" else "unavailable",
                    "receipt_id": message_id, "detail": "Faustus is addressed through its native watcher; its current turn is left undisturbed." if leader_id == "faustus" else "Claude authentication is expired (HTTP 401); no provider turn was started."}
        provider = self._provider_data().get(leader_id) or {}
        saved = self._preferences().get(leader_id, {})
        model = str(saved.get("model") or provider.get("current_model") or "").strip()
        effort = str(saved.get("effort") or provider.get("current_effort") or "").strip().lower()
        selected = next((row for row in provider.get("catalog", []) if row.get("id") == model), None)
        effort = effort or str((selected or {}).get("default_effort") or "").strip().lower()
        executable = self._codex_executable() if leader_id == "codex" else None
        cursor_runtime = Path(r"D:\LocalAI\cursor\runtime\dist-package")
        available = bool(executable) if leader_id == "codex" else (cursor_runtime / "node.exe").is_file() and (cursor_runtime / "index.js").is_file()
        if not available or not selected or effort not in (selected.get("efforts") or []):
            return {"leader_id": leader_id, "status": "unavailable", "receipt_id": None,
                    "detail": provider.get("detail") or "No verified model and effort are configured for this independent leader session."}
        agora = self._agora()
        recent = agora.thread_recent({"thread_id": THREAD_ID, "limit": 40}) if agora is not None else {}
        messages = recent.get("messages", []) if isinstance(recent, dict) else []
        context = []
        for row in messages[-20:]:
            if isinstance(row, dict):
                author = str(row.get("author") or row.get("agent") or "unknown")[:80]
                text = str(row.get("body") or row.get("text") or "")[:4000]
                if text:
                    context.append(f"{author}: {text}")
        identity = "codex-agora" if leader_id == "codex" else "cursor-agora"
        prompt = ("You are the independent Agora console leader " + identity + ". Continue the shared work chat. "
                  "At the start of each turn, use `D:\\LocalAI\\agora.cmd --as " + identity + " hb \"...\"`, then read inbox and board. "
                  "Before editing, read `D:\\LocalAI\\CRITERIOS.md` first and `D:\\LocalAI\\MEMORY.md` second; also read the repo AGENTS.md. "
                  "Use this exact identity for all Agora operations; never impersonate Luis or another agent. "
                  "Do not claim that queued messages woke another agent. Answer the latest message with useful work or a concise status. "
                  "If you change files, follow the repo's skills and Agora protocol, work in an isolated worktree, create/claim the Agora task, "
                  "and take path locks first. Do not fake reviews or claim a task complete without evidence.\n\nRecent canonical thread 32:\n" + "\n".join(context))[-30000:]
        turn_id, now = str(uuid.uuid4()), time.time()
        with self._db_lock, self._db() as db:
            db.execute("INSERT INTO leader_turns(id,leader_id,message_id,prompt,model,effort,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
                       (turn_id, leader_id, str(message_id or ""), prompt, model, effort, now, now))
        self._record("leader_turn", turn_id, "queued", f"{leader_id}; {model}/{effort}; canonical message {message_id}")
        self._ensure_leader_drain(leader_id)
        return {"leader_id": leader_id, "status": "queued", "receipt_id": turn_id,
                "detail": f"Queued for the independent {leader_id.title()} console session ({model}/{effort}); reply will be posted only after a real CLI turn completes."}

    def _drain_leader(self, leader_id: str) -> None:
        with self._leader_locks[leader_id]:
            while True:
                with self._db_lock, self._db() as db:
                    session = db.execute("SELECT state,session_id FROM leader_sessions WHERE leader_id=?", (leader_id,)).fetchone()
                    if session and session["state"] == "unknown":
                        db.execute("UPDATE leader_turns SET detail='Blocked: the prior provider turn is ambiguous after Hub restart. Confirm that session is idle before continuing.',updated_at=? WHERE leader_id=? AND state='queued'",
                                   (time.time(), leader_id))
                        return
                    row = db.execute("SELECT * FROM leader_turns WHERE leader_id=? AND state='queued' ORDER BY created_at,id LIMIT 1", (leader_id,)).fetchone()
                    if row is None:
                        return
                    db.execute("UPDATE leader_turns SET state='working',updated_at=? WHERE id=? AND state='queued'", (time.time(), row["id"]))
                    db.execute("INSERT INTO leader_sessions(leader_id,state,detail,updated_at) VALUES(?,'working','Provider turn started',?) ON CONFLICT(leader_id) DO UPDATE SET state='working',detail='Provider turn started',updated_at=excluded.updated_at",
                               (leader_id, time.time()))
                self._run_leader_turn(dict(row))

    def _run_leader_turn(self, turn: dict[str, Any]) -> None:
        leader_id, turn_id = turn["leader_id"], turn["id"]
        work_dir = Path(__file__).resolve().parents[2]
        base = Path(self.hub.config.data_dir).resolve() / "agora-console" / "leaders" / leader_id
        base.mkdir(parents=True, exist_ok=True)
        log_path, result_path = base / f"{turn_id}.jsonl", base / f"{turn_id}.result.txt"
        with self._db_lock, self._db() as db:
            prior = db.execute("SELECT session_id FROM leader_sessions WHERE leader_id=?", (leader_id,)).fetchone()
        session_id = str(prior["session_id"] or "") if prior else ""
        if leader_id == "codex":
            executable = self._codex_executable()
            if not executable:
                self._finish_leader_turn(turn_id, leader_id, "failed", "Codex CLI is unavailable to the Hub process.", "", "")
                return
            if session_id:
                command = [executable, "exec", "resume", "--json", session_id, "--model", turn["model"],
                           "--config", f'model_reasoning_effort="{turn["effort"]}"',
                           "--config", 'approval_policy="never"', "--sandbox", "danger-full-access",
                           "--cd", str(work_dir), "--output-last-message", str(result_path), turn["prompt"]]
            else:
                command = [executable, "exec", "--json", "--model", turn["model"], "--config",
                           f'model_reasoning_effort="{turn["effort"]}"', "--config", 'approval_policy="never"',
                           "--sandbox", "danger-full-access", "--worktree",
                           "--cd", str(work_dir), "--output-last-message", str(result_path), turn["prompt"]]
        else:
            runtime = Path(r"D:\LocalAI\cursor\runtime\dist-package")
            node, entry = runtime / "node.exe", runtime / "index.js"
            if not node.is_file() or not entry.is_file():
                self._finish_leader_turn(turn_id, leader_id, "failed", "Managed Cursor CLI is unavailable.", "", "")
                return
            command = [str(node), str(entry), "--print", "--output-format", "stream-json", "--model", turn["model"],
                       "--workspace", str(work_dir)]
            if session_id:
                command.extend(["--resume", session_id])
            else:
                command.append("--worktree")
            command.append(turn["prompt"])
        observer_done = threading.Event()
        observer = threading.Thread(target=self._observe_leader_session, args=(turn_id, leader_id, log_path, observer_done),
                                    daemon=True, name=f"agora-session-observer-{leader_id}")
        observer.start()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        text=True, cwd=str(work_dir), timeout=1800, check=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            observer_done.set()
            observer.join(timeout=1)
            self._record_leader_session(turn_id, leader_id, log_path)
            found_session, stream_text = self._read_runner_receipt(log_path)
            answer = result_path.read_text(encoding="utf-8")[:20000] if result_path.is_file() else stream_text[:20000]
            metrics = self._runner_metrics(log_path)
            state = "completed" if result.returncode == 0 else "failed"
            outcome = "provider_error" if metrics["provider_error"] else ("missing_final_response" if not answer.strip() else "unverified")
            detail = (f"{leader_id.title()} provider turn ended with exit code {result.returncode}; task outcome is {outcome}." if state == "completed"
                      else f"{leader_id.title()} CLI exited with status {result.returncode}.")
            self._finish_leader_turn(turn_id, leader_id, state, detail, answer, found_session or session_id,
                                     process_exit_code=result.returncode, outcome=outcome, metrics=metrics)
        except (OSError, subprocess.TimeoutExpired) as exc:
            observer_done.set()
            self._finish_leader_turn(turn_id, leader_id, "failed", f"{leader_id.title()} turn failed: {type(exc).__name__}.", "", session_id)

    def _observe_leader_session(self, turn_id: str, leader_id: str, log_path: Path, done: threading.Event) -> None:
        while not done.wait(0.5):
            self._record_leader_session(turn_id, leader_id, log_path)

    def _record_leader_session(self, turn_id: str, leader_id: str, log_path: Path) -> None:
        session_id, _ = self._read_runner_receipt(log_path)
        if not session_id:
            return
        now = time.time()
        with self._db_lock, self._db() as db:
            turn = db.execute("SELECT model,effort FROM leader_turns WHERE id=?", (turn_id,)).fetchone()
            if not turn:
                return
            db.execute("UPDATE leader_turns SET session_id=?,updated_at=? WHERE id=? AND session_id=''",
                       (session_id, now, turn_id))
            db.execute("INSERT INTO leader_sessions(leader_id,session_id,model,effort,state,detail,updated_at) VALUES(?,?,?,?, 'working','Provider session observed from its runner receipt',?) ON CONFLICT(leader_id) DO UPDATE SET session_id=excluded.session_id,model=excluded.model,effort=excluded.effort,state='working',detail=excluded.detail,updated_at=excluded.updated_at",
                       (leader_id, session_id, turn["model"], turn["effort"], now))

    def _finish_leader_turn(self, turn_id: str, leader_id: str, state: str, detail: str, answer: str, session_id: str,
                            *, process_exit_code: Optional[int] = None, outcome: str = "unverified",
                            metrics: Optional[dict[str, Any]] = None) -> None:
        now = time.time()
        metrics = metrics or {"tool_success_count": 0, "tool_failure_count": 0, "provider_error": ""}
        with self._db_lock, self._db() as db:
            turn = db.execute("SELECT model,effort FROM leader_turns WHERE id=?", (turn_id,)).fetchone()
            model, effort = (turn["model"], turn["effort"]) if turn else ("", "")
            db.execute("UPDATE leader_turns SET state=?,result=?,detail=?,session_id=?,process_exit_code=?,outcome=?,tool_success_count=?,tool_failure_count=?,provider_error=?,updated_at=? WHERE id=?",
                       (state, answer[:20000], detail[:1000], session_id, process_exit_code, outcome,
                        metrics["tool_success_count"], metrics["tool_failure_count"], str(metrics["provider_error"] or "")[:1000], now, turn_id))
            db.execute("INSERT INTO leader_sessions(leader_id,session_id,model,effort,state,detail,updated_at) SELECT leader_id,?,?,?,?,?,? FROM leader_turns WHERE id=? ON CONFLICT(leader_id) DO UPDATE SET session_id=excluded.session_id,model=excluded.model,effort=excluded.effort,state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at",
                       (session_id, model, effort, "idle" if state == "completed" else state, detail[:1000], now, turn_id))
        if state == "completed" and answer.strip():
            agora = self._agora()
            if agora is not None:
                actor = "codex-agora" if leader_id == "codex" else "cursor-agora"
                try:
                    agora.post({"agent": actor, "thread_id": THREAD_ID, "body": answer[:20000], "kind": "comment", "mentions": []})
                except Exception:  # noqa: BLE001
                    logger.exception("could not post real %s console response", leader_id)
        self._record("leader_turn", turn_id, state, detail or f"{leader_id} real provider response recorded.")

    @staticmethod
    def _runner_metrics(path: Path) -> dict[str, Any]:
        successes = failures = 0
        provider_error = ""
        if not path.is_file():
            return {"tool_success_count": 0, "tool_failure_count": 0, "provider_error": ""}
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                kind = str(event.get("type") or event.get("event") or "").lower()
                item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
                item_kind = str(item.get("type") or item.get("kind") or "").lower()
                if "error" in kind or kind.endswith(".failed") or payload.get("is_error") is True:
                    provider_error = str(payload.get("message") or payload.get("error") or payload.get("text") or kind)[:1000]
                code = item.get("exit_code")
                state = str(item.get("status") or payload.get("status") or "").lower()
                is_tool = any(token in item_kind or token in kind for token in
                              ("tool_call", "tool_result", "command_execution", "tool_use"))
                if is_tool:
                    if code == 0 or state in ("success", "succeeded", "completed"):
                        successes += 1
                    elif code is not None or state in ("error", "failed", "failure") or payload.get("is_error") is True:
                        failures += 1
        except OSError:
            pass
        return {"tool_success_count": successes, "tool_failure_count": failures, "provider_error": provider_error}

    def overview(self, refresh: bool = False) -> dict[str, Any]:
        provider_data = self._provider_data(force=refresh)
        preferences = self._preferences()
        agents = {}
        agora = self._agora()
        if agora is not None:
            try:
                agents = {str(row.get("id")): row for row in agora.agents().get("agents", []) if isinstance(row, dict)}
            except Exception:  # noqa: BLE001
                agents = {}
        leaders = []
        for leader_id, name, agent_id in LEADERS:
            provider = provider_data[leader_id]
            pref = preferences.get(leader_id) or {}
            observed = agents.get(agent_id) or {}
            catalog = provider.get("catalog") or []
            model = pref.get("model") or provider.get("current_model")
            selected_effort = pref.get("effort")
            selected = next((row for row in catalog if row.get("id") == model), None)
            effort = selected_effort or provider.get("current_effort") or (selected or {}).get("default_effort")
            state = provider.get("state", "unavailable")
            if observed.get("state") == "working" and state in ("available", "unverified"):
                state = "working"
            leaders.append({"id": leader_id, "name": name, "state": state,
                            "detail": provider.get("detail", ""), "model": model, "effort": effort,
                            "catalog": catalog, "agent_id": agent_id,
                            "agent_state": observed.get("state", "unknown"),
                            "agent_detail": observed.get("doing") or observed.get("note") or "",
                            "console_session": next((row for row in self._console_sessions() if row["leader_id"] == leader_id), None)})
        usage = agora.usage() if agora is not None else {"ok": False, "providers": []}
        with self._db_lock, self._db() as db:
            activity = [dict(row) for row in db.execute("SELECT id,kind,subject,state,detail,created_at FROM activity ORDER BY id DESC LIMIT 40")]
            console_turns = [dict(row) for row in db.execute("SELECT id,leader_id,message_id,model,effort,state,result,detail,session_id,process_exit_code,outcome,tool_success_count,tool_failure_count,provider_error,created_at,updated_at FROM leader_turns ORDER BY created_at DESC LIMIT 40")]
            console_sessions = [dict(row) for row in db.execute("SELECT leader_id,session_id,model,effort,state,detail,updated_at FROM leader_sessions")]
            native_preferences = [dict(row) for row in db.execute("SELECT leader_id,session_id,model,effort,state,detail,updated_at FROM native_preferences")]
        messages = None
        if agora is not None:
            recent = agora.thread_recent({"thread_id": THREAD_ID, "limit": 40})
            if recent.get("ok"):
                messages = recent
        return {"ok": True, "thread_id": THREAD_ID, "leaders": leaders,
                "workers": self._worker_rows(), "quota_pools": usage.get("providers", []),
                "activity": activity, "messages": messages, "console_turns": console_turns,
                "console_sessions": console_sessions, "native_preferences": native_preferences,
                "checked_at": time.time()}

    def _agora(self) -> Any:
        facet = self.hub.facet("agora") if hasattr(self.hub, "facet") else None
        return getattr(facet, "agora", None)

    def set_preferences(self, args: dict[str, Any], actor: str = "codex-console") -> dict[str, Any]:
        leader_id = str(args.get("leader_id") or "").strip().lower()
        if leader_id not in {row[0] for row in LEADERS}:
            return {"ok": False, "status": 400, "error": "leader_id must be codex, claude, cursor or faustus"}
        model = str(args.get("model") or "").strip()
        effort = str(args.get("effort") or "").strip().lower()
        if not model or len(model) > 200 or len(effort) > 32:
            return {"ok": False, "status": 400, "error": "model is required and effort must be at most 32 characters"}
        providers = self._provider_data()
        catalog = providers[leader_id].get("catalog") or []
        selected = next((row for row in catalog if row.get("id") == model), None)
        if not effort and selected:
            effort = str(selected.get("default_effort") or "").strip().lower()
        verified = bool(selected and effort in selected.get("efforts", []))
        warning = None if verified else "Provider has not verified this model and effort; preference is recorded for the next compatible turn."
        applies_to = "next_console_leader_turn_and_child_worker_default" if leader_id in ("codex", "cursor") else "pending_native_session_control"
        if leader_id == "faustus":
            warning = (warning + " " if warning else "") + "Saved for a native per-session control API; no Faustus session or Primary endpoint was changed."
        elif leader_id == "claude":
            warning = (warning + " " if warning else "") + "Claude is currently unavailable because its OAuth token expired."
        now = time.time()
        with self._db_lock, self._db() as db:
            db.execute("INSERT INTO preferences(leader_id,model,effort,updated_by,updated_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(leader_id) DO UPDATE SET model=excluded.model,effort=excluded.effort,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                       (leader_id, model, effort, actor[:80], now))
            if leader_id == "faustus":
                db.execute("INSERT INTO native_preferences(leader_id,session_id,model,effort,state,detail,updated_at) VALUES(?,?,?,?, 'pending','',?) ON CONFLICT(leader_id) DO UPDATE SET session_id=excluded.session_id,model=excluded.model,effort=excluded.effort,state='pending',detail='',updated_at=excluded.updated_at",
                           (leader_id, FAUSTUS_AGORA_SESSION, model, effort, now))
        self._record("preference", leader_id, "saved", f"{model} · {effort}" + (f" · {warning}" if warning else ""))
        if leader_id == "faustus":
            applies_to = "pending_native_per_session_control"
        return {"ok": True, "leader_id": leader_id, "model": model, "effort": effort,
                "verified": verified, "applies_to": applies_to, "warning": warning, "updated_at": now}


    def send_message(self, args: dict[str, Any], actor: str = "codex-console") -> dict[str, Any]:
        body = str(args.get("body") or "").strip()
        if not body or len(body) > 20000:
            return {"ok": False, "status": 400, "error": "body must contain between 1 and 20000 characters"}
        raw_recipients = args.get("recipients")
        recipients = [str(x).strip().lower() for x in raw_recipients] if isinstance(raw_recipients, list) else [x[0] for x in LEADERS]
        leader_map = {row[0]: row[2] for row in LEADERS}
        if not recipients or any(x not in leader_map for x in recipients):
            return {"ok": False, "status": 400, "error": "recipients must be a non-empty list of codex, claude, cursor or faustus"}
        agora = self._agora()
        if agora is None:
            return {"ok": False, "status": 503, "error": "Agora is not available in this Hub instance"}
        known = {str(row.get("id")) for row in agora.agents().get("agents", []) if isinstance(row, dict)}
        mentions = list(dict.fromkeys(leader_map[x] for x in recipients if leader_map[x] in known))
        post = agora.post({"agent": actor, "_person": actor.lower() == "luis",
                           "thread_id": THREAD_ID, "body": body, "kind": "comment", "mentions": mentions})
        if not post.get("ok"):
            return post
        message_id = post.get("message_id")
        dispatch = []
        for leader_id in recipients:
            target = leader_map[leader_id]
            present = target in known
            console = self._enqueue_leader_turn(leader_id, message_id, body)
            if leader_id in ("codex", "cursor"):
                dispatch.append({**console, "agent_id": target,
                                 "legacy_mention": {"status": "queued" if present else "unavailable",
                                                    "receipt_id": message_id if present else None,
                                                    "detail": "Canonical mention waits for the existing agent's next turn." if present else "No matching existing agent identity is registered."}})
            elif leader_id == "faustus":
                dispatch.append({**console, "agent_id": target, "legacy_mention": {"status": "queued" if present else "unavailable",
                                                                                     "receipt_id": message_id if present else None,
                                                                                     "detail": "Native watcher handles this mention on its next turn." if present else "No matching existing agent identity is registered."}})
            else:
                dispatch.append({**console, "agent_id": target,
                                 "legacy_mention": {"status": "queued" if present else "unavailable",
                                                    "receipt_id": message_id if present else None,
                                                    "detail": "Canonical mention waits for Claude's next turn." if present else "No matching existing agent identity is registered."}})
        self._record("message", str(message_id or THREAD_ID), "queued", f"thread={THREAD_ID}; recipients={','.join(recipients)}")
        return {"ok": True, "thread_id": THREAD_ID, "message_id": message_id, "author": actor,
                "dispatch": dispatch, "delivery_semantics": "queued; a mention does not wake an idle agent immediately"}

    def _create_worker(self, args: dict[str, Any], actor: str = "codex-console") -> dict[str, Any]:
        leader_id = str(args.get("leader_id") or "").strip().lower()
        model = str(args.get("model") or "").strip()
        effort = str(args.get("effort") or "").strip().lower()
        task = str(args.get("task") or "").strip()
        if leader_id not in {row[0] for row in LEADERS} or not task or len(task) > MAX_TASK_CHARS:
            return {"ok": False, "status": 400, "error": f"leader_id and task (1-{MAX_TASK_CHARS} characters) are required"}
        worker_id = str(uuid.uuid4())
        now = time.time()
        provider = self._provider_data().get(leader_id) or {}
        saved = self._preferences().get(leader_id, {})
        model = model or str(saved.get("model") or provider.get("current_model") or "").strip()
        effort = effort or str(saved.get("effort") or provider.get("current_effort") or "").strip().lower()
        row = next((m for m in provider.get("catalog", []) if m.get("id") == model), None)
        executable = self._codex_executable() if leader_id == "codex" else None
        cursor_runtime = Path(r"D:\LocalAI\cursor\runtime\dist-package")
        cursor_runner = cursor_runtime / "node.exe"
        cursor_entry = cursor_runtime / "index.js"
        runner_available = bool(executable) if leader_id == "codex" else (
            leader_id == "cursor" and cursor_runner.is_file() and cursor_entry.is_file())
        if row and not effort:
            effort = str(row.get("default_effort") or "").strip().lower()
        if leader_id not in ("codex", "cursor") or not runner_available or not row or effort not in row.get("efforts", []):
            detail = provider.get("detail") or "This provider has no verified child-worker runner in the Hub."
            if leader_id in ("codex", "cursor") and not runner_available:
                detail = f"{leader_id.title()} child runner is unavailable to the Hub process."
            with self._db_lock, self._db() as db:
                db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,detail,created_at,updated_at) VALUES(?,?,?,?,?,'unavailable',?,?,?)",
                           (worker_id, leader_id, model, effort, task, detail, now, now))
            self._record("worker", worker_id, "unavailable", detail)
            return {"ok": True, "worker": next(w for w in self._worker_rows() if w["id"] == worker_id),
                    "warning": detail}
        work_dir = Path(__file__).resolve().parents[2]
        worker_dir = Path(self.hub.config.data_dir).resolve() / "agora-console" / "workers"
        worker_dir.mkdir(parents=True, exist_ok=True)
        log_path = worker_dir / f"{worker_id}.jsonl"
        result_path = worker_dir / f"{worker_id}.result.txt"
        prompt = ("You are an independent child worker for the Agora team. Complete the requested task in the "
                  "isolated worktree. Follow the repository's AGENTS.md and Agora task/lock protocol. Do not claim "
                  "results that are not supported by your work. Return a concise result and concrete evidence.\n\n"
                  f"Task: {task}")
        if leader_id == "codex":
            command = [executable, "exec", "--json", "--model", model, "--config", f'model_reasoning_effort="{effort}"',
                       "--config", 'approval_policy="never"', "--sandbox", "danger-full-access",
                       "--worktree", "--cd", str(work_dir),
                       "--output-last-message", str(result_path), prompt]
            model_source = "installed Codex CLI catalog + independent `codex exec --worktree` session"
        else:
            command = [str(cursor_runner), str(cursor_entry), "--print", "--output-format", "stream-json",
                       "--model", model, "--workspace", str(work_dir), "--worktree", prompt]
            model_source = "installed managed Cursor Agent `models` catalog + independent `--worktree` session"
        try:
            log = open(log_path, "a", encoding="utf-8")
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, cwd=str(work_dir), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.close()
        except OSError as exc:
            with self._db_lock, self._db() as db:
                db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,detail,created_at,updated_at) VALUES(?,?,?,?,?,'failed',?,?,?)",
                           (worker_id, leader_id, model, effort, task, f"Could not start Codex worker: {type(exc).__name__}.", now, time.time()))
            return {"ok": False, "status": 503, "error": f"Could not start Codex worker: {type(exc).__name__}"}
        self._processes[worker_id] = process
        with self._db_lock, self._db() as db:
            db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,pid,log_path,model_source,created_at,updated_at) VALUES(?,?,?,?,?,'running',?,?,?,?,?)",
                       (worker_id, leader_id, model, effort, task, process.pid, str(log_path), model_source, now, now))
        self._record("worker", worker_id, "running", f"{leader_id.title()} child process {process.pid}; model={model}; effort={effort}")
        threading.Thread(target=self._monitor_worker, args=(worker_id, process, result_path), daemon=True,
                         name=f"agora-worker-{worker_id[:8]}").start()
        return {"ok": True, "worker": next(w for w in self._worker_rows() if w["id"] == worker_id)}

    def _monitor_worker(self, worker_id: str, process: subprocess.Popen[str], result_path: Path) -> None:
        try:
            code = process.wait()
            session_id = ""
            try:
                log_path = self._worker_log_path(worker_id)
                session_id, stream_result = self._read_runner_receipt(log_path)
                result = result_path.read_text(encoding="utf-8")[:20000] if result_path.is_file() else stream_result[:20000]
            except OSError:
                result = ""
            metrics = self._runner_metrics(log_path)
            state = "completed" if code == 0 else "failed"
            outcome = "provider_error" if metrics["provider_error"] else ("missing_final_response" if not result.strip() else "unverified")
            detail = (f"Codex provider turn ended with exit code {code}; task outcome is {outcome}." if state == "completed"
                      else f"Codex worker exited with status {code}.")
            with self._db_lock, self._db() as db:
                db.execute("UPDATE workers SET state=?,result=?,detail=?,session_id=CASE WHEN ?='' THEN session_id ELSE ? END,process_exit_code=?,outcome=?,tool_success_count=?,tool_failure_count=?,provider_error=?,updated_at=? WHERE id=?",
                           (state, result, detail, session_id, session_id, code, outcome, metrics["tool_success_count"],
                            metrics["tool_failure_count"], metrics["provider_error"], time.time(), worker_id))
            self._record("worker", worker_id, state, detail or "Child worker finished.")
        except Exception:  # noqa: BLE001
            logger.exception("worker monitor failed for %s", worker_id)
        finally:
            self._processes.pop(worker_id, None)

    def _worker_log_path(self, worker_id: str) -> Path:
        with self._db_lock, self._db() as db:
            row = db.execute("SELECT log_path FROM workers WHERE id=?", (worker_id,)).fetchone()
        return Path(row["log_path"]) if row and row["log_path"] else Path()

    @staticmethod
    def _read_runner_receipt(path: Path) -> tuple[str, str]:
        session_id, final_text = "", ""
        if not path.is_file():
            return session_id, final_text
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                for key in ("thread_id", "session_id", "chat_id"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        session_id = value
                        break
                text = payload.get("result") or payload.get("text")
                if isinstance(text, str) and text:
                    final_text = text
        except OSError:
            pass
        return session_id, final_text

    def _stop_worker(self, worker_id: str) -> dict[str, Any]:
        process = self._processes.get(worker_id)
        if process is None:
            with self._db_lock, self._db() as db:
                row = db.execute("SELECT state FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                return {"ok": False, "status": 404, "error": "unknown worker"}
            return {"ok": False, "status": 409, "error": f"worker is {row['state']} or is not managed by this Hub process"}
        process.terminate()
        self._record("worker", worker_id, "stopping", "Stop requested by the Hub operator.")
        return {"ok": True, "worker_id": worker_id, "state": "stopping"}

    def worker(self, args: dict[str, Any], actor: str = "codex-console") -> dict[str, Any]:
        action = str(args.get("action") or "create").strip().lower()
        if action in ("create", "start"):
            return self._create_worker(args, actor)
        if action in ("register", "heartbeat", "finish"):
            return self._external_worker(args, action, actor)
        if action == "reconcile":
            leader_id = str(args.get("leader_id") or "").strip().lower()
            session_id = str(args.get("session_id") or "").strip()
            if leader_id not in ("codex", "cursor") or not session_id or args.get("confirm_idle") is not True:
                return {"ok": False, "status": 400, "error": "reconcile requires a Codex/Cursor leader, its exact session_id and confirm_idle=true"}
            with self._db_lock, self._db() as db:
                row = db.execute("SELECT session_id,state FROM leader_sessions WHERE leader_id=?", (leader_id,)).fetchone()
                if not row or row["session_id"] != session_id or row["state"] != "unknown":
                    return {"ok": False, "status": 409, "error": "leader session is not the matching ambiguous session"}
                db.execute("UPDATE leader_sessions SET state='idle',detail='Operator confirmed the exact provider session is idle; ambiguous turn remains unknown and will not be retried.',updated_at=? WHERE leader_id=?",
                           (time.time(), leader_id))
            self._record("leader_session", leader_id, "idle", "Operator confirmed the exact provider session is idle; previous ambiguous turn was not retried.")
            self._ensure_leader_drain(leader_id)
            return {"ok": True, "leader_id": leader_id, "session_id": session_id, "state": "idle",
                    "detail": "The ambiguous turn remains unknown; queued later turns may now continue."}
        if action in ("stop", "cancel"):
            return self._stop_worker(str(args.get("worker_id") or ""))
        return {"ok": False, "status": 400, "error": "action must be create, stop, register, heartbeat, finish or reconcile"}

    def _external_worker(self, args: dict[str, Any], action: str, actor: str) -> dict[str, Any]:
        worker_id = str(args.get("worker_id") or "").strip()
        now = time.time()
        if action == "register":
            parent_id = str(args.get("parent_id") or "").strip()
            leader_id = str(args.get("leader_id") or "").strip().lower()
            model = str(args.get("model") or "").strip()
            effort = str(args.get("effort") or "").strip().lower()
            task = str(args.get("task") or "").strip()
            session_id = str(args.get("session_id") or "").strip()
            model_source = str(args.get("model_source") or "").strip()
            if not all((worker_id, parent_id, leader_id, model, effort, task, session_id, model_source)):
                return {"ok": False, "status": 400, "error": "register requires worker_id, parent_id, leader_id, model, effort, task, session_id and model_source"}
            if leader_id not in {row[0] for row in LEADERS} or len(task) > MAX_TASK_CHARS:
                return {"ok": False, "status": 400, "error": "leader_id must be a known provider and task must fit the worker limit"}
            with self._db_lock, self._db() as db:
                existing = db.execute("SELECT id FROM workers WHERE id=?", (worker_id,)).fetchone()
                if existing:
                    db.execute("UPDATE workers SET parent_id=?,model=?,effort=?,task=?,state='running',detail='',session_id=?,model_source=?,updated_at=? WHERE id=?",
                               (parent_id, model, effort, task, session_id, model_source, now, worker_id))
                else:
                    db.execute("INSERT INTO workers(id,parent_id,model,effort,task,state,session_id,model_source,created_at,updated_at) VALUES(?,?,?,?,?,'running',?,?,?,?)",
                               (worker_id, parent_id, model, effort, task, session_id, model_source, now, now))
            self._record("worker", worker_id, "running", f"Registered by {actor}; parent={parent_id}; source={model_source}")
        else:
            if not worker_id:
                return {"ok": False, "status": 400, "error": "worker_id is required"}
            with self._db_lock, self._db() as db:
                row = db.execute("SELECT id FROM workers WHERE id=?", (worker_id,)).fetchone()
                if row is None:
                    return {"ok": False, "status": 404, "error": "worker must be registered before its lifecycle can be updated"}
                if action == "heartbeat":
                    state = "running"
                    db.execute("UPDATE workers SET state='running',detail='',updated_at=? WHERE id=?", (now, worker_id))
                else:
                    state = "completed"
                    result = str(args.get("result") or "")[:20000]
                    db.execute("UPDATE workers SET state='completed',result=?,detail='',updated_at=? WHERE id=?", (result, now, worker_id))
            self._record("worker", worker_id, state, f"Lifecycle {action} recorded by {actor}.")
        return {"ok": True, "worker": next((w for w in self._worker_rows() if w["id"] == worker_id), None)}

    @staticmethod
    def _operator(req: Request) -> bool:
        return req.caller() in ("hub", "ui") or req.agent()

    @staticmethod
    def _mcp_actor(args: dict[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
        actor = str(args.pop("actor", "codex-console") or "codex-console").strip().lower()
        if actor == "luis" or actor in {"hub", "ui", "system"} or not actor or len(actor) > 64 or not all(
                ch.isascii() and (ch.isalnum() or ch in "_-") for ch in actor):
            return "", {"ok": False, "status": 400, "error": "actor must be a valid agent identity; only the Hub page may write as luis"}
        return actor, None

    def get(self, req: Request) -> Optional[Any]:
        if req.method == "GET" and (req.path == "/agora" or req.path.startswith("/agora/")):
            return self._static(req.path)
        if not req.path.startswith("/api/agora/console/"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 401, "error": "use the Hub page or its bearer token"}
        if req.path == "/api/agora/console/overview":
            return self.overview(req.q_bool("refresh"))
        # Let AgoraFacet serve its existing /api/agora/console/usage endpoint.
        return None

    def post(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/agora/console/"):
            return None
        if not self._operator(req):
            return {"ok": False, "status": 401, "error": "use the Hub page or its bearer token"}
        args = dict(req.body or {})
        # Browser requests represent the signed-in operator. Agent/API callers
        # receive only their authenticated caller identity; body fields cannot
        # forge another author.
        caller = req.caller()
        known_agent_ids = {row[2] for row in LEADERS}
        caller_to_agent = {row[0]: row[2] for row in LEADERS}
        caller_text = str(caller or "")
        valid_caller = bool(caller_text) and len(caller_text) <= 64 and caller_text.isascii() and all(
            ch.isalnum() or ch in "_-" for ch in caller_text)
        actor = "luis" if caller == "ui" else (caller_to_agent.get(caller_text, caller_text) if req.agent() and valid_caller
                                                   else caller_to_agent.get(caller_text, caller_text if caller_text in known_agent_ids else "codex-console"))
        if req.path == "/api/agora/console/message":
            return self.send_message(args, actor)
        if req.path == "/api/agora/console/preferences":
            return self.set_preferences(args, actor)
        if req.path == "/api/agora/console/worker":
            return self.worker(args, actor)
        return {"ok": False, "status": 404, "error": f"unknown Agora console route {req.path}"}

    def _static(self, path: str) -> Any:
        root = self._static_root()
        if not root.is_dir():
            return Reply({"ok": False, "status": 503, "error": "Agora console frontend is not built",
                          "detail": "Build apps/agora and place its output in apps/agora/dist."}, status=503)
        relative = path.removeprefix("/agora/") if path.startswith("/agora/") else ""
        relative = relative or "index.html"
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return {"ok": False, "status": 404, "error": "Agora console asset not found"}
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        content = target.read_bytes()
        if target.name.lower() == "index.html":
            # The Hub strips trailing slashes before routing, so /agora itself
            # is the app root. Rewrite relative Vite output URLs at response
            # time to keep built assets under the same-origin /agora prefix.
            html = content.decode("utf-8", "replace")
            html = html.replace('href="./assets/', 'href="/agora/assets/').replace(
                'src="./assets/', 'src="/agora/assets/')
            html = html.replace('href="/assets/', 'href="/agora/assets/').replace(
                'src="/assets/', 'src="/agora/assets/')
            if "<head>" in html and "<base " not in html:
                html = html.replace("<head>", '<head><base href="/agora/">', 1)
            content = html.encode("utf-8")
        return Reply(body=content, content_type=content_type)

    @staticmethod
    def _static_root() -> Path:
        return (Path(__file__).resolve().parents[2] / "apps" / "agora" / "dist").resolve()

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {"name": "hub_agora_console_overview", "description": "Read the Agora team console: live provider catalogs where the official local interface exposes them, shared quota pools, child workers and canonical group messages. Catalogs may be unavailable; no model call is made.",
             "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean"}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_agora_console_message", "description": "Post one attributed message to canonical Agora work thread 32 and mention the selected existing leaders. Mentions queue attention; they do not imply that an idle agent woke or replied. MCP agents may supply their own valid actor id; only the Hub page may post as luis.",
             "inputSchema": {"type": "object", "properties": {"body": {"type": "string", "minLength": 1, "maxLength": 20000}, "recipients": {"type": "array", "items": {"type": "string", "enum": ["codex", "claude", "cursor", "faustus"]}}, "actor": {"type": "string", "maxLength": 64}}, "required": ["body"], "additionalProperties": False},
             "annotations": {"readOnlyHint": False}},
            {"name": "hub_agora_console_preferences", "description": "Save a verified provider model and reasoning effort; Codex/Cursor independent console leaders and child workers consume the saved preferences. Faustus and Claude limitations are reported explicitly.",
             "inputSchema": {"type": "object", "properties": {"leader_id": {"type": "string", "enum": ["codex", "claude", "cursor", "faustus"]}, "model": {"type": "string"}, "effort": {"type": "string"}, "actor": {"type": "string", "maxLength": 64}}, "required": ["leader_id", "model"], "additionalProperties": False},
             "annotations": {"readOnlyHint": False}},
            {"name": "hub_agora_console_worker", "description": "Start or stop a separate Codex or managed Cursor child in an isolated worktree; register external workers from real metadata, or reconcile an ambiguous console session after its operator confirms it is idle.",
             "inputSchema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["create", "stop", "register", "heartbeat", "finish", "reconcile"]},
                 "worker_id": {"type": "string"}, "parent_id": {"type": "string"},
                 "leader_id": {"type": "string", "enum": ["codex", "claude", "cursor", "faustus"]},
                 "model": {"type": "string"}, "effort": {"type": "string"},
                 "task": {"type": "string", "maxLength": MAX_TASK_CHARS}, "confirm_idle": {"type": "boolean"},
                 "session_id": {"type": "string"}, "model_source": {"type": "string"},
                 "result": {"type": "string", "maxLength": 20000}, "actor": {"type": "string", "maxLength": 64}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": False}},
        ]

    def handlers(self) -> dict[str, Any]:
        def post_message(args: dict[str, Any]) -> dict[str, Any]:
            values = dict(args)
            actor, error = self._mcp_actor(values)
            return error or self.send_message(values, actor)

        def post_preferences(args: dict[str, Any]) -> dict[str, Any]:
            values = dict(args)
            actor, error = self._mcp_actor(values)
            return error or self.set_preferences(values, actor)

        def post_worker(args: dict[str, Any]) -> dict[str, Any]:
            values = dict(args)
            actor, error = self._mcp_actor(values)
            return error or self.worker(values, actor)

        return {
            "hub_agora_console_overview": lambda a: self.overview(bool(a.get("refresh"))),
            "hub_agora_console_message": post_message,
            "hub_agora_console_preferences": post_preferences,
            "hub_agora_console_worker": post_worker,
        }
