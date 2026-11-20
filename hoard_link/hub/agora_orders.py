"""Production orders, pieces and the person's gate: how a request of the person becomes finished work.

* **Order** (``order_create``): what the person asks, in their words, with references to where it comes from (an app,
  an object and the exact place: a chapter, a card, a file), the deliverables wanted, an optional branch template,
  the details every piece must keep («the tube on her left») and a folder for its files. States: ``open``,
  ``producing``, ``waiting`` (for the person), ``delivered``, ``cancelled``. The person writes one from the hub's
  page; a Hoard or an agent sends it over the same route or tool, with ``refs`` pointing at the selection.
* **Plan** (``plan_set``): the jobs of the order. Each job is an ordinary Ágora task of the order's space (so claims,
  locks, dependencies, review and checkpoints work as they do); the order keeps beside it the station (Hoard and
  skill), the input pieces, the kinds of piece it must deliver, how its output is checked and who it is meant for
  (agent, model, machine). Parallel branches and sibling derivatives are just jobs that wait on the same one. The
  plan can be recomposed while the order runs: jobs are added, changed or removed (an open task is dropped).
* **Piece** (``piece_add``): a file of the order's folder, a web address or an object of an app with its place;
  who made it (job, station, agent, model); what it derives from (other pieces, and through them the person's
  decisions that fixed them); the details to keep, inherited from the order, which each check marks; checks
  (``piece_check``) and defects sent back to the station that caused them (``piece_defect``: view, pose, second,
  step). States: ``provisional``, ``checked``, ``kept``, ``cancelled``, ``stale``. A cancelled or stale piece makes
  stale every piece derived from it, without deleting anything.
* **Gate** (``gate_open``): a set of pieces shown to the person for one decision, keep or cancel, with an optional
  note (``gate_decide``). A piece can be split off to be decided alone (``gate_split``). Keeping fixes the piece as
  a reference for what comes next (``job`` hands it to every later job); cancelling sends it back to its station
  with the note as a defect, moves its task back to ``changes`` and makes its derivatives stale. Only the person's
  own pages decide a gate: no tool does. Every decision is stored with its situation (the order, its deadline, the
  other pieces that were on the table).

Every write an agent makes returns ``undo``: ``order_undo`` with it puts back what that call changed (tasks it created
are dropped). Pages and tools read the same operations (``orders``, ``order``, ``job``, ``order_decisions``).
``agora_channels`` folds these operations and tools into its own, so ``agora.py`` binds them like the rest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from typing import Any, Optional

from . import order_budget
from . import order_destinations, order_dossier

ORDER_STATES = ("open", "producing", "waiting", "delivered", "cancelled")
PIECE_STATES = ("provisional", "checked", "kept", "cancelled", "stale")
#: The usual kinds of piece. Any other short slug is accepted: the vocabulary grows with the work.
PIECE_KINDS = ("brief", "text", "image", "character_sheet", "video", "audio", "mesh", "rigged_model", "set", "render",
               "playable_scene", "figure", "document", "slides", "web", "code_branch", "feature", "report", "data")
DECISIONS = ("keep", "cancel")
#: How a piece used another: transformed it (material: cancelling the parent makes it stale) or only looked at it
#: (reference: it stays valid and says that its reference was cancelled).
ROLES = ("material", "reference")
#: What a check says of a detail to keep. not_visible is not a failure: the detail cannot be seen in this piece.
DETAIL_STATES = ("pending", "ok", "missing", "not_visible")
MAX_ITEMS = 200
#: A registered file is copied, by content, into the Agora's own folder so that each revision keeps its bytes even when
#: the station overwrites the file. Bigger files are not copied and are checked against their print instead.
STORE_MAX = 2 * 1024 ** 3
#: A local web page is kept with its folder (styles, scripts, images) up to this size.
SITE_MAX = 64 * 1024 ** 2
TEXT_MAX = 20000

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
MESH_EXT = {".glb", ".gltf", ".obj", ".stl", ".3mf", ".blend", ".fbx", ".ply", ".tscn"}
TEXT_EXT = {".md", ".txt", ".json", ".csv", ".log", ".py", ".js", ".css", ".yaml", ".yml"}

ORDERS_MIGRATION = """
    CREATE TABLE agora_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL, title TEXT NOT NULL,
        words TEXT NOT NULL DEFAULT '', refs TEXT NOT NULL DEFAULT '[]', deliverables TEXT NOT NULL DEFAULT '[]',
        template TEXT NOT NULL DEFAULT '', keep TEXT NOT NULL DEFAULT '[]', folder TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', deadline REAL, created_by TEXT NOT NULL, created REAL NOT NULL,
        updated REAL NOT NULL);
    CREATE INDEX agora_orders_space ON agora_orders(space_id, status, updated);
    CREATE TABLE agora_order_jobs (task_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, space_id TEXT NOT NULL,
        station TEXT NOT NULL DEFAULT '', skill TEXT NOT NULL DEFAULT '', inputs TEXT NOT NULL DEFAULT '[]',
        produces TEXT NOT NULL DEFAULT '[]', check_how TEXT NOT NULL DEFAULT '', assignee TEXT NOT NULL DEFAULT '{}',
        branch TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL);
    CREATE INDEX agora_order_jobs_order ON agora_order_jobs(order_id, position);
    CREATE TABLE agora_pieces (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, space_id TEXT NOT NULL,
        kind TEXT NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
        app TEXT NOT NULL DEFAULT '', object TEXT NOT NULL DEFAULT '', place TEXT NOT NULL DEFAULT '',
        previews TEXT NOT NULL DEFAULT '[]', task_id INTEGER, station TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', derives_from TEXT NOT NULL DEFAULT '[]',
        decisions TEXT NOT NULL DEFAULT '[]', keep TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'provisional', stale_because TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1, sha256 TEXT NOT NULL DEFAULT '', source_rev TEXT NOT NULL DEFAULT '',
        supersedes INTEGER, stored TEXT NOT NULL DEFAULT '', previews_stored TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX agora_pieces_order ON agora_pieces(order_id, id);
    CREATE INDEX agora_pieces_task ON agora_pieces(task_id);
    CREATE TABLE agora_piece_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, piece_id INTEGER NOT NULL,
        space_id TEXT NOT NULL, agent TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
        keep TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL);
    CREATE INDEX agora_piece_checks_piece ON agora_piece_checks(piece_id, id);
    CREATE TABLE agora_piece_defects (id INTEGER PRIMARY KEY AUTOINCREMENT, piece_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL, space_id TEXT NOT NULL, task_id INTEGER, station TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL, note TEXT NOT NULL, view TEXT NOT NULL DEFAULT '', pose TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL DEFAULT '', step TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'check',
        expected TEXT NOT NULL DEFAULT '', observed TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', closed_by TEXT, closed_in INTEGER, closed_note TEXT NOT NULL DEFAULT '',
        closed REAL, created REAL NOT NULL);
    CREATE INDEX agora_piece_defects_piece ON agora_piece_defects(piece_id, id);
    CREATE INDEX agora_piece_defects_task ON agora_piece_defects(task_id, status);
    CREATE TABLE agora_gates (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, space_id TEXT NOT NULL,
        title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
        parent_gate INTEGER, opened_by TEXT NOT NULL, created REAL NOT NULL, closed REAL);
    CREATE INDEX agora_gates_order ON agora_gates(order_id, status);
    CREATE TABLE agora_gate_pieces (gate_id INTEGER NOT NULL, piece_id INTEGER NOT NULL, position INTEGER NOT NULL,
        PRIMARY KEY(gate_id, piece_id));
    CREATE TABLE agora_order_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
        gate_id INTEGER NOT NULL, piece_id INTEGER NOT NULL, space_id TEXT NOT NULL, decision TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', situation TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
    CREATE INDEX agora_order_decisions_order ON agora_order_decisions(order_id, id);
    CREATE INDEX agora_order_decisions_piece ON agora_order_decisions(piece_id);
    CREATE TABLE agora_order_journal (token TEXT PRIMARY KEY, space_id TEXT NOT NULL, agent TEXT NOT NULL,
        op TEXT NOT NULL, changes TEXT NOT NULL, created REAL NOT NULL, undone REAL);
"""

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
#: Tables a journal entry may restore, with their key column.
_KEYS = {"agora_orders": "id", "agora_order_jobs": "task_id", "agora_pieces": "id", "agora_piece_checks": "id",
         "agora_piece_defects": "id", "agora_gates": "id", "agora_gate_pieces": None,
         "agora_order_budgets": "order_id", "agora_order_attempts": "id"}


def _ag() -> Any:
    from . import agora
    return agora


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return _ag().AgoraError(message, status, **extra)


def _txt(value: Any, limit: int, what: str, *, required: bool = False) -> str:
    return _ag()._txt(value, limit, what, required=required)


def _load(value: Any, default: Any) -> Any:
    try:
        out = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _int(value: Any, what: str, *, required: bool = True) -> Optional[int]:
    if value in (None, ""):
        if required:
            raise _err(f"{what} is required")
        return None
    if isinstance(value, bool):
        raise _err(f"{what} must be a number")
    try:
        out = int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        raise _err(f"{what} must be a number") from None
    if out < 1:
        raise _err(f"{what} must be a positive number")
    return out


def _ids(value: Any, what: str) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = str(value).replace(",", " ").split()
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise _err(f"{what} must be a list of at most {MAX_ITEMS} ids")
    out: list[int] = []
    for v in value:
        n = _int(v, what)
        if n not in out:
            out.append(n)
    return out


def _strs(value: Any, what: str, *, limit: int = 300, items: int = 60) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [v for v in value.split("\n")]
    if not isinstance(value, list) or len(value) > items:
        raise _err(f"{what} must be a list of at most {items} texts")
    out = []
    for v in value:
        text = _txt(v, limit, what)
        if text and text not in out:
            out.append(text)
    return out


def _kind(value: Any, what: str = "kind") -> str:
    kind = str(value or "").strip().lower().replace(" ", "_")
    if not _SLUG.match(kind):
        raise _err(f"{what} must be a short slug such as {', '.join(PIECE_KINDS[:6])}…")
    return kind


def _refs(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise _err("refs must be a list of at most 50 references {app, object, place, path, url, note}")
    out = []
    for ref in value:
        if isinstance(ref, str):
            ref = {"note": ref}
        if not isinstance(ref, dict):
            raise _err("each reference is an object {app, object, place, path, url, note}")
        clean = {k: _txt(ref.get(k), 1000, f"refs.{k}") for k in ("app", "object", "place", "path", "url", "note")}
        for key, limit in (("sha256", 64), ("source_rev", 200)):
            if ref.get(key):
                clean[key] = _txt(ref[key], limit, f"refs.{key}")
        clean = {k: v for k, v in clean.items() if v}
        if clean:
            out.append(order_dossier.ref_receipt(clean, _err))
    return out


def _assignee(value: Any) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = {"agent": value}
    if not isinstance(value, dict):
        raise _err("assignee is an object {agent, model, machine}")
    return {k: _txt(value.get(k), 120, f"assignee.{k}") for k in ("agent", "model", "machine") if value.get(k)}


def _derivation(value: Any) -> list[dict[str, Any]]:
    """``derives_from`` as given: ids (material) or ``{piece, role, note}`` with role material or reference."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        value = [{"piece": i} for i in _ids(value, "derives_from")]
    if len(value) > MAX_ITEMS:
        raise _err(f"derives_from holds at most {MAX_ITEMS} pieces")
    out: list[dict[str, Any]] = []
    for item in value:
        item = item if isinstance(item, dict) else {"piece": item}
        role = str(item.get("role") or "material").lower()
        if role not in ROLES:
            raise _err(f"derives_from role must be one of {', '.join(ROLES)}")
        pid = _int(item.get("piece") or item.get("piece_id"), "derives_from.piece")
        if all(d["piece"] != pid for d in out):
            out.append({"piece": pid, "role": role,
                        **({"note": _txt(item["note"], 300, "derives_from.note")} if item.get("note") else {})})
    return out


def _parents(raw: Any) -> list[dict[str, Any]]:
    """Stored derivation; a bare id is material."""
    return [d if isinstance(d, dict) else {"piece": int(d), "role": "material"} for d in _load(raw, [])]


def _sha256(path: str) -> str:
    """The content print of a file (empty when it is not there)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return ""


def _store_dir(self: Any) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(str(self.db.path))), "order_pieces")


def _keep_copy(self: Any, path: str, sha: str) -> str:
    """Copy a file into the content store (once per content); its stored path, or '' when it cannot be kept."""
    if not sha or not os.path.isfile(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm"):
        return _keep_site(self, path, sha)
    try:
        if os.path.getsize(path) > STORE_MAX:
            return ""
        dest = os.path.join(_store_dir(self), sha[:2], sha + ext)
        if not os.path.isfile(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            shutil.copyfile(path, tmp)
            if _sha256(tmp) != sha:     # changed while copying: do not keep a copy that is not this revision
                os.remove(tmp)
                return ""
            os.replace(tmp, dest)
        return dest
    except OSError:
        return ""


def _site_files(base: str) -> Optional[list[str]]:
    """The files of a web page's folder, or None when the folder is over SITE_MAX."""
    files, total = [], 0
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
        for name in sorted(names):
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
            if total > SITE_MAX:
                return None
            files.append(full)
    return files


def _print(path: str) -> str:
    """The content print of a piece's file. A local web page is printed with its folder (the entry's name, and each
    file's relative path and content), so a changed style or image makes another revision; a folder too big to keep
    is printed by its entry page alone."""
    if os.path.splitext(path)[1].lower() not in (".html", ".htm") or not os.path.isfile(path):
        return _sha256(path)
    base = os.path.dirname(path)
    files = _site_files(base)
    if files is None:
        return _sha256(path)
    h = hashlib.sha256(f"site:{os.path.basename(path)}|".encode("utf-8"))
    for full in files:
        rel = os.path.relpath(full, base).replace(os.sep, "/")
        h.update(f"{rel}|{_sha256(full)};".encode("utf-8"))
    return h.hexdigest()


def _keep_site(self: Any, path: str, sha: str) -> str:
    """A local web page with the files of its folder, under the print of the whole site."""
    base = os.path.dirname(path)
    files = _site_files(base)
    if files is None:
        return ""
    dest_root = os.path.join(_store_dir(self), "sites", sha)
    try:
        if not os.path.isdir(dest_root):
            tmp = dest_root + ".part"
            shutil.rmtree(tmp, ignore_errors=True)
            for full in files:
                target = os.path.join(tmp, os.path.relpath(full, base))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(full, target)
            if _print(os.path.join(tmp, os.path.basename(path))) != sha:   # changed while copying
                shutil.rmtree(tmp, ignore_errors=True)
                return ""
            os.replace(tmp, dest_root)
        return os.path.join(dest_root, os.path.basename(path))
    except OSError:
        return ""


def _now(self: Any) -> float:
    return float(self.clock())


# ---- journal (undo) ----------------------------------------------------------------------------------------

class _Journal:
    """What one call changes, so ``order_undo`` can put it back."""

    def __init__(self, self_: Any, agent: str, op: str):
        self.ag, self.agent, self.op = self_, agent, op
        self.rows: list[dict[str, Any]] = []
        self.created_tasks: list[int] = []
        self.task_status: list[dict[str, Any]] = []
        self.task_deps: list[dict[str, Any]] = []
        self.task_metadata: list[dict[str, Any]] = []

    def metadata(self, task_id: int, station: str, repo: str) -> None:
        row = self.ag.db.one("SELECT repo, context FROM tasks WHERE id=?", (task_id,))
        old = dict(row)
        context = _load(row["context"], {})
        if station:
            context["station"] = station
        else:
            context.pop("station", None)
        new = {"repo": repo, "context": _dump(context)}
        if repo == old["repo"] and context == _load(old["context"], {}):
            return
        change = next((item for item in self.task_metadata if item["task_id"] == task_id), None)
        if change:
            change["after"] = new
        else:
            self.task_metadata.append({"task_id": task_id, "before": old, "after": new})
        self.ag.db.execute("UPDATE tasks SET repo=?, context=? WHERE id=?", (repo, new["context"], task_id))

    def deps(self, task_id: int) -> None:
        """Remember what a task waits for now."""
        if any(d["task_id"] == task_id for d in self.task_deps):
            return
        rows = self.ag.db.query("SELECT blocker_id FROM agora_task_deps WHERE task_id=?", (task_id,))
        self.task_deps.append({"task_id": task_id, "blockers": [int(r["blocker_id"]) for r in rows]})

    def before(self, table: str, key: Any, *, where: Optional[dict[str, Any]] = None) -> None:
        """Remember a row as it is now (or that it does not exist yet)."""
        cond = where or {_KEYS[table]: key}
        if any(r["table"] == table and r["where"] == cond for r in self.rows):
            return
        sql = " AND ".join(f"{k}=?" for k in cond)
        row = self.ag.db.one(f"SELECT * FROM {table} WHERE {sql}", tuple(cond.values()))
        self.rows.append({"table": table, "where": cond, "row": dict(row) if row else None})

    def created(self, table: str, key: Any, *, where: Optional[dict[str, Any]] = None) -> None:
        self.rows.append({"table": table, "where": where or {_KEYS[table]: key}, "row": None})

    def save(self) -> Optional[str]:
        if self.agent == _ag().PERSON or not (self.rows or self.created_tasks or self.task_status or self.task_deps or self.task_metadata):
            return None
        if any(change["table"] in ("agora_order_budgets", "agora_order_attempts") for change in self.rows):
            for change in self.rows:
                table, cond = change["table"], change["where"]
                sql = " AND ".join(f"{key}=?" for key in cond)
                after = self.ag.db.one(f"SELECT * FROM {table} WHERE {sql}", tuple(cond.values()))
                change["after"] = dict(after) if after else None
        token = "u" + secrets.token_hex(8)
        self.ag.db.insert("agora_order_journal", {
            "token": token, "space_id": self.ag.space_id, "agent": self.agent, "op": self.op, "created": _now(self.ag),
            "changes": _dump({"rows": self.rows, "created_tasks": self.created_tasks, "task_status": self.task_status,
                              "task_deps": self.task_deps, "task_metadata": self.task_metadata})})
        return token


def order_undo(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Put back what one write changed (``token``: the ``undo`` it returned). Tasks it created are dropped."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    token = _txt(a.get("token") or a.get("undo"), 40, "token", required=True)
    with self.db.tx():
        entry = self.db.one("SELECT * FROM agora_order_journal WHERE token=?", (token,))
        if not entry or entry["space_id"] != self.space_id:
            raise _err(f"no write with undo token {token} in space {self.space_id}", 404)
        if entry["undone"]:
            raise _err(f"that write was already undone", 409)
        changes = _load(entry["changes"], {})
        for item in changes.get("task_metadata", []):
            current = self.db.one("SELECT repo, context FROM tasks WHERE id=?", (item["task_id"],))
            if current is None or dict(current) != item["after"]:
                raise _err("The task's station or repository changed since this write", 409)
        for change in changes.get("rows") or []:
            table, cond = change["table"], change["where"]
            if table != "agora_gates" and "after" not in change:
                continue
            if table == "agora_gates" and self.db.one(
                    "SELECT 1 FROM agora_order_decisions WHERE gate_id=?", (cond["id"],)):
                raise _err("A person has already decided a piece in this gate", 409)
            if "after" in change and table in _KEYS:
                sql = " AND ".join(f"{key}=?" for key in cond)
                current = self.db.one(f"SELECT * FROM {table} WHERE {sql}", tuple(cond.values()))
                after = change["after"]
                if table == "agora_orders" and after is not None and "dossier" not in after:
                    after = {**after, "dossier": "{}"}
                if (dict(current) if current else None) != after:
                    raise _err("The order changed since this write", 409)
        for change in reversed(changes.get("rows") or []):
            table, cond, row = change["table"], change["where"], change["row"]
            if table not in _KEYS:
                continue
            sql = " AND ".join(f"{k}=?" for k in cond)
            self.db.execute(f"DELETE FROM {table} WHERE {sql}", tuple(cond.values()))
            if row:
                cols = list(row)
                self.db.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                                tuple(row[c] for c in cols))
        for item in changes.get("task_deps") or []:
            if item["task_id"] not in (changes.get("created_tasks") or []):
                self._apply_deps(agent, self._task_row(item["task_id"]), add=[], remove=[], replace=item["blockers"])
        for item in changes.get("task_status") or []:
            self.db.execute("UPDATE tasks SET status=?, updated=? WHERE id=?", (item["status"], _now(self), item["task_id"]))
        for item in changes.get("task_metadata", []):
            before = item["before"]
            self.db.execute("UPDATE tasks SET repo=?, context=? WHERE id=?", (before["repo"], before["context"], item["task_id"]))
        for tid in changes.get("created_tasks") or []:
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
            if task and task["status"] not in ("done", "dropped"):
                self.db.execute("UPDATE tasks SET status='dropped', updated=? WHERE id=?", (_now(self), tid))
                self._system(dict(task), f"{agent} deshace «{entry['op']}»: la tarea queda descartada.", agent)
        self.db.execute("UPDATE agora_order_journal SET undone=? WHERE token=?", (_now(self), token))
    self._bump("agora.order.undo", {"token": token, "op": entry["op"], "by": agent})
    return {"ok": True, "undone": entry["op"], "token": token}


# ---- reading -----------------------------------------------------------------------------------------------

def _order_row(self: Any, order_id: Any, *, any_space: bool = False) -> dict[str, Any]:
    oid = _int(order_id, "order_id")
    row = self.db.one("SELECT * FROM agora_orders WHERE id=?", (oid,))
    if not row or (not any_space and row["space_id"] != self.space_id):
        raise _err(f"no order #{oid} in space {self.space_id}", 404)
    return dict(row)


def _piece_row(self: Any, piece_id: Any) -> dict[str, Any]:
    pid = _int(piece_id, "piece_id")
    row = self.db.one("SELECT * FROM agora_pieces WHERE id=?", (pid,))
    if not row or row["space_id"] != self.space_id:
        raise _err(f"no piece #{pid} in space {self.space_id}", 404)
    return dict(row)


def _order_dict(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("dossier", None)
    for key, default in (("refs", []), ("deliverables", []), ("keep", [])):
        out[key] = _load(row.get(key), default)
    return out


def _with_dossier(order: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    order["dossier"] = order_dossier.dossier(order, _load(row["dossier"], {}))
    return order


def view_of(piece: dict[str, Any]) -> str:
    """How the page shows a piece: image, turntable, video, audio, web, pdf, text, mesh, file or object."""
    path, url = piece.get("path") or "", piece.get("url") or ""
    ext = os.path.splitext(path)[1].lower()
    previews = piece.get("previews") or []
    if url and not path:
        return "web"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in (".html", ".htm"):
        return "web"
    if ext == ".pdf":
        return "pdf"
    if ext in MESH_EXT:
        return "turntable" if len(previews) > 1 else "mesh"
    if ext in TEXT_EXT:
        return "text"
    if previews:
        return "image"
    return "file" if path else "object"


def _piece_dict(self: Any, row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    out = dict(row)
    for key, default in (("previews", []), ("decisions", []), ("keep", [])):
        out[key] = _load(row.get(key), default)
    out["derivation"] = _parents(row.get("derives_from"))
    out["derives_from"] = [d["piece"] for d in out["derivation"]]
    out["provenance"] = "known" if (out["derivation"] or row.get("task_id")) else "unspecified"
    out["warnings"] = []
    for d in out["derivation"]:
        if d["role"] == "reference":
            parent = self.db.one("SELECT title, status FROM agora_pieces WHERE id=?", (d["piece"],))
            if parent and parent["status"] in ("cancelled", "stale"):
                out["warnings"].append(f"su referencia #{d['piece']} «{parent['title']}» está "
                                       + ("cancelada" if parent["status"] == "cancelled" else "caduca"))
    out["previews_stored"] = _load(row.get("previews_stored"), [])
    out["exists"] = bool(out["path"]) and (os.path.exists(out.get("stored") or "") or os.path.exists(out["path"]))
    # kept: this revision's bytes are in the store; unverified: only the file on disk, checked against the print
    out["bytes"] = "" if not out["path"] else "kept" if out.get("stored") and os.path.isfile(out["stored"]) else "unverified"
    out["view"] = view_of(out)
    out["defects_open"] = int(self.db.scalar("SELECT COUNT(*) FROM agora_piece_defects WHERE piece_id=? AND status='open'",
                                             (row["id"],), 0) or 0)
    if full:
        out["checks"] = [{**dict(c), "keep": _load(c["keep"], [])} for c in
                         self.db.query("SELECT * FROM agora_piece_checks WHERE piece_id=? ORDER BY id", (row["id"],))]
        out["defects"] = [dict(d) for d in
                          self.db.query("SELECT * FROM agora_piece_defects WHERE piece_id=? ORDER BY id", (row["id"],))]
        out["derived"] = [{"piece": int(r["id"]), "role": d["role"]} for r in self.db.query(
            "SELECT id, derives_from FROM agora_pieces WHERE space_id=? ORDER BY id", (row["space_id"],))
            for d in _parents(r["derives_from"]) if d["piece"] == row["id"]]
    return out


def _station_repo(self: Any, station: str) -> str:
    resolve = getattr(self, "order_station_repo", None)
    return (resolve(station) or "") if resolve and station else ""


def _job_dict(self: Any, row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key, default in (("inputs", []), ("produces", []), ("assignee", {})):
        out[key] = _load(row.get(key), default)
    task = self.db.one("SELECT id, title, status, owner, thread_id, repo FROM tasks WHERE id=?", (row["task_id"],))
    out["task"] = dict(task) if task else None
    out["repo"] = (task["repo"] if task else "") or _station_repo(self, row["station"])
    if task:
        out["task"]["station"] = row["station"]
    out["after"] = [int(r["blocker_id"]) for r in
                    self.db.query("SELECT blocker_id FROM agora_task_deps WHERE task_id=? ORDER BY blocker_id", (row["task_id"],))]
    out["removed"] = bool(row["removed"])
    out["defects_open"] = int(self.db.scalar("SELECT COUNT(*) FROM agora_piece_defects WHERE task_id=? AND status='open'",
                                             (row["task_id"],), 0) or 0)
    return out


def _plan(self: Any, order_id: int) -> list[dict[str, Any]]:
    jobs = [_job_dict(self, dict(r)) for r in self.db.query(
        "SELECT * FROM agora_order_jobs WHERE order_id=? ORDER BY position, task_id", (order_id,))]
    catalog = getattr(self, "order_station_catalog", None)
    order_destinations.apply_requirements(jobs, catalog() if catalog else [])
    return jobs


def _gate_dict(self: Any, row: dict[str, Any], pieces: Optional[dict[int, dict[str, Any]]] = None) -> dict[str, Any]:
    out = dict(row)
    reason = order_budget.hold_reason(self, row["order_id"])
    if reason:
        out["budget_hold"] = reason
    ids = [int(r["piece_id"]) for r in
           self.db.query("SELECT piece_id FROM agora_gate_pieces WHERE gate_id=? ORDER BY position", (row["id"],))]
    out["piece_ids"] = ids
    decided = {int(r["piece_id"]): dict(r) for r in
               self.db.query("SELECT * FROM agora_order_decisions WHERE gate_id=? ORDER BY id", (row["id"],))}
    out["decisions"] = [{k: d[k] for k in ("id", "piece_id", "decision", "note", "created")} for d in decided.values()]
    if pieces is not None:
        from .piece_checks import gate_advisories

        out["pieces"] = [pieces[i] for i in ids if i in pieces]
        out["technical_advisories"] = gate_advisories(self, out["pieces"], out.get("note") or "")
    return out


def _summary(self: Any, row: dict[str, Any]) -> dict[str, Any]:
    out = _order_dict(row)
    oid = row["id"]
    jobs = self.db.query("SELECT j.task_id, t.status FROM agora_order_jobs j LEFT JOIN tasks t ON t.id=j.task_id "
                         "WHERE j.order_id=? AND j.removed=0", (oid,))
    out["jobs"] = {"total": len(jobs), "done": sum(1 for j in jobs if j["status"] in ("done", "approved")),
                   "working": sum(1 for j in jobs if j["status"] in ("claimed", "in_progress", "review", "changes"))}
    counts = {s: 0 for s in PIECE_STATES}
    for r in self.db.query("SELECT status, COUNT(*) AS n FROM agora_pieces WHERE order_id=? GROUP BY status", (oid,)):
        counts[r["status"]] = int(r["n"])
    out["pieces"] = counts
    out["gates_open"] = int(self.db.scalar("SELECT COUNT(*) FROM agora_gates WHERE order_id=? AND status='open'", (oid,), 0) or 0)
    thumbs = self.db.query("SELECT id, kind, title, path, url, previews FROM agora_pieces WHERE order_id=? "
                           "AND status NOT IN ('cancelled','stale') ORDER BY (status='kept') DESC, id DESC LIMIT 6", (oid,))
    out["thumbs"] = []
    for t in thumbs:
        previews = _load(t["previews"], [])
        out["thumbs"].append({"id": t["id"], "kind": t["kind"], "title": t["title"],
                              "view": view_of({"path": t["path"], "url": t["url"], "previews": previews}),
                              "preview": 0 if previews else None})
    return out


def orders(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Orders of this space (or every space with all_spaces), newest change first, with what waits for the person."""
    all_spaces = str(a.get("all_spaces", "")).lower() in ("1", "true", "yes") or a.get("all_spaces") is True
    status = str(a.get("status") or "").strip().lower()
    if status and status not in ORDER_STATES:
        raise _err(f"status must be one of {', '.join(ORDER_STATES)}")
    where, params = [], []
    if not all_spaces:
        where.append("space_id=?")
        params.append(self.space_id)
    if status:
        where.append("status=?")
        params.append(status)
    limit = min(_int(a.get("limit"), "limit", required=False) or 100, 500)
    rows = self.db.query("SELECT * FROM agora_orders" + (" WHERE " + " AND ".join(where) if where else "") +
                         " ORDER BY updated DESC LIMIT ?", (*params, limit))
    out = [_summary(self, dict(r)) for r in rows]
    gate_where = "g.status='open'" + ("" if all_spaces else " AND g.space_id=?")
    gates = self.db.query("SELECT g.* FROM agora_gates g WHERE " + gate_where + " ORDER BY g.created",
                          () if all_spaces else (self.space_id,))
    waiting = []
    for g in gates:
        view = self.for_space(g["space_id"])
        order = dict(self.db.one("SELECT id, title, space_id, deadline FROM agora_orders WHERE id=?", (g["order_id"],)) or {})
        piece_rows = self.db.query("SELECT p.* FROM agora_pieces p JOIN agora_gate_pieces gp ON gp.piece_id = p.id "
                                   "WHERE gp.gate_id = ?", (g["id"],))
        pieces = {int(piece["id"]): _piece_dict(view, dict(piece)) for piece in piece_rows}
        gate = _gate_dict(view, dict(g), pieces)
        gate["order"] = order
        waiting.append(gate)
    return {"ok": True, "orders": out, "waiting": waiting, "kinds": list(PIECE_KINDS), "states": list(ORDER_STATES)}


def order(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """One order whole: its words and references, the plan with each job's task state, every piece with its
    checks and defects, the gates and the person's decisions."""
    row = _order_row(self, a.get("order_id"))
    out = _with_dossier(_summary(self, row), row)
    oid = row["id"]
    out["plan"] = _plan(self, oid)
    pieces = {int(r["id"]): _piece_dict(self, dict(r), full=True) for r in
              self.db.query("SELECT * FROM agora_pieces WHERE order_id=? ORDER BY id", (oid,))}
    out["piece_list"] = list(pieces.values())
    out["gates"] = [_gate_dict(self, dict(g), pieces) for g in
                    self.db.query("SELECT * FROM agora_gates WHERE order_id=? ORDER BY (status='open') DESC, id DESC", (oid,))]
    out["decisions"] = _decisions(self, oid)
    return {"ok": True, "order": out}


def _decisions(self: Any, order_id: Optional[int], limit: int = 200) -> list[dict[str, Any]]:
    sql = ("SELECT d.*, p.title AS piece_title, p.kind AS piece_kind FROM agora_order_decisions d "
           "LEFT JOIN agora_pieces p ON p.id=d.piece_id WHERE d.space_id=?")
    params: list[Any] = [self.space_id]
    if order_id:
        sql += " AND d.order_id=?"
        params.append(order_id)
    rows = self.db.query(sql + " ORDER BY d.id DESC LIMIT ?", (*params, limit))
    return [{**dict(r), "situation": _load(r["situation"], {})} for r in rows]


def order_decisions(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person's keep/cancel decisions of this space (or of one order), newest first, each with its situation."""
    oid = _int(a.get("order_id"), "order_id", required=False)
    if oid:
        _order_row(self, oid)
    limit = min(_int(a.get("limit"), "limit", required=False) or 100, 500)
    return {"ok": True, "decisions": _decisions(self, oid, limit)}


def job(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """What a worker needs to start a job: the order's words and details to keep, the station and how the output is
    checked, the input pieces (where they are, what they derive from), the pieces the person kept in this order (they
    fix the look), the defects sent back to this job, and an earlier kept piece of each kind it must deliver."""
    tid = _int(a.get("task_id"), "task_id")
    row = self.db.one("SELECT * FROM agora_order_jobs WHERE task_id=?", (tid,))
    plan = {entry["task_id"]: entry for entry in _plan(self, row["order_id"])} if row and row["space_id"] == self.space_id else {}
    if tid not in plan:
        raise _err(f"task #{tid} is not a job of any order in space {self.space_id}", 404)
    out = plan[tid]
    order_row = _order_row(self, row["order_id"])
    data = _order_dict(order_row)
    out["dossier"] = order_dossier.dossier(data, _load(order_row["dossier"], {}))
    out["order"] = {k: v for k, v in data.items() if k in
                    ("id", "title", "words", "refs", "deliverables", "keep", "folder", "status", "deadline", "template")}
    reason = order_budget.hold_reason(self, order_row["id"])
    if reason:
        out["order"]["budget_hold"] = reason
    inputs = []
    for pid in out["inputs"]:
        p = self.db.one("SELECT * FROM agora_pieces WHERE id=?", (pid,))
        if p:
            inputs.append(_piece_dict(self, dict(p)))
    out["input_pieces"] = inputs
    kept = [_piece_dict(self, dict(p)) for p in
            self.db.query("SELECT * FROM agora_pieces WHERE order_id=? AND status='kept' ORDER BY id", (row["order_id"],))]
    for p in kept:
        d = self.db.one("SELECT note, created FROM agora_order_decisions WHERE piece_id=? AND decision='keep' "
                        "ORDER BY id DESC LIMIT 1", (p["id"],))
        p["kept_note"] = (d["note"] if d else "") or ""
    out["references"] = kept
    active = {task_id: entry for task_id, entry in plan.items() if not entry["removed"] or task_id == tid}
    upstream = list(order_destinations.predecessors(active, tid))[1:]
    candidates = []
    if upstream:
        placeholders = ",".join("?" for _ in upstream)
        candidates = [{**dict(p), "keep": _load(p["keep"], [])} for p in self.db.query(
            f"SELECT id, keep FROM agora_pieces WHERE order_id=? AND task_id IN ({placeholders}) "
            "AND status NOT IN ('cancelled','stale') ORDER BY id DESC", (row["order_id"], *upstream))]
    out["anchored_details"] = order_destinations.anchored_details([*inputs, *kept, *candidates])
    out["defects"] = [dict(d) for d in self.db.query(
        "SELECT d.*, p.title AS piece_title FROM agora_piece_defects d LEFT JOIN agora_pieces p ON p.id=d.piece_id "
        "WHERE d.task_id=? AND d.status='open' ORDER BY d.id", (tid,))]
    examples = []
    for kind in out["produces"]:
        p = self.db.one("SELECT * FROM agora_pieces WHERE kind=? AND status='kept' AND order_id<>? ORDER BY updated DESC LIMIT 1",
                        (kind, row["order_id"]))
        if p:
            examples.append(_piece_dict(self.for_space(p["space_id"]), dict(p)))
    out["examples"] = examples
    return {"ok": True, "job": out}


# ---- writing: orders and plan ------------------------------------------------------------------------------

def order_create(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """A new order: title, the person's words, refs to where it comes from, deliverables, template, details to keep
    and a folder for its files."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    words = _txt(a.get("words") or a.get("body"), TEXT_MAX, "words")
    title = _txt(a.get("title"), 200, "title") or (words.split("\n", 1)[0][:120] if words else "")
    if not title:
        raise _err("an order needs a title or the words of the request")
    folder = _txt(a.get("folder"), 1000, "folder")
    refs = _refs(a.get("refs"))
    dossier = order_dossier.settings(a.get("dossier", {}), {}, _txt, _strs, _err)
    dossier["materials"] = order_dossier.materials(refs, folder)
    deadline = a.get("deadline")
    if deadline not in (None, ""):
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            raise _err("deadline is a Unix time in seconds") from None
    else:
        deadline = None
    now = _now(self)
    with self.db.tx():
        self._touch(agent)
        oid = self.db.insert("agora_orders", {
            "space_id": self.space_id, "title": title, "words": words, "refs": _dump(refs), "dossier": _dump(dossier),
            "deliverables": _dump(_strs(a.get("deliverables"), "deliverables")),
            "template": _txt(a.get("template"), 80, "template"), "keep": _dump(_strs(a.get("keep"), "keep")),
            "folder": folder, "status": "open", "deadline": deadline, "created_by": agent, "created": now, "updated": now})
        journal = _Journal(self, agent, "order_create")
        journal.created("agora_orders", oid)
        token = journal.save()
    self._bump("agora.order.created", {"order_id": oid, "title": title, "by": agent})
    row = _order_row(self, oid)
    return {"ok": True, "order": _with_dossier(_summary(self, row), row), "undo": token}


def order_update(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Change order metadata, source references or dossier settings."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    before = _order_row(self, a.get("order_id"))
    material_receipts = None
    refs = _refs(a["refs"]) if "refs" in a else _load(before["refs"], [])
    folder = _txt(a["folder"], 1000, "folder") if "folder" in a else before["folder"]
    sources_changed = "refs" in a or "folder" in a
    if "refs" in a or (folder != before["folder"] and any(r.get("path") and not os.path.isabs(r["path"]) for r in refs)):
        material_receipts = order_dossier.materials(refs, folder)
    with self.db.tx():
        row = _order_row(self, before["id"])
        if sources_changed and (row["refs"], row["folder"]) != (before["refs"], before["folder"]):
            raise _err("the order's sources changed while fingerprinting them; read the order and retry", 409)
        fields: dict[str, Any] = {}
        if "title" in a:
            fields["title"] = _txt(a["title"], 200, "title", required=True)
        if "words" in a:
            fields["words"] = _txt(a["words"], TEXT_MAX, "words")
        if "refs" in a:
            fields["refs"] = _dump(refs)
        if "dossier" in a or material_receipts is not None:
            saved = _load(row["dossier"], {})
            dossier = order_dossier.settings(a.get("dossier", {}), saved, _txt, _strs, _err)
            if material_receipts is not None:
                dossier["materials"] = material_receipts
            fields["dossier"] = _dump(dossier)
        for key in ("deliverables", "keep"):
            if key in a:
                fields[key] = _dump(_strs(a[key], key))
        for key, limit in (("template", 80), ("folder", 1000)):
            if key in a:
                fields[key] = _txt(a[key], limit, key)
        if "deadline" in a:
            try:
                fields["deadline"] = float(a["deadline"]) if a["deadline"] not in (None, "") else None
            except (TypeError, ValueError):
                raise _err("deadline is a Unix time in seconds") from None
        if "status" in a:
            status = str(a["status"] or "").lower()
            if status not in ORDER_STATES:
                raise _err(f"status must be one of {', '.join(ORDER_STATES)}")
            fields["status"] = status
        if not fields:
            raise _err("nothing to change: give title, words, refs, dossier, deliverables, template, keep, folder, deadline or status")
        journal = _Journal(self, agent, "order_update")
        journal.before("agora_orders", row["id"])
        fields["updated"] = _now(self)
        self.db.execute(f"UPDATE agora_orders SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                        (*fields.values(), row["id"]))
        token = journal.save()
    self._bump("agora.order.updated", {"order_id": row["id"], "by": agent, "fields": sorted(fields)})
    row = _order_row(self, row["id"])
    return {"ok": True, "order": _with_dossier(_summary(self, row), row), "undo": token}


def _set_status(self: Any, order_id: int, status: str, journal: _Journal, *, only_from: tuple[str, ...]) -> None:
    row = self.db.one("SELECT status FROM agora_orders WHERE id=?", (order_id,))
    if row and row["status"] in only_from and row["status"] != status:
        journal.before("agora_orders", order_id)
        self.db.execute("UPDATE agora_orders SET status=?, updated=? WHERE id=?", (status, _now(self), order_id))


def plan_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Add, change or remove the jobs of an order. Each job without task_id becomes an Ágora task of this space;
    ``after`` names the jobs it waits for (their key in this call or a task id)."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    jobs = a.get("jobs") or []
    remove = _ids(a.get("remove"), "remove")
    if not isinstance(jobs, list) or len(jobs) > 60:
        raise _err("jobs must be a list of at most 60 jobs")
    if not jobs and not remove:
        raise _err("give jobs to add or change, or remove with task ids")
    with self.db.tx():
        order_row = _order_row(self, a.get("order_id"))
        oid = order_row["id"]
        journal = _Journal(self, agent, "plan_set")
        keys: dict[str, int] = {}
        position = int(self.db.scalar("SELECT COALESCE(MAX(position), 0) FROM agora_order_jobs WHERE order_id=?", (oid,), 0) or 0)
        touched: list[int] = []
        pending_after: list[tuple[int, list[Any]]] = []
        for raw in jobs:
            if not isinstance(raw, dict):
                raise _err("each job is an object {title, station, repo, skill, inputs, produces, check, assignee, after}")
            inputs = _ids(raw.get("inputs"), "inputs")
            for pid in inputs:
                p = self.db.one("SELECT order_id FROM agora_pieces WHERE id=?", (pid,))
                if not p or p["order_id"] != oid:
                    raise _err(f"input piece #{pid} is not a piece of order #{oid}", 404)
            fields = {"station": _txt(raw.get("station"), 120, "station"), "skill": _txt(raw.get("skill"), 200, "skill"),
                      "inputs": _dump(inputs), "produces": _dump([_kind(k, "produces") for k in (raw.get("produces") or [])][:20]),
                      "check_how": _txt(raw.get("check"), 4000, "check"), "assignee": _dump(_assignee(raw.get("assignee"))),
                      "branch": _txt(raw.get("branch"), 80, "branch")}
            repo = _txt(raw.get("repo"), 120, "repo")
            tid = _int(raw.get("task_id"), "task_id", required=False)
            if tid:
                existing = self.db.one("SELECT * FROM agora_order_jobs WHERE task_id=?", (tid,))
                if not existing or existing["order_id"] != oid:
                    raise _err(f"task #{tid} is not a job of order #{oid}", 404)
                station = fields["station"] if "station" in raw else existing["station"]
                journal.before("agora_order_jobs", tid)
                given = {k: v for k, v in fields.items() if (k if k != "check_how" else "check") in raw}
                if given:
                    self.db.execute(f"UPDATE agora_order_jobs SET {', '.join(f'{k}=?' for k in given)} WHERE task_id=?",
                                    (*given.values(), tid))
                if "repo" not in raw and station == existing["station"]:
                    repo = self.db.scalar("SELECT repo FROM tasks WHERE id=?", (tid,), "")
                journal.metadata(tid, station, repo or _station_repo(self, station))
            else:
                title = _txt(raw.get("title"), 300, "title", required=True)
                station = fields["station"]
                lines = [f"Trabajo de la orden #{oid} «{order_row['title']}»."]
                if station:
                    lines.append(f"Puesto: {station}" + (f" · skill {fields['skill']}" if fields["skill"] else ""))
                if raw.get("produces"):
                    lines.append("Entrega: " + ", ".join(_load(fields["produces"], [])))
                if inputs:
                    lines.append("Piezas de entrada: " + ", ".join(f"#{p}" for p in inputs))
                lines.append("El encargo entero, con las piezas que conservó Luis: hub_agora_job con este task_id.")
                body = _txt(raw.get("body"), 20000, "body")
                assignee = _assignee(raw.get("assignee"))
                added = self.task_add({"agent": agent, "_person": bool(a.get("_person")), "title": title,
                                       "body": (body + "\n\n" if body else "") + "\n".join(lines),
                                       "kind": raw.get("kind") or "task", "acceptance": fields["check_how"],
                                       "repo": repo or _station_repo(self, station),
                                       "context": {"station": station} if station else {},
                                       "labels": [f"order-{oid}"] + ([fields["branch"]] if fields["branch"] else []),
                                       **({"mentions": [assignee["agent"]]} if assignee.get("agent") else {})})
                tid = int(added["task"]["id"])
                journal.created_tasks.append(tid)
                position += 1
                self.db.insert("agora_order_jobs", {"task_id": tid, "order_id": oid, "space_id": self.space_id,
                                                    "position": position, "created": _now(self), **fields})
                journal.created("agora_order_jobs", tid)
            if raw.get("key"):
                keys[str(raw["key"])] = tid
            if "after" in raw:
                pending_after.append((tid, list(raw.get("after") or [])))
            touched.append(tid)
        for tid, after in pending_after:
            journal.deps(tid)
            blockers = []
            for ref in after:
                blockers.append(keys[str(ref)] if str(ref) in keys else _int(ref, "after"))
            self._apply_deps(agent, self._task_row(tid), add=[], remove=[], replace=blockers)
        for tid in remove:
            existing = self.db.one("SELECT * FROM agora_order_jobs WHERE task_id=?", (tid,))
            if not existing or existing["order_id"] != oid:
                raise _err(f"task #{tid} is not a job of order #{oid}", 404)
            journal.before("agora_order_jobs", tid)
            self.db.execute("UPDATE agora_order_jobs SET removed=1 WHERE task_id=?", (tid,))
            task = self._task_row(tid)
            if task["status"] == "open":
                journal.task_status.append({"task_id": tid, "status": task["status"]})
                self.db.execute("UPDATE tasks SET status='dropped', updated=? WHERE id=?", (_now(self), tid))
                self._system(task, f"{agent} quita este trabajo del plan de la orden #{oid}.", agent)
        if touched:
            _set_status(self, oid, "producing", journal, only_from=("open",))
        journal.before("agora_orders", oid)
        self.db.execute("UPDATE agora_orders SET updated=? WHERE id=?", (_now(self), oid))
        token = journal.save()
    self._bump("agora.order.plan", {"order_id": oid, "by": agent, "jobs": touched, "removed": remove})
    return {"ok": True, "plan": _plan(self, oid), "keys": keys, "undo": token}


# ---- writing: pieces, checks and defects --------------------------------------------------------------------

def _resolve(folder: str, path: str) -> str:
    if not path:
        return ""
    if not os.path.isabs(path) and folder:
        path = os.path.join(folder, path)
    return os.path.normpath(path)


def piece_add(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Register a piece of an order: kind, title, where it is (path, url, or app + object + place), previews, the job
    that made it, the agent and model, the pieces it derives from and the details to keep (the order's by default)."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    # Reading and copying the files happens before the transaction, so a large file never holds the Agora.
    folder = _order_row(self, a.get("order_id"))["folder"]
    path = _resolve(folder, _txt(a.get("path"), 1000, "path"))
    previews = [_resolve(folder, p) for p in _strs(a.get("previews"), "previews", limit=1000, items=24)]
    sha = _print(path) if path else ""
    stored = _keep_copy(self, path, sha) if path else ""
    previews_stored = [_keep_copy(self, pv, _sha256(pv)) for pv in previews]
    with self.db.tx():
        order_row = _order_row(self, a.get("order_id"))
        oid = order_row["id"]
        kind = _kind(a.get("kind"))
        title = _txt(a.get("title"), 200, "title", required=True)
        url = _txt(a.get("url"), 2000, "url")
        app, obj, place = (_txt(a.get(k), 300, k) for k in ("app", "object", "place"))
        if not (path or url or app):
            raise _err("say where the piece is: path (a file), url, or app with object and place")
        task_id = _int(a.get("task_id"), "task_id", required=False)
        station = _txt(a.get("station"), 120, "station")
        if task_id:
            job_row = self.db.one("SELECT * FROM agora_order_jobs WHERE task_id=?", (task_id,))
            if not job_row or job_row["order_id"] != oid:
                raise _err(f"task #{task_id} is not a job of order #{oid}", 404)
            station = station or job_row["station"]
        derivation = _derivation(a.get("derives_from"))
        derives = [d["piece"] for d in derivation]
        source_rev = _txt(a.get("source_rev"), 200, "source_rev")
        supersedes = _int(a.get("supersedes"), "supersedes", required=False)
        revision = 1
        # The logical output: the same job, kind and place (file, address or app object). A retry of it with the same
        # content (print, or the app object's revision) and the same inputs is the piece it already is; other content
        # is its next revision. The same bytes from another job or of another kind are a piece of their own.
        same = [dict(r) for r in self.db.query(
            "SELECT id, sha256, source_rev, derives_from FROM agora_pieces WHERE order_id=? AND task_id IS ? AND kind=? "
            "AND path=? AND url=? AND app=? AND object=? AND place=? ORDER BY revision DESC",
            (oid, task_id, kind, path, url, app, obj, place))]
        for prev in same:
            content_same = (prev["sha256"] == sha) if path else (prev["source_rev"] == source_rev)
            if content_same and (not path or sha) and _parents(prev["derives_from"]) == derivation:
                return {"ok": True, "existing": True, "undo": None,
                        "piece": _piece_dict(self, _piece_row(self, prev["id"]), full=True)}
        if same and not supersedes:
            supersedes = int(same[0]["id"])
        if supersedes:
            old = self.db.one("SELECT order_id, revision FROM agora_pieces WHERE id=?", (supersedes,))
            if not old or old["order_id"] != oid:
                raise _err(f"piece #{supersedes} is not a piece of order #{oid}", 404)
            revision = int(old["revision"]) + 1
        decisions: list[int] = []
        inherited: list[dict[str, Any]] = []
        for pid in derives:
            parent = _piece_row(self, pid)
            decisions += [d for d in _load(parent["decisions"], []) if d not in decisions]
            kept = self.db.one("SELECT id FROM agora_order_decisions WHERE piece_id=? AND decision='keep' ORDER BY id DESC LIMIT 1", (pid,))
            if kept and kept["id"] not in decisions:
                decisions.append(int(kept["id"]))
            for item in _load(parent["keep"], []):
                if isinstance(item, dict) and item.get("detail"):
                    inherited.append({"detail": item["detail"], "parent": pid, "source": item.get("source") or "piece",
                                      "from": item.get("from"), "anchor": item.get("anchor")})
        # The details to keep are a union, never a replacement: the order's, every detail of the pieces this one comes
        # from (material or reference), every detail of the revision it replaces, and then the ones given here. A detail
        # given here that is already inherited keeps the provenance it came with; a new one is this piece's own. Every
        # detail starts unchecked: inheriting a detail never inherits its approval, and a new revision carries neither the
        # checks of the one it replaces nor the person's decision on it.
        details: dict[str, dict[str, Any]] = {}

        def _detail(text: str, prov: dict[str, Any]) -> None:
            key = text.casefold()
            if key in details:
                for k, v in prov.items():           # more of where it came through; what it already says stays
                    if v is not None and k not in details[key]:
                        details[key][k] = v
                return
            details[key] = {"detail": text, **{k: v for k, v in prov.items() if v is not None}}

        for d in _load(order_row["keep"], []):
            if isinstance(d, str) and d.strip():
                _detail(d, {"source": "order"})
        for item in inherited:
            set_at_piece = item["source"] != "order"
            _detail(item["detail"], {"source": "piece" if set_at_piece else "order", "via": item["parent"],
                                     "anchor": item.get("anchor"),
                                     "from": (item["from"] if item["from"] is not None else item["parent"])
                                     if set_at_piece else None})
        if supersedes:
            old_keep = self.db.one("SELECT keep FROM agora_pieces WHERE id=?", (supersedes,))
            for item in _load(old_keep["keep"] if old_keep else "[]", []):
                if not isinstance(item, dict) or not item.get("detail"):
                    continue
                set_at_piece = item.get("source") != "order"
                _detail(item["detail"], {"source": "piece" if set_at_piece else "order", "via": item.get("via"),
                                         "anchor": item.get("anchor"),
                                         "from": (item.get("from") if item.get("from") is not None else supersedes)
                                         if set_at_piece else None, "revision_of": supersedes})
        for d in (_strs(a.get("keep"), "keep") if a.get("keep") is not None else []):
            _detail(d, {"source": "piece"})
        keep: list[dict[str, Any]] = [{"id": f"k{i + 1}", **item, "state": "pending"}
                                      for i, item in enumerate(details.values())]
        now = _now(self)
        pid = self.db.insert("agora_pieces", {
            "order_id": oid, "space_id": self.space_id, "kind": kind, "title": title, "path": path, "url": url,
            "app": app, "object": obj, "place": place, "previews": _dump(previews), "task_id": task_id,
            "station": station, "agent": _txt(a.get("by"), 80, "by") or agent, "model": _txt(a.get("model"), 120, "model"),
            "derives_from": _dump(derivation), "decisions": _dump(decisions), "keep": _dump(keep),
            "note": _txt(a.get("note"), 4000, "note"), "status": "provisional", "revision": revision, "sha256": sha,
            "source_rev": source_rev, "supersedes": supersedes, "stored": stored,
            "previews_stored": _dump(previews_stored),
            "created": now, "updated": now})
        journal = _Journal(self, agent, "piece_add")
        journal.created("agora_pieces", pid)
        material = [d["piece"] for d in derivation if d["role"] == "material"]
        stale_parent = self.db.one(f"SELECT id FROM agora_pieces WHERE id IN ({','.join('?' for _ in material) or 'NULL'}) "
                                   "AND status IN ('cancelled','stale') LIMIT 1", tuple(material))
        if stale_parent:
            self.db.execute("UPDATE agora_pieces SET status='stale', stale_because=? WHERE id=?",
                            (f"deriva de la pieza #{stale_parent['id']}, cancelada o caduca", pid))
        journal.before("agora_orders", oid)
        self.db.execute("UPDATE agora_orders SET updated=? WHERE id=?", (now, oid))
        token = journal.save()
    self._bump("agora.order.piece", {"order_id": oid, "piece_id": pid, "kind": kind, "by": agent})
    return {"ok": True, "piece": _piece_dict(self, _piece_row(self, pid), full=True), "undo": token}


def piece_check(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Note a check of a piece: what was looked at (note), whether it passed (ok) and, for each detail to keep, whether
    it is there (keep: [{id or detail, state ok|missing|not_visible, how, evidence, note}]). A passed check with no
    detail missing or unchecked marks the piece checked."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    note = _txt(a.get("note"), TEXT_MAX, "note", required=True)
    ok = a.get("ok", True) not in (False, "false", "0", 0)
    marks = a.get("keep") or []
    if not isinstance(marks, list) or len(marks) > 60:
        raise _err("keep is a list of {detail, ok, note}")
    with self.db.tx():
        piece = _piece_row(self, a.get("piece_id"))
        journal = _Journal(self, agent, "piece_check")
        journal.before("agora_pieces", piece["id"])
        keep = _load(piece["keep"], [])
        clean = []
        anchor_changed = False
        for mark in marks:
            if not isinstance(mark, dict):
                raise _err("each keep mark is {detail or id, state or ok, how, evidence, note}")
            key = _txt(mark.get("id"), 20, "keep.id")
            detail = _txt(mark.get("detail"), 300, "keep.detail", required=not key)
            item = next((k for k in keep if (key and k.get("id") == key) or (detail and k["detail"] == detail)), None)
            if item is None:
                if not detail:
                    raise _err(f"no detail {key} on piece #{piece['id']}", 404)
                item = {"id": f"k{len(keep) + 1}", "detail": detail, "source": "check"}
                keep.append(item)
            state = str(mark.get("state") or "").lower() or (
                "ok" if mark.get("ok", True) not in (False, "false", "0", 0) else "missing")
            if state not in DETAIL_STATES[1:]:
                raise _err("keep state is ok, missing or not_visible")
            if "anchor" in mark:
                raw = mark["anchor"]
                target = _piece_row(self, raw.get("piece")) if isinstance(raw, dict) else piece
                if target["order_id"] != piece["order_id"]:
                    raise _err("anchor must refer to a piece of the same order", 404)
                item["anchor"] = order_destinations.pin_anchor(raw, target, _load(target["previews_stored"], []), _err)
                anchor_changed = True
            item.update({"state": state, "by": agent, "check_rev": int(piece["revision"]),
                         **{k: _txt(mark.get(k), 1000, f"keep.{k}") for k in ("note", "how", "evidence") if mark.get(k)}})
            clean.append({"id": item["id"], "detail": item["detail"], "state": state,
                          **{k: item[k] for k in ("note", "how", "evidence", "anchor") if k in item}})
        cid = self.db.insert("agora_piece_checks", {"piece_id": piece["id"], "space_id": self.space_id, "agent": agent,
                                                    "ok": 1 if ok else 0, "note": note, "keep": _dump(clean),
                                                    "created": _now(self)})
        journal.created("agora_piece_checks", cid)
        status = piece["status"]
        if ok and status == "provisional" and all(k.get("state") in ("ok", "not_visible") for k in keep):
            status = "checked"
        self.db.execute("UPDATE agora_pieces SET keep=?, status=?, updated=? WHERE id=?",
                        (_dump(keep), status, _now(self), piece["id"]))
        if anchor_changed:
            journal.before("agora_orders", piece["order_id"])
            self.db.execute("UPDATE agora_orders SET updated=? WHERE id=?", (_now(self), piece["order_id"]))
        token = journal.save()
    self._bump("agora.order.check", {"piece_id": piece["id"], "ok": ok, "by": agent})
    return {"ok": True, "piece": _piece_dict(self, _piece_row(self, piece["id"]), full=True), "undo": token}


def _send_back(self: Any, journal: _Journal, piece: dict[str, Any], agent: str, note: str, *, view: str = "",
               pose: str = "", at: str = "", step: str = "", expected: str = "", observed: str = "", evidence: str = "",
               source: str = "check", task_id: Optional[int] = None) -> int:
    """Record a defect of a piece and tell the job that made it, in its task's thread."""
    target = task_id or piece.get("task_id")
    station = piece.get("station") or ""
    if task_id and task_id != piece.get("task_id"):
        job_row = self.db.one("SELECT station, order_id FROM agora_order_jobs WHERE task_id=?", (task_id,))
        if not job_row or job_row["order_id"] != piece["order_id"]:
            raise _err(f"task #{task_id} is not a job of order #{piece['order_id']}", 404)
        station = job_row["station"]
    did = self.db.insert("agora_piece_defects", {
        "piece_id": piece["id"], "order_id": piece["order_id"], "space_id": self.space_id, "task_id": target,
        "station": station, "agent": agent, "note": note, "view": view, "pose": pose, "at": at, "step": step,
        "expected": expected, "observed": observed, "evidence": evidence,
        "source": source, "status": "open", "created": _now(self)})
    journal.created("agora_piece_defects", did)
    order_budget.check_returns(self, _order_row(self, piece["order_id"]), journal, agent)
    if target:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (target,))
        if task:
            task = self._task_row(target)
            where = ", ".join(f"{k} {v}" for k, v in (("vista", view), ("pose", pose), ("segundo", at), ("paso", step)) if v)
            who = "Luis cancela" if source == "gate" else f"{agent} devuelve"
            text = (f"{who} la pieza #{piece['id']} «{piece['title']}» (revisión {piece.get('revision', 1)})"
                    + (f" ({where})" if where else "") + f": {note}"
                    + (f"\nEsperado: {expected}" if expected else "") + (f"\nObservado: {observed}" if observed else "")
                    + (f"\nPrueba: {evidence}" if evidence else "")
                    + f"\nDetalle: hub_agora_job con task_id {target}.")
            self._msg(int(task["thread_id"]), agent, "comment", text, [task["owner"]] if task.get("owner") else [])
            if task["status"] in ("review", "approved", "done"):
                journal.task_status.append({"task_id": target, "status": task["status"]})
                self.db.execute("UPDATE tasks SET status='changes', updated=? WHERE id=?", (_now(self), target))
    return did


def _stale_below(self: Any, journal: _Journal, piece_id: int) -> list[int]:
    """Mark stale every piece derived (directly or not) from piece_id; returns their ids."""
    rows = [dict(r) for r in self.db.query("SELECT id, derives_from, status FROM agora_pieces WHERE space_id=?", (self.space_id,))]
    stale, frontier = [], [piece_id]
    while frontier:
        current = frontier.pop()
        for r in rows:
            uses = any(d["piece"] == current and d["role"] == "material" for d in _parents(r["derives_from"]))
            if uses and r["id"] not in stale and r["id"] != piece_id:
                stale.append(r["id"])
                frontier.append(r["id"])
                if r["status"] not in ("cancelled", "stale"):
                    journal.before("agora_pieces", r["id"])
                    self.db.execute("UPDATE agora_pieces SET status='stale', stale_because=?, updated=? WHERE id=?",
                                    (f"deriva de la pieza #{piece_id}, cancelada", _now(self), r["id"]))
    return stale


def piece_defect(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Send a reproducible defect of a piece back to the job that caused it (task_id, default the job that made the
    piece): note, and view, pose, at (second) and step where it shows. The job's task hears it in its thread."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    note = _txt(a.get("note"), TEXT_MAX, "note", required=True)
    with self.db.tx():
        piece = _piece_row(self, a.get("piece_id"))
        journal = _Journal(self, agent, "piece_defect")
        did = _send_back(self, journal, piece, agent, note,
                         **{k: _txt(a.get(k), 300, k) for k in ("view", "pose", "at", "step")},
                         **{k: _txt(a.get(k), 4000, k) for k in ("expected", "observed", "evidence")},
                         task_id=_int(a.get("task_id"), "task_id", required=False))
        token = journal.save()
    self._bump("agora.order.defect", {"piece_id": piece["id"], "defect_id": did, "by": agent})
    return {"ok": True, "defect_id": did, "piece": _piece_dict(self, _piece_row(self, piece["id"]), full=True), "undo": token}


def defect_close(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Close a defect once the same check passes again: fixed_in is the piece (the new revision) where it was
    repeated, note says what was checked."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    note = _txt(a.get("note"), TEXT_MAX, "note", required=True)
    did = _int(a.get("defect_id"), "defect_id")
    with self.db.tx():
        row = self.db.one("SELECT * FROM agora_piece_defects WHERE id=?", (did,))
        if not row or row["space_id"] != self.space_id:
            raise _err(f"no defect #{did} in space {self.space_id}", 404)
        if row["status"] != "open":
            raise _err(f"defect #{did} is already closed", 409)
        fixed_in = _int(a.get("fixed_in"), "fixed_in", required=False)
        if fixed_in:
            p = self.db.one("SELECT order_id FROM agora_pieces WHERE id=?", (fixed_in,))
            if not p or p["order_id"] != row["order_id"]:
                raise _err(f"piece #{fixed_in} is not a piece of order #{row['order_id']}", 404)
        journal = _Journal(self, agent, "defect_close")
        journal.before("agora_piece_defects", did)
        self.db.execute("UPDATE agora_piece_defects SET status='closed', closed_by=?, closed_in=?, closed_note=?, closed=? "
                        "WHERE id=?", (agent, fixed_in, note, _now(self), did))
        token = journal.save()
    self._bump("agora.order.defect_closed", {"defect_id": did, "by": agent})
    return {"ok": True, "defect": dict(self.db.one("SELECT * FROM agora_piece_defects WHERE id=?", (did,))), "undo": token}


# ---- gates ------------------------------------------------------------------------------------------------

def gate_open(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Show a set of pieces of an order to the person for one decision (keep or cancel). The order waits for them."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    ids = _ids(a.get("piece_ids"), "piece_ids")
    if not ids:
        raise _err("piece_ids: the pieces to show")
    with self.db.tx():
        order_row = _order_row(self, a.get("order_id"))
        for pid in ids:
            p = self.db.one("SELECT order_id FROM agora_pieces WHERE id=?", (pid,))
            if not p or p["order_id"] != order_row["id"]:
                raise _err(f"piece #{pid} is not a piece of order #{order_row['id']}", 404)
        title = _txt(a.get("title"), 200, "title") or f"Puerta de «{order_row['title']}»"
        journal = _Journal(self, agent, "gate_open")
        gid = self.db.insert("agora_gates", {"order_id": order_row["id"], "space_id": self.space_id, "title": title,
                                             "note": _txt(a.get("note"), TEXT_MAX, "note"), "status": "open",
                                             "opened_by": agent, "created": _now(self)})
        journal.created("agora_gates", gid)
        for i, pid in enumerate(ids):
            self.db.insert("agora_gate_pieces", {"gate_id": gid, "piece_id": pid, "position": i})
            journal.created("agora_gate_pieces", None, where={"gate_id": gid, "piece_id": pid})
        _set_status(self, order_row["id"], "waiting", journal, only_from=("open", "producing"))
        token = journal.save()
    self._bump("agora.order.gate", {"order_id": order_row["id"], "gate_id": gid, "pieces": ids, "by": agent})
    return {"ok": True, "gate": _gate_dict(self, dict(self.db.one("SELECT * FROM agora_gates WHERE id=?", (gid,)))), "undo": token}


def _gate_row(self: Any, gate_id: Any) -> dict[str, Any]:
    gid = _int(gate_id, "gate_id")
    row = self.db.one("SELECT * FROM agora_gates WHERE id=?", (gid,))
    if not row or row["space_id"] != self.space_id:
        raise _err(f"no gate #{gid} in space {self.space_id}", 404)
    return dict(row)


def gate_split(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Take a piece out of an open gate into a gate of its own, to be decided alone."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    with self.db.tx():
        gate = _gate_row(self, a.get("gate_id"))
        pid = _int(a.get("piece_id"), "piece_id")
        if gate["status"] != "open":
            raise _err(f"gate #{gate['id']} is already decided", 409)
        if self.db.one("SELECT 1 AS x FROM agora_order_decisions WHERE gate_id=? AND piece_id=?", (gate["id"], pid)):
            raise _err(f"piece #{pid} is already decided in gate #{gate['id']}", 409)
        if not self.db.one("SELECT 1 AS x FROM agora_gate_pieces WHERE gate_id=? AND piece_id=?", (gate["id"], pid)):
            raise _err(f"piece #{pid} is not in gate #{gate['id']}", 404)
        title = self.db.scalar("SELECT title FROM agora_pieces WHERE id=?", (pid,), "")
        journal = _Journal(self, agent, "gate_split")
        journal.before("agora_gate_pieces", None, where={"gate_id": gate["id"], "piece_id": pid})
        self.db.execute("DELETE FROM agora_gate_pieces WHERE gate_id=? AND piece_id=?", (gate["id"], pid))
        gid = self.db.insert("agora_gates", {"order_id": gate["order_id"], "space_id": self.space_id,
                                             "title": f"{title}" or gate["title"], "note": gate["note"], "status": "open",
                                             "parent_gate": gate["id"], "opened_by": agent, "created": _now(self)})
        journal.created("agora_gates", gid)
        self.db.insert("agora_gate_pieces", {"gate_id": gid, "piece_id": pid, "position": 0})
        journal.created("agora_gate_pieces", None, where={"gate_id": gid, "piece_id": pid})
        _close_if_done(self, journal, gate["id"])
        token = journal.save()
    self._bump("agora.order.gate", {"order_id": gate["order_id"], "gate_id": gid, "split_from": gate["id"], "by": agent})
    return {"ok": True, "gate": _gate_dict(self, dict(self.db.one("SELECT * FROM agora_gates WHERE id=?", (gid,)))), "undo": token}


def _close_if_done(self: Any, journal: _Journal, gate_id: int) -> None:
    left = self.db.scalar("SELECT COUNT(*) FROM agora_gate_pieces gp WHERE gp.gate_id=? AND NOT EXISTS "
                          "(SELECT 1 FROM agora_order_decisions d WHERE d.gate_id=gp.gate_id AND d.piece_id=gp.piece_id)",
                          (gate_id,), 0)
    if not left:
        journal.before("agora_gates", gate_id)
        self.db.execute("UPDATE agora_gates SET status='decided', closed=? WHERE id=?", (_now(self), gate_id))
        order_id = self.db.scalar("SELECT order_id FROM agora_gates WHERE id=?", (gate_id,))
        if (not order_budget.hold_reason(self, order_id)
                and not self.db.scalar("SELECT COUNT(*) FROM agora_gates WHERE order_id=? AND status='open'", (order_id,), 0)):
            _set_status(self, order_id, "producing", journal, only_from=("waiting",))


def gate_decide(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person's decision on a gate: keep or cancel, with an optional note, for every undecided piece of the gate
    or only for piece_id. Only the person's own pages may call it."""
    if not a.get("_person"):
        raise _err("only the person decides a gate, from their own page", 403)
    agent = self._agent(a.get("agent"), allow_person=True)
    decision = str(a.get("decision") or "").lower()
    if decision not in DECISIONS:
        raise _err("decision is keep or cancel")
    note = _txt(a.get("note"), TEXT_MAX, "note")
    with self.db.tx():
        gate = _gate_row(self, a.get("gate_id"))
        if gate["status"] != "open":
            raise _err(f"gate #{gate['id']} is already decided", 409)
        in_gate = [int(r["piece_id"]) for r in
                   self.db.query("SELECT piece_id FROM agora_gate_pieces WHERE gate_id=? ORDER BY position", (gate["id"],))]
        done = {int(r["piece_id"]) for r in self.db.query("SELECT piece_id FROM agora_order_decisions WHERE gate_id=?", (gate["id"],))}
        only = _int(a.get("piece_id"), "piece_id", required=False)
        if only and only not in in_gate:
            raise _err(f"piece #{only} is not in gate #{gate['id']}", 404)
        targets = [p for p in ([only] if only else in_gate) if p not in done]
        if not targets:
            raise _err("every piece of this gate is already decided", 409)
        for pid in targets:
            if not _bytes_ok(self, _piece_row(self, pid)):
                raise _err(f"piece #{pid}: its file changed since it was registered and no copy of that revision is "
                           "kept; the station has to register it again before it can be decided", 409,
                           error_code="piece_changed", piece_id=pid)
        order_row = _order_row(self, gate["order_id"])
        journal = _Journal(self, agent, "gate_decide")
        alternatives = []
        for pid in in_gate:
            p = self.db.one("SELECT id, kind, title, status FROM agora_pieces WHERE id=?", (pid,))
            if p:
                alternatives.append(dict(p))
        situation = {"order": {"id": order_row["id"], "title": order_row["title"], "status": order_row["status"]},
                     "deadline": order_row["deadline"], "gate": {"id": gate["id"], "title": gate["title"]},
                     "alternatives": alternatives}
        result = []
        for pid in targets:
            piece = _piece_row(self, pid)
            did = self.db.insert("agora_order_decisions", {
                "order_id": order_row["id"], "gate_id": gate["id"], "piece_id": pid, "space_id": self.space_id,
                "decision": decision, "note": note, "situation": _dump(situation), "created": _now(self)})
            stale: list[int] = []
            if decision == "keep":
                self.db.execute("UPDATE agora_pieces SET status='kept', updated=? WHERE id=?", (_now(self), pid))
            else:
                self.db.execute("UPDATE agora_pieces SET status='cancelled', updated=? WHERE id=?", (_now(self), pid))
                _send_back(self, journal, piece, agent, note or "Cancelada en la puerta, sin nota.", source="gate")
                stale = _stale_below(self, journal, pid)
            result.append({"piece_id": pid, "decision_id": did, "stale": stale})
        _close_if_done(self, journal, gate["id"])
    self._bump("agora.order.decided", {"order_id": order_row["id"], "gate_id": gate["id"], "decision": decision,
                                       "pieces": targets})
    return {"ok": True, "decided": result, "gate": _gate_dict(self, _gate_row(self, gate["id"]))}


# ---- files ------------------------------------------------------------------------------------------------

def _bytes_ok(self: Any, piece: dict[str, Any]) -> bool:
    """True when what would be shown of a piece is the revision that was registered."""
    if not piece.get("path"):
        return True
    if piece.get("stored") and os.path.isfile(piece["stored"]):
        return True
    return bool(piece.get("sha256")) and _print(piece["path"]) == piece["sha256"]


def piece_file(self: Any, piece_id: Any, *, preview: Any = None, rel: str = "") -> str:
    """The path of a piece's file, one of its previews, or a file next to it (``rel``, for a web page's own styles and
    scripts): the copy kept for that revision, else the file on disk while it still has the registered print. Only
    what a piece names, or what sits in its folder, is served."""
    piece = dict(self.db.one("SELECT * FROM agora_pieces WHERE id=?", (_int(piece_id, "piece_id"),)) or {})
    if not piece:
        raise _err("no such piece", 404)
    stored = piece.get("stored") if piece.get("stored") and os.path.isfile(piece["stored"]) else ""
    if preview not in (None, ""):
        previews = _load(piece["previews"], [])
        kept = _load(piece.get("previews_stored"), [])
        try:
            i = int(preview)
        except (TypeError, ValueError):
            raise _err("preview is a number") from None
        if not 0 <= i < len(previews):
            raise _err("no such preview", 404)
        snapshot = kept[i] if i < len(kept) else ""
        path = snapshot if snapshot and os.path.isfile(snapshot) else previews[i]
        digest = os.path.splitext(os.path.basename(snapshot))[0]
        if path != snapshot and re.fullmatch(r"[a-f0-9]{64}", digest) and _sha256(path) != digest:
            raise _err("the preview changed since it was registered", 409, error_code="piece_changed")
    elif rel:
        if not stored and not _bytes_ok(self, piece):
            raise _err("the page changed since it was registered", 409, error_code="piece_changed")
        base = os.path.dirname(stored or piece["path"]) if piece["path"] else ""
        if not base:
            raise _err("this piece has no folder", 404)
        path = os.path.normpath(os.path.join(base, rel))
        root = os.path.normcase(os.path.realpath(base))
        if os.path.commonpath([root, os.path.normcase(os.path.realpath(path))]) != root:
            raise _err("outside the piece's folder", 403)
    else:
        path = stored or piece["path"]
        if not stored and path and not _bytes_ok(self, piece):
            raise _err("the file changed since it was registered and no copy of that revision is kept", 409,
                       error_code="piece_changed")
    if not path or not os.path.isfile(path):
        raise _err("the file is not there", 404)
    return path


_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_SCRIPT = re.compile(r"<script\b([^>]*?)\bsrc=([\"'])([^\"']+)\2([^>]*)>\s*</script>", re.IGNORECASE)
_SRC = re.compile(r"(<(?:img|source|video|audio)\b[^>]*?\bsrc=)([\"'])([^\"']+)\2", re.IGNORECASE)
_HREF = re.compile(r"\bhref=([\"'])([^\"']+)\1", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", re.IGNORECASE)
INLINE_MAX = 16 * 1024 * 1024


def inline_html(path: str) -> str:
    """A local web page as one document: its own styles, scripts and images (relative paths inside its folder) are
    written into it, so the page can be shown in a sandboxed frame that never reaches the hub."""
    import base64
    import mimetypes
    root = os.path.normcase(os.path.realpath(os.path.dirname(path)))
    budget = [INLINE_MAX]

    def local(base: str, rel: str) -> Optional[str]:
        rel = rel.split("#", 1)[0].split("?", 1)[0]
        if not rel or re.match(r"^[a-z][a-z0-9+.-]*:|^//|^/", rel, re.IGNORECASE):
            return None
        target = os.path.normpath(os.path.join(base, rel))
        real = os.path.normcase(os.path.realpath(target))
        if os.path.commonpath([root, real]) != root or not os.path.isfile(target) or os.path.getsize(target) > budget[0]:
            return None
        budget[0] -= os.path.getsize(target)
        return target

    def text(target: str) -> str:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def data_uri(target: str) -> str:
        with open(target, "rb") as fh:
            return f"data:{mimetypes.guess_type(target)[0] or 'application/octet-stream'};base64," + \
                   base64.b64encode(fh.read()).decode("ascii")

    def css(code: str, base: str) -> str:
        def url(m: "re.Match[str]") -> str:
            target = local(base, m.group(2))
            return f'url("{data_uri(target)}")' if target else m.group(0)
        return _CSS_URL.sub(url, code)

    base = os.path.dirname(path)
    html = text(path)

    def link(m: "re.Match[str]") -> str:
        tag = m.group(0)
        href = _HREF.search(tag)
        if "stylesheet" not in tag.lower() or not href:
            return tag
        target = local(base, href.group(2))
        return f"<style>{css(text(target), os.path.dirname(target))}</style>" if target else tag

    def script(m: "re.Match[str]") -> str:
        # The script stays an external script (a data: address), so defer, async and type=module keep their timing:
        # an inline script would run at once, before the elements a deferred script expects exist.
        target = local(base, m.group(3))
        if not target:
            return m.group(0)
        with open(target, "rb") as fh:
            code = base64.b64encode(fh.read()).decode("ascii")
        return (f"<script{m.group(1)}src={m.group(2)}data:text/javascript;charset=utf-8;base64,{code}{m.group(2)}"
                f"{m.group(4)}></script>")

    def src(m: "re.Match[str]") -> str:
        target = local(base, m.group(3))
        return f"{m.group(1)}{m.group(2)}{data_uri(target)}{m.group(2)}" if target else m.group(0)

    html = _LINK.sub(link, html)
    html = _SCRIPT.sub(script, html)
    html = _SRC.sub(src, html)
    return re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", lambda m: m.group(1) + css(m.group(2), base) + m.group(3), html,
                  flags=re.IGNORECASE | re.DOTALL)


OPERATIONS = {"order_create": order_create, "order_update": order_update, "orders": orders, "order": order,
              "plan_set": plan_set, "job": job, "piece_add": piece_add, "piece_check": piece_check,
              "piece_defect": piece_defect, "gate_open": gate_open, "gate_split": gate_split,
              "gate_decide": gate_decide, "order_decisions": order_decisions, "order_undo": order_undo,
              "defect_close": defect_close}
WRITE_OPS = ("order_create", "order_update", "plan_set", "piece_add", "piece_check", "piece_defect", "defect_close",
             "gate_open", "gate_split", "gate_decide", "order_undo")
READ_ROUTES = ("orders", "order", "job", "order_decisions")
#: The person's decision has no tool: only their pages call it.
NO_TOOL = ("gate_decide",)


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    order_id = {"type": "integer", "minimum": 1, "description": "The order's id (id in hub_agora_orders)."}
    piece_id = {"type": "integer", "minimum": 1, "description": "The piece's id (in hub_agora_order)."}
    ids = {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": MAX_ITEMS}
    texts = {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 60}
    ref = {"type": "object", "properties": {**{k: {"type": "string", "maxLength": 1000} for k in
                                               ("app", "object", "place", "path", "url", "note")},
                                             **order_dossier.MATERIAL_PROPERTIES}, "additionalProperties": False}
    kind = {"type": "string", "maxLength": 40, "description": "Piece kind: " + ", ".join(PIECE_KINDS) + " or another short slug."}
    undo = " Returns undo: pass it to hub_agora_order_undo to put back what it changed."
    job_item = {"type": "object", "additionalProperties": False, "properties": {
        "key": {"type": "string", "maxLength": 40, "description": "A name for this job inside the call, for after."},
        "task_id": {"type": "integer", "minimum": 1, "description": "Change an existing job instead of adding one."},
        "title": {"type": "string", "maxLength": 300}, "body": {"type": "string", "maxLength": 20000},
        "kind": {"type": "string", "maxLength": 20},
        "station": {"type": "string", "maxLength": 120, "description": "The Hoard (app id) that does it."},
        "repo": {"type": "string", "maxLength": 120, "description": "Repository name. Defaults to the station's discovered repository."},
        "skill": {"type": "string", "maxLength": 200}, "inputs": ids, "produces": {"type": "array", "items": kind, "maxItems": 20},
        "check": {"type": "string", "maxLength": 4000, "description": "How its output is checked."},
        "assignee": {"type": "object", "properties": {"agent": {"type": "string"}, "model": {"type": "string"},
                                                     "machine": {"type": "string"}}, "additionalProperties": False},
        "after": {"type": "array", "items": {"type": ["string", "integer"]}, "maxItems": 50,
                  "description": "Jobs it waits for: keys of this call or task ids."},
        "branch": {"type": "string", "maxLength": 80}}}
    return [
        {"name": "hub_agora_orders", "read": True,
         "description": "Production orders of a space (all_spaces for every one): status, job and piece counts, "
                        "thumbnails, and the gates waiting for the person's decision.",
         "inputSchema": schema({"status": {"type": "string", "enum": list(ORDER_STATES)}, "all_spaces": {"type": "boolean"},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, [])},
        {"name": "hub_agora_order", "read": True,
         "description": "One order whole: the person's words and references, the plan with each job's task state, every "
                        "piece with checks, defects and what derives from it, the gates and the decisions.",
         "inputSchema": schema({"order_id": order_id}, ["order_id"])},
        {"name": "hub_agora_order_create",
         "description": "Create an order with starting materials (refs), deliverables, details to keep and an optional "
                        "dossier (audience, constraints, native context)." + undo,
         "inputSchema": schema({"agent": agent_prop, "title": {"type": "string", "maxLength": 200},
                                "words": {"type": "string", "maxLength": TEXT_MAX}, "refs": {"type": "array", "items": ref, "maxItems": 50},
                                "dossier": order_dossier.DOSSIER_INPUT,
                                "deliverables": texts, "template": {"type": "string", "maxLength": 80}, "keep": texts,
                                "folder": {"type": "string", "maxLength": 1000}, "deadline": {"type": "number"}}, ["agent"])},
        {"name": "hub_agora_order_update",
         "description": "Change an order, its dossier or its refs. Resupplying refs fingerprints the current local materials." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id, "title": {"type": "string", "maxLength": 200},
                                "words": {"type": "string", "maxLength": TEXT_MAX}, "refs": {"type": "array", "items": ref, "maxItems": 50},
                                "dossier": order_dossier.DOSSIER_INPUT,
                                "deliverables": texts, "template": {"type": "string", "maxLength": 80}, "keep": texts,
                                "folder": {"type": "string", "maxLength": 1000}, "deadline": {"type": ["number", "null"]},
                                "status": {"type": "string", "enum": list(ORDER_STATES)}}, ["agent", "order_id"])},
        {"name": "hub_agora_plan_set",
         "description": "Compose or recompose an order's plan. Each new job becomes an Ágora task of the space with its "
                        "station, input pieces, kinds to deliver, check and assignee; after sets what it waits for "
                        "(parallel branches wait on the same job). task_id changes a job; remove drops jobs." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id,
                                "jobs": {"type": "array", "items": job_item, "maxItems": 60}, "remove": ids}, ["agent", "order_id"])},
        {"name": "hub_agora_job", "read": True,
         "description": "Everything to start a job (task_id): the order's dossier, station and check, "
                        "input pieces, destination requirements, pinned details, kept references, defects and an "
                        "earlier kept piece of each kind it delivers.",
         "inputSchema": schema({"task_id": {"type": "integer", "minimum": 1}}, ["task_id"])},
        {"name": "hub_agora_piece_add",
         "description": "Register a piece of an order: kind, title, path (relative to the order's folder or absolute), "
                        "url, or app + object + place (+ source_rev); previews (e.g. turntable frames); task_id of the "
                        "job that made it, model, derives_from (pieces in this space, role: material|reference) and keep (the "
                        "order's details by default). Files get a content print: the same output again returns the "
                        "same piece; a changed file at the same path is its next revision." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id, "kind": kind,
                                "title": {"type": "string", "maxLength": 200}, "path": {"type": "string", "maxLength": 1000},
                                "url": {"type": "string", "maxLength": 2000}, "app": {"type": "string", "maxLength": 300},
                                "object": {"type": "string", "maxLength": 300}, "place": {"type": "string", "maxLength": 300},
                                "previews": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 24},
                                "task_id": {"type": "integer", "minimum": 1}, "station": {"type": "string", "maxLength": 120},
                                "by": {"type": "string", "maxLength": 80, "description": "Who made it, when not you."},
                                "model": {"type": "string", "maxLength": 120}, "keep": texts,
                                "derives_from": {"type": "array", "maxItems": MAX_ITEMS, "items": {"anyOf": [
                                    {"type": "integer", "minimum": 1},
                                    {"type": "object", "additionalProperties": False, "required": ["piece"], "properties": {
                                        "piece": {"type": "integer", "minimum": 1}, "role": {"type": "string", "enum": list(ROLES)},
                                        "note": {"type": "string", "maxLength": 300}}}]}},
                                "supersedes": {"type": "integer", "minimum": 1, "description": "The piece this one replaces."},
                                "source_rev": {"type": "string", "maxLength": 200, "description": "Revision of an app object."},
                                "note": {"type": "string", "maxLength": 4000}}, ["agent", "order_id", "kind", "title"])},
        {"name": "hub_agora_piece_check",
         "description": "Note a check of a piece: what you looked at (note), ok, and per detail to keep (by id or "
                        "detail) its state, evidence and an optional anchor to a registered reference. Passing with "
                        "no detail missing or unchecked marks it checked." + undo,
         "inputSchema": schema({"agent": agent_prop, "piece_id": piece_id, "note": {"type": "string", "maxLength": TEXT_MAX},
                                "ok": {"type": "boolean"},
                                "keep": {"type": "array", "maxItems": 60, "items": {"type": "object", "additionalProperties": False,
                                         "properties": {"id": {"type": "string", "maxLength": 20},
                                                        "detail": {"type": "string", "maxLength": 300}, "ok": {"type": "boolean"},
                                                        "state": {"type": "string", "enum": list(DETAIL_STATES[1:])},
                                                        "anchor": order_destinations.ANCHOR_SCHEMA,
                                                        "how": {"type": "string", "maxLength": 1000},
                                                        "evidence": {"type": "string", "maxLength": 1000},
                                                        "note": {"type": "string", "maxLength": 1000}}}}},
                               ["agent", "piece_id", "note"])},
        {"name": "hub_agora_piece_defect",
         "description": "Send a reproducible defect of a piece back to the job that caused it (task_id, default the "
                        "job that made it): note, expected, observed, evidence, and view, pose, at (second) or step "
                        "(slide, block, size). Its task hears it in its thread." + undo,
         "inputSchema": schema({"agent": agent_prop, "piece_id": piece_id, "note": {"type": "string", "maxLength": TEXT_MAX},
                                "view": {"type": "string", "maxLength": 300}, "pose": {"type": "string", "maxLength": 300},
                                "at": {"type": "string", "maxLength": 300}, "step": {"type": "string", "maxLength": 300},
                                "expected": {"type": "string", "maxLength": 4000}, "observed": {"type": "string", "maxLength": 4000},
                                "evidence": {"type": "string", "maxLength": 4000},
                                "task_id": {"type": "integer", "minimum": 1}}, ["agent", "piece_id", "note"])},
        {"name": "hub_agora_defect_close",
         "description": "Close a defect when the same check passes again: fixed_in is the piece (new revision) where "
                        "it was repeated, note what was checked." + undo,
         "inputSchema": schema({"agent": agent_prop, "defect_id": {"type": "integer", "minimum": 1},
                                "fixed_in": {"type": "integer", "minimum": 1}, "note": {"type": "string", "maxLength": TEXT_MAX}},
                               ["agent", "defect_id", "note"])},
        {"name": "hub_agora_gate_open",
         "description": "Show pieces of an order to the person for one keep-or-cancel decision, made on their page; the "
                        "order waits for it. Pieces not yet decided can keep moving as provisional." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id, "piece_ids": ids,
                                "title": {"type": "string", "maxLength": 200}, "note": {"type": "string", "maxLength": TEXT_MAX}},
                               ["agent", "order_id", "piece_ids"])},
        {"name": "hub_agora_gate_split",
         "description": "Take one piece out of an open gate into its own gate, to be decided alone." + undo,
         "inputSchema": schema({"agent": agent_prop, "gate_id": {"type": "integer", "minimum": 1}, "piece_id": piece_id},
                               ["agent", "gate_id", "piece_id"])},
        {"name": "hub_agora_order_decisions", "read": True,
         "description": "The person's keep/cancel decisions in a space or one order, newest first, each with its "
                        "situation (order, deadline, the other pieces on the table).",
         "inputSchema": schema({"order_id": order_id, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, [])},
        {"name": "hub_agora_order_undo",
         "description": "Undo one write of orders, plans, pieces, checks, defects or gates (token: the undo it returned). "
                        "Tasks it created are dropped.",
         "inputSchema": schema({"agent": agent_prop, "token": {"type": "string", "maxLength": 40}}, ["agent", "token"])},
    ]
