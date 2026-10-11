"""Ágora: where the agents that build the family (one per chat or session) and the person coordinate their work.

Several coding agents work on the same repositories and the same machine at once. Without a shared place they
step on each other: one edits a file the other has half-changed, both restart the same app, two live evaluations
fight for the one principal model, and a disagreement about a design ends up as two contradictory commits. The hub
is the long-lived local process every agent can reach (HTTP, the MCP bridge, or ``scripts/agora.py``), so the
Ágora lives here:

* **agents** announce themselves with a heartbeat (what they are doing, whether they are working, waiting or away);
* **tasks** go ``open → claimed → in_progress → review → approved → done`` (also ``changes``, ``blocked``,
  ``dropped``). Claiming a task takes its **locks** at the same time, all or nothing;
* **locks** are leases on resources with a time-to-live that the owner's heartbeat renews:
  ``path:<Repo>/<file or dir>`` (editing; prefixes overlap), ``repo:<Repo>`` (the whole repo), ``merge:<Repo>``
  (integrating into the shared checkout / main branch), and exact names such as ``model:principal``, ``gpu:2``,
  ``port:7001`` or ``app:lumiere``. A lock whose owner stops renewing it expires by itself;
* **threads** carry the conversation: every task has one, and agents open ``debate``, ``question``, ``decision``,
  ``review`` or ``handoff`` threads. Messages can ``agree`` / ``disagree``; a thread is resolved with a written
  resolution (resolved threads are the **decision log**) or **escalated** to the person, who is notified and
  answers from the hub's page;
* every agent has an **inbox**: messages from others since its last read, reviews waiting for it, changes asked of
  it and answers to escalations. ``wait_s`` turns the inbox into a long poll so an agent can wait for a reply.

Only the hub's own page may write as the person (``luis``); agents use their own ids. Everything is stored in
``<data>/agora.db`` and announced on the event bus as ``agora.*``.

HTTP (reads): ``GET /api/agora/board|inbox|tasks|tasks/<id>|threads|threads/<id>|locks|decisions|agents``.
HTTP (writes): ``POST /api/agora/<op>`` with the same arguments as the tool ``hub_agora_<op>``.
``POST /api/agora/sync`` is the agent resume call: it heartbeats, peeks without acknowledging, and returns posts after
the caller's durable message-id cursor. The caller stores ``next_since_id`` and sends it back on the next call.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..sqlkit import Database
from . import agent_watch
from .facets import Facet, Request

# code_peek refs: path/to/file.py#L10-L24@abc1234 (commit optional when the submission lists commits)
_CODE_PEEK_RE = re.compile(
    r"^(?P<path>[^#]+)#L(?P<start>\d+)(?:-L(?P<end>\d+))?(?:@(?P<commit>[0-9a-fA-F]{7,40}))?$"
)
_BLOCK_KINDS = frozenset({"what_why", "paths", "code_peek", "flow", "sequence"})
_GIT_TIMEOUT_S = 15.0
CREATE_NO_WINDOW = 0x08000000

PERSON = "luis"
STALE_AGENT_S = 6 * 3600.0
#: Silence (no heartbeat) after which another agent may take over the work of an agent that stopped answering.
HANDOVER_IDLE_S = 30 * 60.0
#: The coordination thread where a handover is announced (a new note thread is opened when it does not exist).
HANDOVER_THREAD_ID = 32
DEFAULT_LOCK_TTL_S = 3 * 3600
MAX_LOCK_TTL_S = 24 * 3600
REVIEW_GRACE_S = 2 * 3600
MAX_WAIT_S = 120.0

TASK_STATUSES = ("open", "claimed", "in_progress", "review", "changes", "approved", "blocked", "done", "dropped")
ACTIVE = ("claimed", "in_progress", "review", "changes", "approved", "blocked")
FINAL = ("done", "dropped")
TASK_KINDS = ("feature", "bug", "research", "review", "chore", "eval", "docs")
THREAD_KINDS = ("task", "debate", "question", "decision", "review", "handoff", "note")
THREAD_STATUSES = ("open", "resolved", "escalated")
MSG_KINDS = ("comment", "proposal", "agree", "disagree", "approve", "changes", "resolution", "escalation", "system")
AGENT_STATES = ("working", "idle", "waiting", "away")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,40}$")
_SCOPED = ("path", "repo", "merge")

MIGRATIONS = [
    """
    CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, kind TEXT, note TEXT, state TEXT, doing TEXT,
                         last_seen REAL, created REAL);
    CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT, repo TEXT, paths TEXT,
                        kind TEXT, priority INTEGER, status TEXT, created_by TEXT, owner TEXT, reviewer TEXT,
                        branch TEXT, commits TEXT, result TEXT, reviewed INTEGER DEFAULT 0, thread_id INTEGER,
                        created REAL, updated REAL, claimed_at REAL, submitted_at REAL, closed_at REAL);
    CREATE TABLE locks (resource TEXT PRIMARY KEY, owner TEXT, task_id INTEGER, note TEXT, ttl_s INTEGER,
                        acquired REAL, expires REAL);
    CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT, task_id INTEGER,
                          status TEXT, created_by TEXT, resolution TEXT, resolved_by TEXT, created REAL, updated REAL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER NOT NULL, author TEXT, kind TEXT,
                           body TEXT, mentions TEXT, created REAL);
    CREATE TABLE reads (agent TEXT PRIMARY KEY, last_msg_id INTEGER DEFAULT 0, updated REAL);
    CREATE INDEX messages_thread ON messages(thread_id, id);
    CREATE INDEX tasks_status ON tasks(status, priority);
    """,
    """
    ALTER TABLE tasks ADD COLUMN review_state TEXT;
    CREATE TABLE acks (agent TEXT NOT NULL, thread_id INTEGER NOT NULL, upto INTEGER NOT NULL, updated REAL,
                       PRIMARY KEY(agent, thread_id));
    UPDATE tasks SET review_state='approved' WHERE status='done' AND reviewed=1;
    UPDATE tasks SET review_state='unreviewed' WHERE status='done' AND reviewed=0;
    """,
    """
    ALTER TABLE tasks ADD COLUMN reviewed_commits TEXT;
    UPDATE tasks SET reviewed_commits=commits WHERE reviewed=1 AND status='approved';
    """,
    """
    CREATE TABLE task_checkpoints (task_id INTEGER NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
                                  author TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL,
                                  PRIMARY KEY(task_id, revision));
    """,
    """
    ALTER TABLE tasks ADD COLUMN submission_revision INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN reviewed_submission_revision INTEGER;
    UPDATE tasks SET submission_revision=1
        WHERE submitted_at IS NOT NULL OR (reviewed=1 AND status IN ('approved', 'done'));
    UPDATE tasks SET reviewed_submission_revision=1
        WHERE reviewed=1 AND status IN ('approved', 'done');
    """,
    """
    ALTER TABLE tasks ADD COLUMN submission_blocks TEXT;
    """,
    """
    ALTER TABLE task_checkpoints ADD COLUMN message_id INTEGER;
    """,
    """
    CREATE TABLE agora_usage_pools (
        provider_id TEXT NOT NULL, pool_id TEXT NOT NULL, pool_label TEXT NOT NULL,
        source TEXT NOT NULL, reported_at REAL NOT NULL, windows_json TEXT NOT NULL,
        PRIMARY KEY(provider_id, pool_id));
    CREATE TABLE agora_usage_reporters (
        provider_id TEXT NOT NULL, pool_id TEXT NOT NULL, agent_id TEXT NOT NULL, last_reported_at REAL NOT NULL,
        PRIMARY KEY(provider_id, pool_id, agent_id),
        FOREIGN KEY(provider_id, pool_id) REFERENCES agora_usage_pools(provider_id, pool_id) ON DELETE CASCADE);
    CREATE INDEX agora_usage_reporters_agent ON agora_usage_reporters(agent_id);
    """,
    """
    CREATE TABLE agora_orphan_findings (key TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id INTEGER, created REAL NOT NULL);
    """,
    """
    CREATE TABLE agora_task_deps (task_id INTEGER NOT NULL, blocker_id INTEGER NOT NULL, created_by TEXT, created REAL,
                                  PRIMARY KEY(task_id, blocker_id));
    CREATE INDEX agora_task_deps_blocker ON agora_task_deps(blocker_id);
    CREATE TABLE agora_dep_wakeups (key TEXT PRIMARY KEY, task_id INTEGER NOT NULL, created REAL NOT NULL);
    """,
]
#: A blocker stops holding its dependents back once it reaches one of these.
DEP_CLEAR = ("approved", "done", "dropped")
#: Kinds whose work may be closed without a cross review (recorded as «exenta», not as «sin revisión»).
EXEMPT_KINDS = ("docs", "eval", "research", "chore")


# A1 only informs: the Ágora never blocks an agent for not having read a thread (Luis, 09-10: no more
# obstacles for the agents' autonomy). "warn" lists what was unread in the answer; "off" says nothing.
FRESHNESS_MODES = ("off", "warn")


class AgoraError(ValueError):
    """A refused operation (bad argument, conflict, not allowed): the caller gets ``ok: False`` and the reason."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ---- resources -------------------------------------------------------------------------------------------------

def normalize_resource(resource: Any) -> str:
    """``Path:Faustus\\src\\x.py`` → ``path:faustus/src/x.py``. Scoped kinds compare case-insensitively (Windows)."""
    raw = str(resource or "").strip()
    if ":" not in raw:
        raise AgoraError(f"resource must look like kind:name (path:Repo/file, repo:Repo, merge:Repo, model:principal): {raw!r}")
    kind, name = raw.split(":", 1)
    kind = kind.strip().lower()
    name = name.strip().replace("\\", "/")
    if not re.fullmatch(r"[a-z][a-z0-9_\-]{0,20}", kind) or not name or len(name) > 400:
        raise AgoraError(f"invalid resource: {raw!r}")
    if kind in _SCOPED:
        name = re.sub(r"/{2,}", "/", name).lower().strip()
        if kind in ("repo", "merge"):
            name = name.strip("/")
            if "/" in name:
                raise AgoraError(f"{kind}: takes a repository name, not a path: {raw!r}")
        else:
            name = name.lstrip("./")
            if "/" not in name.rstrip("/"):
                name = name.rstrip("/") + "/"          # path:Repo means the whole tree of Repo
        if ".." in name.split("/"):
            raise AgoraError(f"invalid resource: {raw!r}")
    return f"{kind}:{name}"


def _repo_of(resource: str) -> str:
    kind, name = resource.split(":", 1)
    return name.split("/", 1)[0] if kind == "path" else name


def _paths_overlap(a: str, b: str) -> bool:
    if any(c in a + b for c in "*?["):
        return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a.rstrip("/") + "/x", b) \
            or fnmatch.fnmatch(b.rstrip("/") + "/x", a)
    if a == b:
        return True
    a_dir, b_dir = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a_dir.startswith(b_dir) or b_dir.startswith(a_dir)


def conflicts(a: str, b: str) -> bool:
    """Whether two normalised resources can not be held by two owners at once."""
    ka, na = a.split(":", 1)
    kb, nb = b.split(":", 1)
    if ka in _SCOPED and kb in _SCOPED:
        if _repo_of(a) != _repo_of(b):
            return False
        kinds = {ka, kb}
        if "repo" in kinds:
            return True                          # the whole repository excludes everything in it
        if kinds == {"path"}:
            return _paths_overlap(na, nb)
        if kinds == {"merge"}:
            return True
        return False                              # editing a file never blocks someone integrating something else
    return a == b


def group_locks(locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold a long list of locks into one line per owner, task and resource family.

    ``path:faustus/src/a.py`` … ``path:faustus/tests/b.py`` held by one agent for one task become
    ``{owner, task_id, kind: "path", repo: "faustus", count: 36, folders: {"src/": 13, "tests/": 15, …}}``;
    exact resources (``model:principal``) stay one per group. ``resources`` keeps the full list."""
    groups: dict[tuple, dict[str, Any]] = {}
    for lk in locks:
        res = str(lk.get("resource") or "")
        kind, _, name = res.partition(":")
        repo = _repo_of(res) if kind in _SCOPED else name
        key = (lk.get("owner"), lk.get("task_id"), kind, repo if kind in _SCOPED else res)
        g = groups.setdefault(key, {"owner": lk.get("owner"), "task_id": lk.get("task_id"), "kind": kind,
                                    "repo": repo if kind in _SCOPED else "", "count": 0, "folders": {},
                                    "resources": [], "expires": lk.get("expires"), "note": lk.get("note")})
        g["count"] += 1
        g["resources"].append(res)
        g["expires"] = max(g["expires"] or 0, lk.get("expires") or 0)
        if kind == "path":
            rest = name.split("/", 1)[1] if "/" in name else ""
            top = rest.split("/", 1)[0] + "/" if "/" in rest else (rest or "/")
            g["folders"][top] = g["folders"].get(top, 0) + 1
    out = []
    for g in groups.values():
        if g["kind"] == "path":
            parts = sorted(g["folders"].items(), key=lambda kv: (-kv[1], kv[0]))
            folders = ", ".join(f"{k} {v}" for k, v in parts[:4]) + (", …" if len(parts) > 4 else "")
            g["label"] = (g["resources"][0] if g["count"] == 1 else
                          f"path:{g['repo']}/ · {g['count']} rutas ({folders})")
        else:
            g["label"] = g["resources"][0] if g["count"] == 1 else f"{g['kind']}:{g['repo']} · {g['count']}"
        out.append(g)
    out.sort(key=lambda g: (str(g["owner"]), g["task_id"] or 0, g["kind"], g["repo"]))
    return out


# ---- store ---------------------------------------------------------------------------------------------------

def _j(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _l(value: Any) -> list:
    try:
        out = json.loads(value) if value else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _txt(value: Any, limit: int, field: str, *, required: bool = False) -> str:
    out = str(value if value is not None else "").strip()
    if required and not out:
        raise AgoraError(f"{field} is required")
    if len(out) > limit:
        raise AgoraError(f"{field} is longer than {limit} characters")
    return out


def _list(value: Any, field: str, limit: int = 50) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\n]", value) if v.strip()]
    if not isinstance(value, list) or len(value) > limit:
        raise AgoraError(f"{field} must be a list (at most {limit})")
    return [str(v).strip() for v in value if str(v).strip()]


# Optional test hook run inside ``_git_show`` (e.g. concurrent writes while verify is slow).
_GIT_SHOW_HOOK: Optional[Callable[[], None]] = None


def _git_run(repo: Path, *args: str, timeout: float = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    argv = [git, "--no-optional-locks", "-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=utf-8",
            "-C", str(repo), *args]
    kw: dict[str, Any] = {
        "capture_output": True, "timeout": timeout,
        "encoding": "utf-8", "errors": "strict",
        "env": {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"},
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(argv, **kw)


def _git_show(repo: Path, commit: str, rel_path: str) -> str:
    """Read ``rel_path`` at ``commit`` from ``repo`` (read-only, UTF-8)."""
    if _GIT_SHOW_HOOK is not None:
        _GIT_SHOW_HOOK()
    try:
        res = _git_run(repo, "show", f"{commit}:{rel_path.replace(chr(92), '/')}")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise AgoraError(f"code_peek: git failed for {rel_path}@{commit}: {exc}") from exc
    if res.returncode != 0:
        err = ((res.stderr or res.stdout or "git show failed") or "").strip().splitlines()
        raise AgoraError(f"code_peek: cannot read {rel_path}@{commit}: {err[0] if err else 'unknown'}")
    if res.stdout is None:
        raise AgoraError(f"code_peek: empty git output for {rel_path}@{commit}")
    return res.stdout


def _normalize_repo_relpath(raw: str) -> str:
    """Normalize a repo-relative path without ``str.lstrip('./')`` (which eats ``...`` prefixes)."""
    rel = str(raw or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or rel.startswith("../") or "/../" in f"/{rel}/":
        raise AgoraError(f"code_peek path is invalid: {raw!r}")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise AgoraError(f"code_peek path is invalid: {raw!r}")
    return "/".join(parts)


def _slice_lines(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    if start < 1 or end < start or end > len(lines):
        raise AgoraError(f"code_peek: lines L{start}-L{end} out of range (file has {len(lines)} lines)")
    return "\n".join(lines[start - 1:end])


def _git_is_ancestor(repo: Path, maybe_ancestor: str, commit: str) -> bool:
    try:
        res = _git_run(repo, "merge-base", "--is-ancestor", maybe_ancestor, commit, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return False
    return res.returncode == 0


def _resolve_peek_commit(
        explicit: Optional[str], commits: list[str], *, roots: Optional[list[Path]] = None
) -> tuple[str, str]:
    """Return ``(commit, how)`` where ``how`` is ``explicit``, ``sole`` or ``tip``.

    Without ``@commit``, a multi-commit submission uses the unique tip (every other commit is an
    ancestor). Parallel tips require an explicit ``@commit``.
    """
    if explicit:
        return explicit, "explicit"
    if not commits:
        raise AgoraError("code_peek needs @commit or submission commits")
    if len(commits) == 1:
        return commits[0], "sole"
    roots = [Path(r) for r in (roots or []) if r and _is_git_checkout(Path(r))]
    if not roots:
        raise AgoraError("code_peek without @commit needs a git checkout to resolve the tip among commits")

    def reachable(cand: str, other: str) -> bool:
        return any(_git_is_ancestor(root, other, cand) for root in roots)

    tips = [
        cand for cand in commits
        if all(c == cand or reachable(cand, c) for c in commits)
    ]
    # Prefer the first tip in list order only when unique.
    if len(tips) != 1:
        raise AgoraError(
            "code_peek without @commit needs a unique tip among submission commits "
            "(every other commit an ancestor); pass path#Lx-Ly@commit explicitly"
        )
    return tips[0], "tip"


def _is_git_checkout(path: Path) -> bool:
    return path.is_dir() and ((path / ".git").exists() or (path / ".git").is_file())


def _git_common_dir(repo: Path) -> Optional[Path]:
    try:
        res = _git_run(repo, "rev-parse", "--git-common-dir", timeout=5.0)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    raw = res.stdout.strip()
    p = Path(raw)
    return (repo / p).resolve() if not p.is_absolute() else p.resolve()


def _git_origin(repo: Path) -> Optional[str]:
    try:
        res = _git_run(repo, "remote", "get-url", "origin", timeout=5.0)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    return res.stdout.strip().rstrip("/").lower()


def _same_repo_identity(primary: Path, other: Path) -> bool:
    """True when ``other`` is the same repository as ``primary`` (common dir or same origin URL)."""
    if not _is_git_checkout(other):
        return False
    ca, cb = _git_common_dir(primary), _git_common_dir(other)
    if ca and cb and ca == cb:
        return True
    oa, ob = _git_origin(primary), _git_origin(other)
    return bool(oa and ob and oa == ob)


def _git_worktrees(primary: Path) -> list[Path]:
    try:
        res = _git_run(primary, "worktree", "list", "--porcelain", timeout=10.0)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return []
    if res.returncode != 0 or not res.stdout:
        return []
    out: list[Path] = []
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line.split(" ", 1)[1].strip())
            if p.is_dir():
                out.append(p.resolve())
    return out


def resolve_code_peek_roots(name: str, *, primary: Optional[Path], checkout: Optional[str],
                            repo_roots: Optional[Callable[[str], list[Path]]] = None) -> list[Path]:
    """Roots that may hold the commit object: registered checkout, its worktrees, optional verified clone."""
    roots: list[Path] = []
    if repo_roots is not None:
        for p in repo_roots(name) or []:
            if p and _is_git_checkout(Path(p)):
                roots.append(Path(p).resolve())
    if primary and _is_git_checkout(primary):
        roots.append(Path(primary).resolve())
        roots.extend(_git_worktrees(Path(primary)))
    # Deduplicate preserving order
    seen: set[str] = set()
    ordered: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            ordered.append(r)
    if not ordered:
        raise AgoraError(f"code_peek: unknown repo {name!r}")
    primary_root = ordered[0]
    if checkout:
        hint = Path(str(checkout).strip()).expanduser()
        if not hint.is_absolute():
            raise AgoraError("code_peek checkout must be an absolute path")
        hint = hint.resolve()
        if not _same_repo_identity(primary_root, hint):
            raise AgoraError(
                f"code_peek: checkout {hint} is not the same repository as {name!r} "
                "(common git dir or origin URL must match)")
        ordered = [hint] + [r for r in ordered if r != hint]
    return ordered


def verify_code_peek(ref: str, *, roots: list[Path], commits: list[str]) -> dict[str, Any]:
    """Verify ``path#Lx-Ly@commit`` against the first root that has the object; return the peek payload."""
    m = _CODE_PEEK_RE.match(str(ref or "").strip())
    if not m:
        raise AgoraError("code_peek must look like path/file.py#L10-L24@commit")
    rel = _normalize_repo_relpath(m.group("path"))
    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if not roots:
        raise AgoraError("code_peek: no repository roots to search")
    commit, how = _resolve_peek_commit(m.group("commit"), commits, roots=roots)
    last: Optional[AgoraError] = None
    used: Optional[Path] = None
    text = ""
    for root in roots:
        if not _is_git_checkout(root):
            continue
        try:
            text = _slice_lines(_git_show(root, commit, rel), start, end)
            used = root
            break
        except AgoraError as exc:
            last = exc
            continue
    if used is None:
        raise last or AgoraError(f"code_peek: cannot read {rel}@{commit} in any registered checkout")
    return {"ref": f"{rel}#L{start}-L{end}@{commit}", "path": rel, "start": start, "end": end,
            "commit": commit, "commit_source": how, "text": text, "verified": True, "checkout": str(used)}


def normalize_submission_blocks(
        raw: Any, *, repo_name: str, commits: list[str],
        repo_lookup: Optional[Callable[[str], Optional[Path]]] = None,
        repo_roots: Optional[Callable[[str], list[Path]]] = None) -> Optional[list[dict[str, Any]]]:
    """Validate optional structured submission blocks; verify every ``code_peek`` against git (no DB lock)."""
    if raw is None or raw == "" or raw == []:
        return None
    if not isinstance(raw, list) or len(raw) > 40:
        raise AgoraError("blocks must be a list of at most 40 objects")
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AgoraError(f"blocks[{i}] must be an object")
        kind = str(item.get("kind") or "").strip()
        if kind not in _BLOCK_KINDS:
            raise AgoraError(f"blocks[{i}].kind must be one of {sorted(_BLOCK_KINDS)}")
        if "id" in item and not str(item.get("id") or "").strip():
            raise AgoraError(f"blocks[{i}].id is empty")
        bid = str(item.get("id") or f"b{i + 1}").strip()[:80]
        if not bid or bid in seen_ids:
            raise AgoraError(f"blocks[{i}].id must be unique and non-empty (got {bid!r})")
        seen_ids.add(bid)
        title = _txt(item.get("title"), 200, f"blocks[{i}].title")
        block: dict[str, Any] = {"id": bid, "kind": kind, "title": title}
        if kind == "what_why":
            block["body"] = _txt(item.get("body"), 8000, f"blocks[{i}].body", required=True)
        elif kind == "paths":
            paths = _list(item.get("paths"), f"blocks[{i}].paths", 80)
            if not paths:
                raise AgoraError(f"blocks[{i}].paths is required")
            block["paths"] = paths
        elif kind in ("flow", "sequence"):
            steps = item.get("steps")
            if not isinstance(steps, list) or not steps or len(steps) > 40:
                raise AgoraError(f"blocks[{i}].steps must be a non-empty list (at most 40)")
            block["steps"] = [_txt(s, 500, f"blocks[{i}].steps") for s in steps]
            if item.get("body"):
                block["body"] = _txt(item.get("body"), 4000, f"blocks[{i}].body")
        else:  # code_peek
            ref = item.get("peek") or item.get("ref")
            if not ref:
                raise AgoraError(f"blocks[{i}].peek is required (path#Lx-Ly@commit)")
            name = str(item.get("repo") or repo_name or "").strip()
            if not name:
                raise AgoraError(f"blocks[{i}]: set repo on the task or on the block for code_peek")
            if repo_lookup is None and repo_roots is None:
                raise AgoraError("code_peek verification needs a repository lookup (hub repos)")
            primary = repo_lookup(name) if repo_lookup else None
            roots = resolve_code_peek_roots(
                name, primary=Path(primary) if primary else None,
                checkout=str(item["checkout"]) if item.get("checkout") else None,
                repo_roots=repo_roots)
            peek = verify_code_peek(str(ref), roots=roots, commits=commits)
            block["repo"] = name
            block["peek"] = peek
            if item.get("checkout"):
                block["checkout"] = str(Path(str(item["checkout"]).strip()).expanduser().resolve())
            if item.get("body"):
                block["body"] = _txt(item.get("body"), 4000, f"blocks[{i}].body")
        out.append(block)
    return out


def _format_blocks_for_message(blocks: list[dict[str, Any]]) -> str:
    lines = ["", "Structured review blocks:"]
    for b in blocks:
        head = f"- [{b['id']}] {b['kind']}" + (f": {b['title']}" if b.get("title") else "")
        lines.append(head)
        if b["kind"] == "code_peek":
            p = b["peek"]
            lines.append(f"  peek {p['ref']} ({'verified' if p.get('verified') else 'unverified'})")
            preview = "\n".join((p.get("text") or "").splitlines()[:12])
            if preview:
                lines.append("  ```")
                lines.append(preview)
                lines.append("  ```")
        elif b["kind"] == "paths":
            lines.append("  " + ", ".join(b.get("paths") or []))
        elif b["kind"] in ("flow", "sequence"):
            for step in b.get("steps") or []:
                lines.append(f"  → {step}")
        elif b.get("body"):
            lines.append("  " + b["body"][:500].replace("\n", "\n  "))
    return "\n".join(lines)


CHECKPOINT_MAX_BYTES = 65536
CHECKPOINT_FIELDS = {"summary": 8000, "workspace": 2000, "branch": 500, "base_head": 64, "head": 64}


def _checkpoint_int(value: Any, field: str, minimum: int = 0, maximum: int = 9_223_372_036_854_775_807) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgoraError(f"{field} must be an integer in {minimum}..{maximum}")
    return value


def _checkpoint_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise AgoraError(f"{field} must be a string of at most {limit} characters" + (" (non-empty)" if required else ""))
    return value


def _checkpoint_payload(value: Any) -> dict[str, Any]:
    """Declared evidence only: paths, hashes and tests are never inspected or executed."""
    if not isinstance(value, dict) or set(value) - (set(CHECKPOINT_FIELDS) | {"next_steps", "tests", "artifacts"}):
        raise AgoraError("payload must be an object with only checkpoint fields")
    out = {"summary": _checkpoint_text(value.get("summary"), "summary", 8000, required=True)}
    for key, limit in CHECKPOINT_FIELDS.items():
        if key in value:
            out[key] = _checkpoint_text(value[key], key, limit)
            if key in ("base_head", "head") and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", out[key]):
                raise AgoraError(f"{key} must be a full 40 or 64 digit hexadecimal SHA")
    for key in ("next_steps", "artifacts"):
        if key in value:
            items = value[key]
            if not isinstance(items, list) or len(items) > 50:
                raise AgoraError(f"{key} must be a list of at most 50 strings")
            out[key] = [_checkpoint_text(item, key, 2000) for item in items]
    if "tests" in value:
        tests = value["tests"]
        if not isinstance(tests, list) or len(tests) > 50:
            raise AgoraError("tests must be a list of at most 50 objects")
        out["tests"] = []
        for test in tests:
            if not isinstance(test, dict) or set(test) != {"name", "status", "evidence"}:
                raise AgoraError("each test requires exactly name, status and evidence")
            name = _checkpoint_text(test["name"], "test.name", 300, required=True)
            status = _checkpoint_text(test["status"], "test.status", 20)
            if status not in ("passed", "failed", "not_run"):
                raise AgoraError("test.status must be passed, failed or not_run")
            out["tests"].append({"name": name, "status": status,
                                 "evidence": _checkpoint_text(test["evidence"], "test.evidence", 4000)})
    try:
        size = len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, UnicodeError):
        raise AgoraError("payload must be valid UTF-8 JSON without NaN") from None
    if size > CHECKPOINT_MAX_BYTES:
        raise AgoraError(f"payload exceeds {CHECKPOINT_MAX_BYTES} UTF-8 JSON bytes")
    return out


# What a metadata-only inbox keeps of each section: who, where, what kind and state, never text written by someone.
_META_MESSAGE = ("id", "thread_id", "author", "kind", "created", "mentions", "for_you", "thread_title", "thread_kind",
                 "thread_status", "task_id", "task_owner", "thread_by")
_META_TASK = ("id", "title", "status", "repo", "kind", "priority", "owner", "reviewer", "created_by", "thread_id",
              "submission_revision", "review_revision", "branch", "commits", "paths", "created", "updated",
              "submitted_at", "closed_at", "reviewed", "review_state")
_META_THREAD = ("id", "title", "kind", "status", "task_id", "created_by", "created", "updated", "escalated_by")


def project_inbox_metadata(out: dict[str, Any]) -> dict[str, Any]:
    """Reduce an inbox answer to metadata: message and task bodies, checkpoint summaries, submission blocks and
    escalation questions become their length. Applied after any enrichment, so nothing added later leaks text."""
    def keep(item: dict[str, Any], fields: tuple[str, ...], texts: dict[str, Any]) -> dict[str, Any]:
        slim = {k: item[k] for k in fields if k in item}
        for name, value in texts.items():
            slim[name] = len(str(value or ""))
        return slim
    for key in ("messages", "pending"):
        out[key] = [keep(m, _META_MESSAGE, {"body_chars": m.get("body")}) for m in out.get(key) or []]
    for key in ("reviews", "changes", "progress_alerts"):
        out[key] = [dict(keep(t, _META_TASK, {"body_chars": t.get("body")}),
                         **({"activity": {k: t["activity"].get(k) for k in ("state", "last_progress_at", "heartbeat_at")}}
                            if isinstance(t.get("activity"), dict) else {}))
                    for t in out.get(key) or []]
    out["escalated"] = [keep(th, _META_THREAD, {"question_chars": th.get("question")}) for th in out.get("escalated") or []]
    out["metadata_only"] = True
    return out


class Agora:
    """The store and every operation. Thread-safe; ``clock`` is injectable for tests."""

    def __init__(self, path: Any, *, clock: Callable[[], float] = time.time,
                 emit: Optional[Callable[[str, dict[str, Any]], Any]] = None,
                 escalate_hook: Optional[Callable[[dict[str, Any], str, str], Any]] = None,
                 review_grace_s: float = REVIEW_GRACE_S, handover_idle_s: float = HANDOVER_IDLE_S,
                 handover_thread_id: Optional[int] = HANDOVER_THREAD_ID,
                 task_idle_s: int = 3600,
                 repo_lookup: Optional[Callable[[str], Optional[Path]]] = None,
                 repo_roots: Optional[Callable[[str], list[Path]]] = None,
                 freshness: Optional[str] = None):
        self.clock = clock
        # A1: when an agent acts on a task without having read what others wrote there since, "warn" (default)
        # says so in the answer and an event; "off" says nothing. Nothing is ever refused for it.
        mode = str(freshness or os.environ.get("HOARD_AGORA_FRESHNESS") or "warn").strip().lower()
        self.freshness = mode if mode in FRESHNESS_MODES else "warn"
        self.task_idle_s = _checkpoint_int(task_idle_s, "task_idle_s", 60, 7 * 86400)
        self.handover_idle_s = float(handover_idle_s)
        self.handover_thread_id = handover_thread_id
        self.emit = emit or (lambda t, d: None)
        self.escalate_hook = escalate_hook
        self.review_grace_s = review_grace_s
        self.repo_lookup = repo_lookup
        self.repo_roots = repo_roots
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(path, migrations=MIGRATIONS)
        self.db.migrate()
        self._changed = threading.Condition()
        self._version = 0

    def close(self) -> None:
        self.db.close()

    # -- plumbing ------------------------------------------------------------------------------------------------

    def _bump(self, event: str, data: dict[str, Any]) -> None:
        with self._changed:
            self._version += 1
            self._changed.notify_all()
        try:
            self.emit(event, data)
        except Exception:  # noqa: BLE001 — the bus must never break the Ágora
            pass

    def _agent(self, value: Any, *, allow_person: bool = False) -> str:
        agent = str(value or "").strip().lower()
        if not _AGENT_RE.match(agent):
            raise AgoraError("agent is required: a short id such as builder, reviewer or agent-2")
        if agent == PERSON and not allow_person:
            raise AgoraError(f"only the hub's page may write as {PERSON!r}; agents use their own id", 403)
        if agent in ("hub", "ui", "system"):
            raise AgoraError(f"{agent!r} is reserved")
        return agent

    def _touch(self, agent: str) -> None:
        now = self.clock()
        if agent == PERSON:
            kind = "human"
        else:
            kind = "agent"
        self.db.execute(
            "INSERT INTO agents(id, name, kind, state, last_seen, created) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (agent, agent, kind, "working", now, now))

    def _task_row(self, task_id: Any) -> dict[str, Any]:
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            raise AgoraError("task_id must be a number") from None
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if row is None:
            raise AgoraError(f"no task {tid}", 404)
        return self._task_dict(row, full_checkpoint=True)

    def _thread_row(self, thread_id: Any) -> dict[str, Any]:
        try:
            tid = int(thread_id)
        except (TypeError, ValueError):
            raise AgoraError("thread_id must be a number") from None
        row = self.db.one("SELECT * FROM threads WHERE id=?", (tid,))
        if row is None:
            raise AgoraError(f"no thread {tid}", 404)
        return dict(row)

    def _task_dict(self, row: Any, *, full_checkpoint: bool = False) -> dict[str, Any]:
        d = dict(row)
        d["paths"] = _l(d.get("paths"))
        d["commits"] = _l(d.get("commits"))
        d["reviewed_commits"] = _l(d.get("reviewed_commits"))
        d["reviewed"] = bool(d.get("reviewed"))
        d.setdefault("review_state", None)
        try:
            blocks = json.loads(d["submission_blocks"]) if d.get("submission_blocks") else None
        except (TypeError, ValueError):
            blocks = None
        d["submission_blocks"] = blocks if isinstance(blocks, list) else None
        latest = self.db.one("SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY revision DESC LIMIT 1", (d["id"],))
        d["checkpoint_summary"] = None
        if latest:
            summary = json.loads(latest["payload"])["payload"]["summary"]
            d["checkpoint_summary"] = {"revision": latest["revision"], "author": latest["author"],
                                       "created": latest["created"], "summary": summary[:300], "truncated": len(summary) > 300}
        if full_checkpoint:
            d["latest_checkpoint"] = self._checkpoint_dict(latest, d.get("owner")) if latest else None
        deps = self.db.query("SELECT t.id, t.title, t.status, t.owner FROM agora_task_deps d JOIN tasks t ON t.id=d.blocker_id "
                             "WHERE d.task_id=? ORDER BY t.id", (d["id"],))
        d["blocked_by"] = [{"id": r["id"], "title": r["title"], "status": r["status"], "owner": r["owner"]} for r in deps]
        d["waiting_on"] = [r["id"] for r in deps if r["status"] not in DEP_CLEAR]
        d["dependents"] = [r["task_id"] for r in self.db.query(
            "SELECT task_id FROM agora_task_deps WHERE blocker_id=? ORDER BY task_id", (d["id"],))]
        d["activity"] = self._task_activity(d, volatile=False)
        return d

    def _task_activity(self, task: dict[str, Any], *, idle_s: Optional[int] = None,
                       observed: Optional[dict[str, Any]] = None, volatile: bool = True) -> dict[str, Any]:
        """A read-only inference from task reports; an agent heartbeat is not task progress."""
        now = self.clock()
        threshold = self.task_idle_s if idle_s is None else idle_s
        owner = task.get("owner")
        agent = self.db.one("SELECT state,last_seen FROM agents WHERE id=?", (owner,)) if owner else None
        since = float(task.get("claimed_at") or task.get("created") or 0)
        cp = self.db.one("SELECT revision,created FROM task_checkpoints WHERE task_id=? AND author=? "
                         "AND created>=? ORDER BY revision DESC LIMIT 1", (task["id"], owner, since))
        note = self.db.one("SELECT m.id,m.created FROM messages m JOIN threads t ON t.id=m.thread_id "
                           "WHERE t.task_id=? AND m.author=? AND m.created>=? "
                           "ORDER BY m.id DESC LIMIT 1", (task["id"], owner, since))
        last_progress = max(since, float(cp["created"]) if cp else since, float(note["created"]) if note else since)
        heartbeat = float(agent["last_seen"]) if agent else None
        fresh = heartbeat is not None and now - heartbeat <= DEFAULT_LOCK_TTL_S
        elapsed = max(0.0, now - last_progress)
        state, reason = "recent", "recent_task_report"
        status = task["status"]
        if status in FINAL:
            state, reason = "final", "task_closed"
        elif not owner:
            state, reason = "unclaimed", "no_owner"
        elif status in ("review", "approved", "blocked"):
            state, reason = "waiting", "task_" + status
        elif agent and agent["state"] in ("waiting", "idle", "away"):
            state, reason = "waiting", "owner_" + agent["state"]
        elif elapsed >= threshold:
            state, reason = ("stalled", "heartbeat_without_task_report") if fresh else ("quiet", "owner_unreachable")
        observation = None
        if observed:
            observation = {k: observed.get(k) for k in ("state", "last_activity", "session_key")}
            observation["scope"] = "agent"
            at = observed.get("last_activity")
            recent = isinstance(at, (float, int)) and not isinstance(at, bool) and now - at <= threshold
            if state == "stalled" and recent and observed.get("state") in ("working", "tool"):
                state, reason = "unconfirmed", "owner_observed_running"
            elif state == "stalled" and observed.get("state") == "waiting":
                state, reason = "waiting", "owner_observed_waiting"
        result = {"state": state, "reason": reason, "inferred": True,
                "idle_after_s": threshold, "last_progress_at": last_progress,
                "heartbeat_at": heartbeat, "heartbeat_recent": fresh,
                "checkpoint_revision": cp["revision"] if cp else None,
                "message_id": note["id"] if note else None, "owner_observed": observation}
        if volatile:
            result.update(checked_at=now, idle_s=elapsed)
        return result

    def activity(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        if set(a) - {"task_id", "idle_after_s"}:
            raise AgoraError("unknown activity argument")
        threshold = _checkpoint_int(a.get("idle_after_s", self.task_idle_s), "idle_after_s", 60, 7 * 86400)
        task = self._task_row(_checkpoint_int(a.get("task_id"), "task_id", 1))
        return {"ok": True, "task_id": task["id"], "activity": self._task_activity(task, idle_s=threshold)}

    def timeline(self, a: dict[str, Any]) -> dict[str, Any]:
        """Chronology is stable across equal timestamps and process restarts: cursor = message ID."""
        if set(a) - {"task_id", "after_id", "limit"}:
            raise AgoraError("unknown timeline argument")
        tid = _checkpoint_int(a.get("task_id"), "task_id", 1)
        after = _checkpoint_int(a.get("after_id", 0), "after_id")
        limit = _checkpoint_int(a.get("limit", 100), "limit", 1, 500)
        task = self._task_row(tid)
        rows = self.db.query("SELECT m.*,t.title AS thread_title FROM messages m JOIN threads t ON t.id=m.thread_id "
                             "WHERE t.task_id=? AND m.id>? ORDER BY m.id LIMIT ?", (tid, after, limit + 1))
        events = []
        for row in rows[:limit]:
            event = dict(row)
            event["mentions"] = _l(event.get("mentions"))
            event["event_id"] = event["id"]
            event["event_kind"] = "message"
            match = re.match(r"^Checkpoint r([0-9]+):", event["body"]) if event["kind"] == "system" else None
            if match and f"[hub_agora_checkpoints task_id={tid}]" in event["body"] and event["thread_id"] == task["thread_id"]:
                cp = self.db.one("SELECT * FROM task_checkpoints WHERE task_id=? AND revision=? AND author=? "
                                 "AND (message_id=? OR (message_id IS NULL AND created<=? AND created>=?))",
                                 (tid, int(match[1]), event["author"], event["id"], event["created"], event["created"] - 5))
                if cp:
                    event["event_kind"] = "checkpoint"
                    event["checkpoint"] = self._checkpoint_dict(cp, task.get("owner"))
                    event["checkpoint_link"] = "recorded" if cp["message_id"] else "legacy_inferred"
            events.append(event)
        return {"ok": True, "task_id": tid, "events": events, "has_more": len(rows) > limit,
                "next_after_id": events[-1]["event_id"] if events else after,
                "last_event_id": self.db.scalar("SELECT COALESCE(MAX(m.id),0) FROM messages m "
                                               "JOIN threads t ON t.id=m.thread_id WHERE t.task_id=?", (tid,))}

    def _checkpoint_dict(self, row: Any, current_owner: Optional[str]) -> dict[str, Any]:
        d = dict(row)
        stored = json.loads(d.pop("payload"))
        d.update(stored)
        live = [dict(r) for r in self.db.query("SELECT * FROM locks WHERE expires>?", (self.clock(),))]
        held, missing, clashes = [], [], []
        for snapshot in d["lock_snapshot"]:
            resource = snapshot["resource"]
            exact = next((lk for lk in live if lk["resource"] == resource and lk["owner"] == current_owner
                          and lk["task_id"] == d["task_id"] and lk["acquired"] == snapshot["acquired"]
                          and lk["owner"] == snapshot["owner"]), None)
            if exact:
                held.append(exact)
            else:
                missing.append(resource)
            for lk in live:
                if conflicts(resource, lk["resource"]) and (lk["owner"] != current_owner or lk["task_id"] != d["task_id"]):
                    clashes.append({"resource": resource, "held": lk["resource"], "owner": lk["owner"],
                                    "task_id": lk["task_id"], "expires": lk["expires"]})
        d["current_lock_status"] = {"owner": current_owner, "held": held, "missing": missing, "conflicts": clashes,
                                    "owner_changed": current_owner != d["author"], "checked_at": self.clock()}
        return d

    def checkpoint(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"))
        if set(a) - {"agent", "task_id", "expected_revision", "payload"}:
            raise AgoraError("unknown checkpoint argument")
        task_id = _checkpoint_int(a.get("task_id"), "task_id", 1)
        expected = _checkpoint_int(a.get("expected_revision"), "expected_revision", maximum=9_223_372_036_854_775_806)
        payload = _checkpoint_payload(a.get("payload"))
        duplicate = False
        with self.db.tx():
            task = self._task_row(task_id)
            if task["owner"] != agent:
                raise AgoraError("only the current task owner may checkpoint", 403)
            if task["status"] in FINAL:
                raise AgoraError("a final task cannot receive checkpoints", 409)
            latest = task["latest_checkpoint"]
            revision = latest["revision"] if latest else 0
            if expected != revision:
                if latest and expected == revision - 1 and latest["author"] == agent and latest["payload"] == payload:
                    duplicate = True
                else:
                    raise AgoraError("checkpoint revision conflict", 409, current_revision=revision)
            if not duplicate:
                snapshot = [dict(r) for r in self.db.query(
                    "SELECT * FROM locks WHERE owner=? AND task_id=? AND expires>? ORDER BY resource",
                    (agent, task_id, self.clock()))]
                self.db.execute("INSERT INTO task_checkpoints(task_id,revision,author,created,payload) VALUES(?,?,?,?,?)",
                                (task_id, revision + 1, agent, self.clock(),
                                 json.dumps({"payload": payload, "lock_snapshot": snapshot}, ensure_ascii=False, allow_nan=False)))
                preview = payload['summary'][:300]
                if len(payload['summary']) > 300:
                    preview += "…"
                message_id = self._system(task, f"Checkpoint r{revision + 1}: {preview} "
                                          f"[hub_agora_checkpoints task_id={task_id}]", agent)
                self.db.execute("UPDATE task_checkpoints SET message_id=? WHERE task_id=? AND revision=?",
                                (message_id, task_id, revision + 1))
            row = self.db.one("SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,))
            result = self._checkpoint_dict(row, task["owner"])
        if not duplicate:
            self._bump("agora.checkpoint", {"task_id": task_id, "revision": result["revision"], "author": agent})
        return {"ok": True, "checkpoint": result, "duplicate": duplicate}

    def checkpoints(self, a: dict[str, Any]) -> dict[str, Any]:
        if set(a) - {"task_id", "after_revision", "limit"}:
            raise AgoraError("unknown checkpoints argument")
        task_id = _checkpoint_int(a.get("task_id"), "task_id", 1)
        after = _checkpoint_int(a.get("after_revision", 0), "after_revision")
        limit = _checkpoint_int(a.get("limit", 100), "limit", 1, 500)
        task = self._task_row(task_id)
        rows = self.db.query("SELECT * FROM task_checkpoints WHERE task_id=? AND revision>? ORDER BY revision LIMIT ?",
                             (task_id, after, limit + 1))
        items = [self._checkpoint_dict(row, task["owner"]) for row in rows[:limit]]
        return {"ok": True, "task_id": task_id, "checkpoints": items, "has_more": len(rows) > limit,
                "next_after_revision": items[-1]["revision"] if items else after}

    def _msg(self, thread_id: int, author: str, kind: str, body: str, mentions: Optional[list[str]] = None) -> int:
        now = self.clock()
        mid = self.db.insert("messages", {"thread_id": thread_id, "author": author, "kind": kind, "body": body,
                                          "mentions": _j(mentions), "created": now})
        self.db.execute("UPDATE threads SET updated=? WHERE id=?", (now, thread_id))
        return mid

    def _system(self, task: dict[str, Any], text: str, actor: str = "system") -> Optional[int]:
        """A bookkeeping line in the task's thread, authored by whoever caused it (so it never lands in their own inbox)."""
        if task.get("thread_id"):
            return self._msg(int(task["thread_id"]), actor, "system", text)
        return None

    def _ack(self, agent: str, thread_id: int, upto: Optional[int] = None) -> None:
        """The agent has seen this thread up to ``upto`` (default: its last message): its mentions there stop being pending."""
        if upto is None:
            upto = int(self.db.scalar("SELECT MAX(id) FROM messages WHERE thread_id=?", (thread_id,), default=0) or 0)
        self.db.execute("INSERT INTO acks(agent, thread_id, upto, updated) VALUES(?,?,?,?) ON CONFLICT(agent, thread_id) "
                        "DO UPDATE SET upto=MAX(acks.upto, excluded.upto), updated=excluded.updated",
                        (agent, thread_id, upto, self.clock()))

    # -- freshness (A1): act on a task only after reading what others wrote there --------------------------------

    def _unseen(self, agent: str, task_id: int) -> list[dict[str, Any]]:
        """Messages from others in this task's threads that the agent has not seen yet: newer than its ack of that
        thread (thread/task reads with the agent, its own posts) and than its inbox read mark (a non-peek inbox
        delivers the bodies). Bookkeeping lines (kind ``system``) and the task's own description do not count."""
        row = self.db.one("SELECT last_msg_id FROM reads WHERE agent=?", (agent,))
        read_upto = int(row["last_msg_id"]) if row else 0
        return [dict(r) for r in self.db.query(
            "SELECT m.id, m.thread_id, m.author, m.kind, m.created FROM messages m JOIN threads t ON t.id=m.thread_id "
            "LEFT JOIN acks k ON k.agent=? AND k.thread_id=m.thread_id "
            "WHERE t.task_id=? AND m.author<>? AND m.kind<>'system' AND m.id>COALESCE(k.upto, 0) AND m.id>? "
            # the task's own description (first message of its thread) is what the board and task listings show
            "AND NOT (t.kind='task' AND m.id=(SELECT MIN(id) FROM messages WHERE thread_id=t.id)) "
            "ORDER BY m.id", (agent, task_id, agent, read_upto))]

    def _require_fresh(self, agent: str, task: dict[str, Any], a: dict[str, Any], action: str) -> Optional[str]:
        """A1, before claim/submit/review/done/post on a task: did others write there something the agent has not
        read? It never refuses: the unseen list goes in the answer (``unseen``) and in an ``agora.task.unseen``
        event, and nothing is written in the thread. Mode ``off`` and the person acting from the hub page skip it.
        Returns None (kept as a hook for the callers)."""
        if self.freshness == "off" or agent == PERSON or a.get("_person"):
            return None
        unseen = self._unseen(agent, int(task["id"]))
        if not unseen:
            return None
        a["_unseen_warning"] = {"count": len(unseen), "messages": unseen[:20], "action": action,
                                "hint": "others wrote in this task's thread since you last read it: task/thread <id>"}
        self._bump("agora.task.unseen", {"task_id": task["id"], "agent": agent, "action": action, "count": len(unseen),
                                         "message_ids": [m["id"] for m in unseen[:20]]})
        return None

    @staticmethod
    def _with_unseen(a: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
        if a.get("_unseen_warning"):
            out["unseen"] = a["_unseen_warning"]
        return out

    def _live_locks(self) -> list[dict[str, Any]]:
        now = self.clock()
        self.db.execute("DELETE FROM locks WHERE expires <= ?", (now,))
        return [dict(r) for r in self.db.query("SELECT * FROM locks ORDER BY acquired")]

    def _agent_stale(self, agent: Optional[str]) -> bool:
        if not agent:
            return True
        row = self.db.one("SELECT last_seen FROM agents WHERE id=?", (agent,))
        return row is None or (self.clock() - float(row["last_seen"] or 0)) > STALE_AGENT_S

    # -- locks ---------------------------------------------------------------------------------------------------

    def _acquire(self, agent: str, resources: list[str], task_id: Optional[int], ttl_s: int, note: str) -> dict[str, Any]:
        wanted = [normalize_resource(r) for r in resources]
        live = self._live_locks()
        clashes = []
        for res in wanted:
            for lock in live:
                if lock["owner"] != agent and conflicts(res, lock["resource"]):
                    clashes.append({"resource": res, "held": lock["resource"], "owner": lock["owner"],
                                    "task_id": lock["task_id"], "note": lock["note"], "expires": lock["expires"]})
        if clashes:
            return {"ok": False, "conflicts": clashes}
        now = self.clock()
        for res in wanted:
            self.db.execute(
                "INSERT INTO locks(resource, owner, task_id, note, ttl_s, acquired, expires) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner, task_id=COALESCE(excluded.task_id, locks.task_id), "
                "note=excluded.note, ttl_s=excluded.ttl_s, expires=excluded.expires",
                (res, agent, task_id, note, ttl_s, now, now + ttl_s))
        return {"ok": True, "locks": wanted}

    def _ttl(self, value: Any) -> int:
        if value in (None, ""):
            return DEFAULT_LOCK_TTL_S
        try:
            ttl = int(value)
        except (TypeError, ValueError):
            raise AgoraError("ttl_s must be a number of seconds") from None
        if not 60 <= ttl <= MAX_LOCK_TTL_S:
            raise AgoraError(f"ttl_s must be between 60 and {MAX_LOCK_TTL_S}")
        return ttl

    def lock(self, a: dict[str, Any]) -> dict[str, Any]:
        """Take (or renew) locks outside a task: ``model:principal`` for a live evaluation, ``merge:Repo`` to integrate."""
        agent = self._agent(a.get("agent"))
        resources = _list(a.get("resources") or a.get("resource"), "resources")
        if not resources:
            raise AgoraError("resources is required")
        task_id = int(a["task_id"]) if a.get("task_id") not in (None, "") else None
        with self.db.tx():
            self._touch(agent)
            res = self._acquire(agent, resources, task_id, self._ttl(a.get("ttl_s")), _txt(a.get("note"), 300, "note"))
        if res["ok"]:
            self._bump("agora.lock.acquired", {"agent": agent, "locks": res["locks"], "task_id": task_id})
        else:
            self._bump("agora.lock.conflict", {"agent": agent, "conflicts": res["conflicts"]})
        return res

    def unlock(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        resources = [normalize_resource(r) for r in _list(a.get("resources") or a.get("resource"), "resources")]
        released = []
        with self.db.tx():
            for res in resources:
                row = self.db.one("SELECT owner FROM locks WHERE resource=?", (res,))
                if row is None:
                    continue
                if row["owner"] != agent and agent != PERSON:
                    raise AgoraError(f"{res} belongs to {row['owner']}", 409)
                self.db.execute("DELETE FROM locks WHERE resource=?", (res,))
                released.append(res)
        if released:
            self._bump("agora.lock.released", {"agent": agent, "locks": released})
        return {"ok": True, "released": released}

    def locks(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self.db.tx():
            live = self._live_locks()
        probe = (a or {}).get("check")
        if probe:
            res = normalize_resource(probe)
            clash = [lk for lk in live if conflicts(res, lk["resource"])]
            return {"ok": True, "resource": res, "free": not clash, "held_by": clash}
        return {"ok": True, "locks": live, "capabilities": {"heartbeat_selective_renewal": True}}

    # -- provider quota observations ---------------------------------------------------------------------------

    _USAGE_PROVIDERS = frozenset({"claude", "codex", "cursor", "faustus"})
    _USAGE_STALE_S = 24 * 3600

    def usage_report(self, a: dict[str, Any]) -> dict[str, Any]:
        """Save a sanitized account-pool snapshot. The pool key is an opaque label, never an account identifier."""
        agent = self._agent(a.get("agent"))
        provider = str(a.get("provider_id") or "").strip().lower()
        if provider not in self._USAGE_PROVIDERS:
            raise AgoraError("provider_id must be one of claude, codex, cursor or faustus")
        pool_id = str(a.get("pool_id") or "").strip()
        pool_label = _txt(a.get("pool_label"), 80, "pool_label", required=True)
        if not pool_id or len(pool_id) > 80 or "@" in pool_id or re.search(r"[\\/\s]", pool_id):
            raise AgoraError("pool_id must be a short opaque label without email, path or whitespace")
        source = _txt(a.get("source"), 120, "source", required=True)
        raw_windows = a.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows or len(raw_windows) > 20:
            raise AgoraError("windows must be a non-empty array of at most 20 quota windows")
        windows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_windows:
            if not isinstance(raw, dict):
                raise AgoraError("each quota window must be an object")
            window_id = str(raw.get("window_id") or "").strip()
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", window_id) or window_id in seen:
                raise AgoraError("window_id must be unique and contain only letters, digits, dot, underscore or hyphen")
            seen.add(window_id)
            label = _txt(raw.get("label") or window_id, 100, "window label", required=True)
            measurement = str(raw.get("measurement") or "unknown").strip().lower()
            if measurement not in {"measured", "declared", "unknown"}:
                raise AgoraError("measurement must be measured, declared or unknown")
            used = raw.get("used_percent")
            remaining = raw.get("remaining_percent")
            if measurement == "unknown":
                if used is not None or remaining is not None:
                    raise AgoraError("unknown quota windows must not include percentages")
                used = remaining = None
            else:
                if isinstance(used, bool) or isinstance(remaining, bool):
                    raise AgoraError("quota percentages must be numbers from 0 to 100")
                try:
                    used, remaining = float(used), float(remaining)
                except (TypeError, ValueError):
                    raise AgoraError("measured and declared windows require used_percent and remaining_percent") from None
                if not (math.isfinite(used) and math.isfinite(remaining) and 0 <= used <= 100 and 0 <= remaining <= 100):
                    raise AgoraError("quota percentages must be finite numbers from 0 to 100")
                if abs(used + remaining - 100) > 0.5:
                    raise AgoraError("used_percent and remaining_percent must sum to 100 (within 0.5)")
            reset_at = raw.get("reset_at")
            if reset_at is not None:
                reset_at = str(reset_at).strip()
                try:
                    datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                except ValueError:
                    raise AgoraError("reset_at must be an ISO-8601 timestamp or null") from None
            windows.append({"window_id": window_id, "label": label, "measurement": measurement,
                            "used_percent": used, "remaining_percent": remaining, "reset_at": reset_at,
                            "source": _txt(raw.get("source") or source, 120, "window source", required=True)})

        before_pools = self._quota_pools()
        before = {r["id"]: self._quota_state_from(r["id"], before_pools)["state"]
                  for r in self.db.query("SELECT id FROM agents") if self._quota_provider(r["id"]) == provider}
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            self.db.execute(
                "INSERT INTO agora_usage_pools(provider_id,pool_id,pool_label,source,reported_at,windows_json) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(provider_id,pool_id) DO UPDATE SET "
                "pool_label=excluded.pool_label,source=excluded.source,reported_at=excluded.reported_at,"
                "windows_json=excluded.windows_json",
                (provider, pool_id, pool_label, source, now, json.dumps(windows, separators=(",", ":"))))
            self.db.execute(
                "INSERT INTO agora_usage_reporters(provider_id,pool_id,agent_id,last_reported_at) VALUES(?,?,?,?) "
                "ON CONFLICT(provider_id,pool_id,agent_id) DO UPDATE SET last_reported_at=excluded.last_reported_at",
                (provider, pool_id, agent, now))
            self._quota_exhaustion_notice(provider, before)
        self._bump("agora.usage.reported", {"provider_id": provider, "pool_id": pool_id, "agent_id": agent})
        return {"ok": True, "provider_id": provider, "pool_id": pool_id, "agent_id": agent,
                "reported_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}

    def usage(self, _a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Read shared quota pools; old records remain visible but explicitly stale/unknown, never zero."""
        now = self.clock()
        rows = self.db.query(
            "SELECT p.*, GROUP_CONCAT(r.agent_id, ',') AS reporter_ids FROM agora_usage_pools p "
            "LEFT JOIN agora_usage_reporters r ON r.provider_id=p.provider_id AND r.pool_id=p.pool_id "
            "GROUP BY p.provider_id,p.pool_id ORDER BY p.provider_id,p.pool_label")
        providers: dict[str, list[dict[str, Any]]] = {key: [] for key in sorted(self._USAGE_PROVIDERS)}
        provider_agents: dict[str, list[dict[str, Any]]] = {key: [] for key in sorted(self._USAGE_PROVIDERS)}
        for row in rows:
            stale = now - float(row["reported_at"]) > self._USAGE_STALE_S
            try:
                windows = json.loads(row["windows_json"])
            except (TypeError, ValueError):
                windows = []
            reported_at = datetime.fromtimestamp(float(row["reported_at"]), timezone.utc).isoformat()
            for window in windows:
                window["reported_at"] = reported_at
                if stale:
                    window["stale"] = True
                    window["used_percent"] = None
                    window["remaining_percent"] = None
                    window["reset_at"] = None
                    window["measurement"] = "unknown"
                else:
                    window["stale"] = False
                    # A fresh report can still contain a window whose reset has
                    # already passed. Expire that window alone; other windows
                    # in the same pool remain usable until their own reset.
                    reset_at = window.get("reset_at")
                    if reset_at:
                        try:
                            reset = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
                            if reset.tzinfo is None:
                                reset = reset.replace(tzinfo=timezone.utc)
                            expired = reset.timestamp() <= now
                        except (OverflowError, OSError, TypeError, ValueError):
                            expired = False
                        if expired:
                            window["stale"] = True
                            window["used_percent"] = None
                            window["remaining_percent"] = None
                            window["reset_at"] = None
                            window["measurement"] = "unknown"
            providers[row["provider_id"]].append({
                "pool_id": row["pool_id"], "pool_label": row["pool_label"], "source": row["source"],
                "reported_at": reported_at, "stale": stale,
                "agent_ids": sorted(filter(None, str(row["reporter_ids"] or "").split(","))), "windows": windows})
            agents = sorted(filter(None, str(row["reporter_ids"] or "").split(",")))
            for agent_id in agents:
                provider_agents[row["provider_id"]].append({
                    "agent_id": agent_id, "pool_id": row["pool_id"], "pool_label": row["pool_label"],
                    "shared": len(agents) > 1, "shared_agent_count": len(agents), "source": row["source"],
                    "reported_at": reported_at, "stale": stale, "windows": json.loads(json.dumps(windows))})
        return {"ok": True, "providers": [{"provider_id": key, "pools": providers[key],
                                             "agents": provider_agents[key]} for key in sorted(providers)]}

    # -- quota state of each agent (warn, never block) -------------------------------------------------------------

    _QUOTA_LOW_PERCENT = 15.0

    @classmethod
    def _quota_provider(cls, agent_id: Optional[str]) -> Optional[str]:
        """The provider whose quota an agent spends: its id is the provider or starts with ``<provider>-``
        (``cursor-agora`` -> cursor, ``codex-ayuda`` -> codex, ``claude-capacity`` -> claude)."""
        agent = str(agent_id or "").strip().lower()
        for provider in sorted(cls._USAGE_PROVIDERS):
            if agent == provider or agent.startswith(provider + "-"):
                return provider
        return None

    @staticmethod
    def _quota_when(reset_at: Optional[str]) -> str:
        try:
            when = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone().strftime("%d-%m %H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            return str(reset_at)

    def _quota_pools(self) -> dict[str, list[dict[str, Any]]]:
        return {p["provider_id"]: p["pools"] for p in self.usage()["providers"]}

    def _quota_state_from(self, agent_id: str, pools_by_provider: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        provider = self._quota_provider(agent_id)
        out: dict[str, Any] = {"agent": agent_id, "provider": provider, "state": "none", "remaining_percent": None,
                               "reset_at": None, "pool_label": None, "note": None}
        pools = pools_by_provider.get(provider or "", [])
        if not pools:
            return out
        mine = [p for p in pools if agent_id in (p.get("agent_ids") or [])]
        pool = max(mine or pools, key=lambda p: str(p.get("reported_at") or ""))
        out["pool_label"] = pool.get("pool_label")
        if pool.get("stale"):
            out.update(state="stale", note=f"{agent_id}: cuota sin dato reciente")
            return out
        measured = [w for w in pool.get("windows") or [] if not w.get("stale") and w.get("remaining_percent") is not None]
        if not measured:
            out.update(state="unknown")
            return out
        remaining = min(float(w["remaining_percent"]) for w in measured)
        out["remaining_percent"] = round(remaining, 1)
        exhausted = [w for w in measured if float(w["remaining_percent"]) <= 0.5]
        if exhausted:
            resets = [w["reset_at"] for w in exhausted if w.get("reset_at")]
            reset_at = max(resets) if resets else None
            out.update(state="exhausted", reset_at=reset_at,
                       note=f"{agent_id} sin cuota" + (f" hasta {self._quota_when(reset_at)}" if reset_at else ""))
        elif remaining < self._QUOTA_LOW_PERCENT:
            low = min(measured, key=lambda w: float(w["remaining_percent"]))
            out.update(state="low", reset_at=low.get("reset_at"), note=f"{agent_id} con poca cuota ({remaining:.0f} % libre)")
        else:
            out["state"] = "ok"
        return out

    def agent_quota_state(self, a: Any) -> dict[str, Any]:
        """ok / low (<15 % left) / exhausted (0 %, with reset_at) / unknown / stale (no fresh report) / none (no
        provider or no report) for one agent, from the latest report of its provider's pool."""
        agent = str((a or {}).get("agent") if isinstance(a, dict) else a or "").strip().lower()
        if not _AGENT_RE.match(agent):
            raise AgoraError("agent is required")
        return {"ok": True, **self._quota_state_from(agent, self._quota_pools())}

    def _quota_warnings(self, agent_ids: Any) -> list[str]:
        """Notes for the agents among ``agent_ids`` that are out of quota or nearly so (a warning, nothing stops)."""
        ids = []
        for value in agent_ids or []:
            agent = str(value or "").strip().lower()
            if agent and agent not in ids and agent != PERSON:
                ids.append(agent)
        if not ids:
            return []
        pools = self._quota_pools()
        notes = []
        for agent in ids:
            state = self._quota_state_from(agent, pools)
            if state["state"] in ("exhausted", "low") and state["note"]:
                notes.append(state["note"])
        return notes

    def _quota_exhaustion_notice(self, provider: str, before: dict[str, str]) -> None:
        """When a report leaves agents of ``provider`` out of quota (and they were not before), say in the coordination
        thread which of their tasks and reviews should be handed over. Nothing moves by itself."""
        pools = self._quota_pools()
        agents = sorted({r["id"] for r in self.db.query("SELECT id FROM agents")} | set(before))
        lines = []
        for agent in agents:
            if self._quota_provider(agent) != provider:
                continue
            state = self._quota_state_from(agent, pools)
            if state["state"] != "exhausted" or before.get(agent) == "exhausted":
                continue
            final_sql = ",".join("?" * len(FINAL))
            owned = self.db.query(f"SELECT id, title, status FROM tasks WHERE owner=? AND status NOT IN ({final_sql}) "
                                  "ORDER BY id", (agent, *FINAL))
            reviews = self.db.query("SELECT id, title FROM tasks WHERE reviewer=? AND status IN ('review','changes') "
                                    "ORDER BY id", (agent,))
            if not owned and not reviews:
                continue
            lines.append(f"{state['note']}.")
            lines += [f"- #{r['id']} [{r['status']}] {r['title']}" for r in owned]
            lines += [f"- revisión de #{r['id']} {r['title']}" for r in reviews]
        thread_id = self.handover_thread_id
        if not lines or thread_id is None or self.db.one("SELECT id FROM threads WHERE id=?", (thread_id,)) is None:
            return
        lines.append("Repartidlas con `agora handover --from <agente> --to <relevo> --force` o soltadlas; no se mueven solas.")
        self._msg(thread_id, "system", "system", "\n".join(lines))

    # -- orphans: work nobody is moving (warn once, suggest who takes it, never move it) -----------------------------

    ORPHAN_SWEEP_EVERY_S = 300.0

    def _worker_ids(self) -> list[str]:
        """Agents that can take work: not the person, not the hub, not a quota reporter (``*-capacity``)."""
        return [r["id"] for r in self.db.query("SELECT id FROM agents ORDER BY id")
                if r["id"] not in (PERSON, "system", "hub", "ui") and not r["id"].endswith("-capacity")]

    def orphans(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Work nobody is moving, computed now: a task in review with no reviewer for ``handover_idle_s`` or whose
        reviewer is silent (no activity for that long) or out of quota, and a claimed task whose owner is. Each
        finding names a free agent (recent activity, quota left, fewest open tasks) and the command that moves it."""
        now = self.clock()
        idle_limit = self.handover_idle_s
        pools = self._quota_pools()
        seen = {r["id"]: float(r["last_seen"] or 0) for r in self.db.query("SELECT id, last_seen FROM agents")}
        quota = {aid: self._quota_state_from(aid, pools)["state"] for aid in seen}
        load: dict[str, int] = {}
        for r in self.db.query("SELECT owner, reviewer, status FROM tasks WHERE status IN ('claimed','in_progress','changes','review')"):
            if r["owner"]:
                load[r["owner"]] = load.get(r["owner"], 0) + 1
            if r["reviewer"] and r["status"] == "review":
                load[r["reviewer"]] = load.get(r["reviewer"], 0) + 1

        def silent(agent: Optional[str]) -> Optional[str]:
            if not agent:
                return None
            if quota.get(agent) == "exhausted":
                return "sin cuota"
            last = seen.get(agent)
            if last is None or now - last >= idle_limit:
                return "sin actividad" if last is None else f"sin actividad desde hace {int((now - last) // 60)} min"
            return None

        free = sorted((aid for aid in self._worker_ids()
                       if now - seen.get(aid, 0) < idle_limit and quota.get(aid) != "exhausted"),
                      key=lambda aid: (load.get(aid, 0), -seen.get(aid, 0), aid))

        def pick(*avoid: Optional[str]) -> Optional[str]:
            return next((aid for aid in free if aid not in avoid), None)

        findings: list[dict[str, Any]] = []
        for t in self.db.query("SELECT * FROM tasks WHERE status IN ('review','claimed','in_progress','changes') ORDER BY id"):
            t = dict(t)
            if t["status"] == "review":
                reviewer = t.get("reviewer")
                rev = int(t.get("submission_revision") or 0)
                if not reviewer:
                    if now - float(t.get("submitted_at") or now) < idle_limit:
                        continue
                    who, why, kind = None, "nadie la ha revisado", "review_unassigned"
                else:
                    why = silent(reviewer)
                    if not why:
                        continue
                    who, kind = reviewer, "review_silent"
                suggest = pick(t.get("owner"), reviewer)
                command = (f"agora handover --from {reviewer} --to {suggest} --tasks {t['id']} --force"
                           if reviewer and suggest else (f"{suggest} revisa con: agora --as {suggest} task {t['id']}" if suggest else None))
                key = f"{kind}:{t['id']}:{who or '-'}:{rev}"
            else:
                owner = t.get("owner")
                why = silent(owner)
                if not why:
                    continue
                who, kind = owner, "owner_silent"
                suggest = pick(owner, t.get("reviewer"))
                command = f"agora handover --from {owner} --to {suggest} --tasks {t['id']} --force" if suggest else None
                key = f"{kind}:{t['id']}:{owner}:{int(float(t.get('claimed_at') or 0))}"
            findings.append({"key": key, "kind": kind, "task_id": t["id"], "title": t["title"], "status": t["status"],
                             "who": who, "why": why, "suggest": suggest, "command": command})
        # A blocker nobody has claimed while someone is already waiting on it.
        waiting: dict[int, list[int]] = {}
        titles: dict[int, str] = {}
        for r in self.db.query("SELECT d.blocker_id, d.task_id, b.title FROM agora_task_deps d "
                               "JOIN tasks b ON b.id=d.blocker_id JOIN tasks t ON t.id=d.task_id "
                               "WHERE b.status='open' AND b.owner IS NULL AND t.status IN ('claimed','in_progress','blocked') "
                               "ORDER BY d.blocker_id, d.task_id"):
            waiting.setdefault(int(r["blocker_id"]), []).append(int(r["task_id"]))
            titles[int(r["blocker_id"])] = r["title"]
        for bid, deps in waiting.items():
            suggest = pick()
            findings.append({"key": f"blocker_unowned:{bid}:{','.join(map(str, deps))}", "kind": "blocker_unowned",
                             "task_id": bid, "title": titles[bid], "status": "open", "who": None,
                             "why": "bloquea a " + ", ".join(f"#{d}" for d in deps) + " y nadie la ha reclamado",
                             "suggest": suggest, "command": f"agora --as {suggest} claim {bid}" if suggest else None,
                             "dependents": deps})
        return {"ok": True, "findings": findings, "idle_limit_s": int(idle_limit)}

    def orphan_sweep(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Say each new orphan once: a system note in the task thread and one summary in the coordination thread
        mentioning the suggested agents. A finding comes back only when its situation changes (another reviewer,
        a new submission, a new claim). Runs by itself from heartbeats at most every five minutes."""
        a = a or {}
        now = self.clock()
        if not a.get("force") and now - getattr(self, "_last_orphan_sweep", 0.0) < self.ORPHAN_SWEEP_EVERY_S:
            return {"ok": True, "skipped": True, "new": []}
        self._last_orphan_sweep = now
        found = self.orphans()["findings"]
        new = []
        with self.db.tx():
            for f in found:
                if self.db.one("SELECT key FROM agora_orphan_findings WHERE key=?", (f["key"],)) is not None:
                    continue
                self.db.execute("INSERT INTO agora_orphan_findings(key, kind, task_id, created) VALUES(?,?,?,?)",
                                (f["key"], f["kind"], f["task_id"], now))
                new.append(f)
            for f in new:
                what = {"review_unassigned": "en revisión sin revisor", "review_silent": f"su revisor {f['who']} está {f['why']}",
                        "owner_silent": f"su dueño {f['who']} está {f['why']}", "blocker_unowned": f["why"]}[f["kind"]]
                note = f"Huérfana: {what}." + (f" Propuesta: {f['suggest']}. `{f['command']}`" if f["suggest"] else
                                                " No hay ningún agente libre ahora.")
                self._system(self._task_row(f["task_id"]), note, "system")
            thread_id = self.handover_thread_id
            if new and thread_id is not None and self.db.one("SELECT id FROM threads WHERE id=?", (thread_id,)) is not None:
                lines = ["Trabajo huérfano (nadie lo mueve):"]
                for f in new:
                    lines.append(f"- #{f['task_id']} [{f['status']}] {f['title']}: "
                                 + ("sin revisor" if f["kind"] == "review_unassigned" else
                                    f["why"] if not f["who"] else f"{f['who']} {f['why']}")
                                 + (f" → {f['suggest']}: `{f['command']}`" if f["suggest"] else " → nadie libre"))
                mentions = sorted({f["suggest"] for f in new if f["suggest"]})
                self._msg(thread_id, "system", "system", "\n".join(lines), mentions)
        if new:
            self._bump("agora.orphans", {"new": [f["key"] for f in new]})
        # Backup for dependency wake-ups a transition could have missed (each set of blockers wakes only once).
        return {"ok": True, "skipped": False, "new": new, "findings": found, "unblocked": self._wake_dependents()}

    # -- agents --------------------------------------------------------------------------------------------------

    def heartbeat(self, a: dict[str, Any]) -> dict[str, Any]:
        """Report state; renew all owned locks by default, or an exact selection."""
        agent = self._agent(a.get("agent"))
        state = str(a.get("state") or "working").lower()
        if state not in AGENT_STATES:
            raise AgoraError(f"state must be one of {', '.join(AGENT_STATES)}")
        selected = None
        if "renew_resources" in a:
            raw = a["renew_resources"]
            if not isinstance(raw, list) or not all(isinstance(r, str) and r.strip() for r in raw):
                raise AgoraError("renew_resources must be an array of resource names; [] renews no locks")
            selected = sorted({normalize_resource(r) for r in raw})
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            fields = {"state": state, "doing": _txt(a.get("doing"), 300, "doing")}
            if a.get("name"):
                fields["name"] = _txt(a.get("name"), 80, "name")
            if a.get("note") is not None:
                fields["note"] = _txt(a.get("note"), 500, "note")
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE agents SET {sets} WHERE id=?", (*fields.values(), agent))
            live = [dict(row) for row in self.db.query(
                "SELECT resource FROM locks WHERE owner=? AND expires>? ORDER BY resource", (agent, now))]
            renewed = [row["resource"] for row in live if selected is None or row["resource"] in selected]
            for res in renewed:
                self.db.execute("UPDATE locks SET expires = ? + ttl_s WHERE owner=? AND resource=? AND expires > ?",
                                (now, agent, res, now))
        self._bump("agora.agent.heartbeat", {"agent": agent, "state": state})
        try:
            self.orphan_sweep()          # at most every five minutes; a heartbeat never fails because of it
            if state != "working":
                self._wake_dependents()  # closing a turn: dependency wake-ups go out right away
        except Exception:  # noqa: BLE001
            pass
        inbox = self.inbox({"agent": agent, "peek": True})
        return {"ok": True, "agent": agent, "inbox": inbox["counts"],
                "lock_renewal": {"mode": "all" if selected is None else "selected",
                                 "renewed_resources": renewed}}

    def sync(self, a: dict[str, Any]) -> dict[str, Any]:
        """Resume an agent in one call without consuming inbox messages or mentions.

        ``since_id`` is a durable messages.id cursor, not the process-local long-poll version. Only the last post
        returned advances ``next_since_id``; when a thread filter is used, the cursor is scoped to those threads.
        The caller persists that value and sends it on the next call. A post arriving after the query therefore has
        an id above the returned cursor and is replayed on the next sync. Inbox peek and pending mentions retain
        their existing explicit read/ack behavior.
        """
        agent = self._agent(a.get("agent"))
        state = str(a.get("state") or "working").lower()
        if state not in AGENT_STATES:
            raise AgoraError(f"state must be one of {', '.join(AGENT_STATES)}")
        raw_since = a.get("since_id", 0)
        if isinstance(raw_since, bool):
            raise AgoraError("since_id must be a non-negative message id")
        if isinstance(raw_since, float) and not raw_since.is_integer():
            raise AgoraError("since_id must be a non-negative message id")
        try:
            since_id = int(raw_since)
        except (TypeError, ValueError):
            raise AgoraError("since_id must be a non-negative message id") from None
        if since_id < 0 or since_id > 9_223_372_036_854_775_807:
            raise AgoraError("since_id must be a non-negative message id")
        raw_limit = a.get("limit")
        if isinstance(raw_limit, bool) or (isinstance(raw_limit, float) and not raw_limit.is_integer()):
            raise AgoraError("limit must be an integer")
        try:
            limit = max(1, min(int(100 if raw_limit is None else raw_limit), 500))
        except (TypeError, ValueError):
            raise AgoraError("limit must be an integer") from None
        thread_ids = []
        for value in _list(a.get("thread_ids"), "thread_ids", 100):
            try:
                thread_id = int(value)
            except (TypeError, ValueError):
                raise AgoraError("thread_ids must contain positive thread ids") from None
            if thread_id < 1:
                raise AgoraError("thread_ids must contain positive thread ids")
            if thread_id not in thread_ids:
                if self.db.one("SELECT id FROM threads WHERE id=?", (thread_id,)) is None:
                    raise AgoraError(f"no thread {thread_id}", 404)
                thread_ids.append(thread_id)
        thread_ids.sort()

        doing = _txt(a.get("doing"), 300, "doing")
        heartbeat_args: dict[str, Any] = {"agent": agent, "doing": doing, "state": state}
        if a.get("name"):
            heartbeat_args["name"] = a.get("name")
        if a.get("note") is not None:
            heartbeat_args["note"] = a.get("note")
        if "renew_resources" in a:
            heartbeat_args["renew_resources"] = a["renew_resources"]
        heartbeat = self.heartbeat(heartbeat_args)

        # Peek by design. This preserves the inbox read mark and pending mention acknowledgements.
        inbox = self.inbox({"agent": agent, "peek": True})
        board = self.board()
        leased_tasks = self.tasks({"owner": agent, "status": "active", "limit": 500})["tasks"]
        now = self.clock()
        for task in leased_tasks:
            latest = self.db.one("SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task["id"],))
            task["latest_checkpoint"] = self._checkpoint_dict(latest, agent) if latest else None
            task["locks"] = [dict(r) for r in self.db.query(
                "SELECT resource, note, expires FROM locks WHERE task_id=? AND owner=? AND expires>? ORDER BY acquired",
                (task["id"], agent, now))]

        filters = ""
        params: list[Any] = [since_id]
        if thread_ids:
            filters = f" AND m.thread_id IN ({','.join('?' for _ in thread_ids)})"
            params.extend(thread_ids)
        params.append(limit + 1)
        rows = self.db.query(
            "SELECT m.*, t.title AS thread_title, t.kind AS thread_kind, t.status AS thread_status, "
            "t.task_id AS task_id FROM messages m JOIN threads t ON t.id=m.thread_id "
            "WHERE m.id>?" + filters + " ORDER BY m.id LIMIT ?", params)
        has_more = len(rows) > limit
        posts = []
        for row in rows[:limit]:
            post = dict(row)
            post["mentions"] = _l(post.get("mentions"))
            posts.append(post)
        next_since_id = int(posts[-1]["id"]) if posts else since_id
        agent_row = self.db.one("SELECT id, name, kind, note, state, doing, last_seen FROM agents WHERE id=?", (agent,))
        cursor = {"since_id": since_id, "next_since_id": next_since_id, "thread_ids": thread_ids or None,
                  "has_more": has_more, "limit": limit}
        return {"ok": True, "heartbeat": heartbeat, "agent_state": dict(agent_row) if agent_row else None,
                "inbox": inbox, "pending_reviews": inbox["reviews"], "requested_changes": inbox["changes"],
                "board": board, "leased_tasks": leased_tasks, "posts": posts, "cursor": cursor,
                "next_since_id": next_since_id}

    def agents(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        now = self.clock()
        out = []
        pools = self._quota_pools()
        for r in self.db.query("SELECT * FROM agents ORDER BY last_seen DESC"):
            d = dict(r)
            d["stale"] = (now - float(d.get("last_seen") or 0)) > STALE_AGENT_S
            d["quota"] = self._quota_state_from(d["id"], pools)
            mine = [dict(x) for x in self.db.query("SELECT * FROM locks WHERE owner=? AND expires>?", (d["id"], now))]
            d["locks"] = [x["resource"] for x in mine]
            d["lock_groups"] = [{k: g[k] for k in ("task_id", "kind", "repo", "count", "label")} for g in group_locks(mine)]
            out.append(d)
        return {"ok": True, "agents": out}

    # -- tasks ---------------------------------------------------------------------------------------------------

    def task_add(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        title = _txt(a.get("title"), 200, "title", required=True)
        body = _txt(a.get("body"), 20000, "body")
        kind = str(a.get("kind") or "feature").lower()
        if kind not in TASK_KINDS:
            raise AgoraError(f"kind must be one of {', '.join(TASK_KINDS)}")
        try:
            priority = int(a.get("priority", 2))
        except (TypeError, ValueError):
            raise AgoraError("priority must be 0 (urgent) .. 3 (someday)") from None
        if not 0 <= priority <= 3:
            raise AgoraError("priority must be 0 (urgent) .. 3 (someday)")
        paths = _list(a.get("paths"), "paths")
        repo = _txt(a.get("repo"), 120, "repo")
        blocked_by = self._dep_ids(a.get("blocked_by"), "blocked_by")
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            tid = self.db.insert("tasks", {"title": title, "body": body, "repo": repo, "paths": _j(paths), "kind": kind,
                                           "priority": priority, "status": "open", "created_by": agent,
                                           "created": now, "updated": now})
            th = self.db.insert("threads", {"title": title, "kind": "task", "task_id": tid, "status": "open",
                                            "created_by": agent, "created": now, "updated": now})
            self.db.execute("UPDATE tasks SET thread_id=? WHERE id=?", (th, tid))
            self._msg(th, agent, "proposal", body or title, _list(a.get("mentions"), "mentions"))
            if blocked_by:
                self._apply_deps(agent, self._task_row(tid), add=blocked_by, remove=[], replace=None)
        self._bump("agora.task.added", {"task_id": tid, "title": title, "by": agent, "repo": repo, "kind": kind})
        res = {"ok": True, "task": self._task_row(tid)}
        if a.get("claim"):
            res["claim"] = self.task_claim({"agent": agent, "task_id": tid, "locks": a.get("locks"), "ttl_s": a.get("ttl_s")})
        return res

    def task_claim(self, a: dict[str, Any]) -> dict[str, Any]:
        """Take a task and its locks, all or nothing. A task held by an agent silent for 6 hours can be taken with force."""
        agent = self._agent(a.get("agent"))
        ttl = self._ttl(a.get("ttl_s"))
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(a.get("task_id"))
            if task["status"] in FINAL:
                raise AgoraError(f"task {task['id']} is {task['status']}", 409)
            stale_note = self._require_fresh(agent, task, a, "claim")
            owner = task.get("owner")
            if owner and owner != agent and task["status"] in ACTIVE:
                if not (a.get("force") and self._agent_stale(owner)):
                    raise AgoraError(f"task {task['id']} is held by {owner}"
                                     + ("" if self._agent_stale(owner) else " (active)"), 409, owner=owner)
                self.db.execute("DELETE FROM locks WHERE owner=? AND task_id=?", (owner, task["id"]))
                self._system(task, f"{agent} toma la tarea: {owner} lleva más de 6 h sin dar señales.", agent)
            locks = _list(a.get("locks"), "locks") or [f"path:{task['repo']}/{p}" for p in task["paths"] if task.get("repo")]
            got = self._acquire(agent, locks, task["id"], ttl, task["title"][:120]) if locks else {"ok": True, "locks": []}
            if not got["ok"]:
                self._bump("agora.lock.conflict", {"agent": agent, "task_id": task["id"], "conflicts": got["conflicts"]})
                return {"ok": False, "status": 409, "error": "locks are held by someone else", "conflicts": got["conflicts"]}
            status = task["status"] if task["status"] in ACTIVE and owner == agent else "claimed"
            now = self.clock()
            self.db.execute("UPDATE tasks SET owner=?, status=?, claimed_at=?, updated=? WHERE id=?",
                            (agent, status, now, now, task["id"]))
            if owner != agent:
                self._system(task, f"{agent} reclama la tarea" + (f" con {', '.join(got['locks'])}" if got["locks"] else "") + ".", agent)
            if stale_note:
                self._system(task, stale_note, agent)
        self._bump("agora.task.claimed", {"task_id": task["id"], "agent": agent, "locks": got["locks"]})
        self._rearm_dependents(task["id"])
        fresh = self._task_row(task["id"])
        res = {"ok": True, "task": fresh, "locks": got["locks"]}
        if fresh["waiting_on"]:
            # Informs, never refuses: the owner may prepare work while the blockers finish.
            pending = [b for b in fresh["blocked_by"] if b["id"] in fresh["waiting_on"]]
            res["dep_warnings"] = [f"#{fresh['id']} espera todavía a " + ", ".join(
                f"#{b['id']} ({b['status']}" + (f", {b['owner']})" if b.get("owner") else ")") for b in pending)]
        return self._with_unseen(a, res)

    def _owned(self, a: dict[str, Any], *, allow_person: bool = True) -> tuple[str, dict[str, Any]]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")) and allow_person)
        task = self._task_row(a.get("task_id"))
        if task.get("owner") != agent and agent != PERSON:
            raise AgoraError(f"task {task['id']} belongs to {task.get('owner') or 'nobody'}: claim it first", 409)
        return agent, task

    def task_update(self, a: dict[str, Any]) -> dict[str, Any]:
        """Progress note, status in_progress/blocked, branch, more paths (with their locks)."""
        with self.db.tx():
            agent, task = self._owned(a)
            self._touch(agent)
            status = str(a.get("status") or task["status"]).lower()
            if status not in ("claimed", "in_progress", "blocked", task["status"]):
                raise AgoraError("task_update sets claimed, in_progress or blocked; use task_submit / task_done / task_release")
            now = self.clock()
            extra = _list(a.get("locks"), "locks")
            if extra:
                got = self._acquire(task["owner"] or agent, extra, task["id"], self._ttl(a.get("ttl_s")), task["title"][:120])
                if not got["ok"]:
                    return {"ok": False, "status": 409, "error": "locks are held by someone else", "conflicts": got["conflicts"]}
            fields: dict[str, Any] = {"status": status, "updated": now}
            if a.get("branch") is not None:
                fields["branch"] = _txt(a.get("branch"), 200, "branch")
            if a.get("paths") is not None:
                fields["paths"] = _j(sorted(set(task["paths"]) | set(_list(a.get("paths"), "paths"))))
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task["id"]))
            note = _txt(a.get("note"), 20000, "note")
            if note:
                self._msg(int(task["thread_id"]), agent, "comment", note)
            elif status != task["status"]:
                self._system(task, f"{agent}: {task['status']} → {status}.", agent)
        self._bump("agora.task.status", {"task_id": task["id"], "agent": agent, "status": status})
        self._rearm_dependents(task["id"])
        return {"ok": True, "task": self._task_row(task["id"])}

    def task_submit(self, a: dict[str, Any]) -> dict[str, Any]:
        """Ask for review: what changed, where (branch/commits) and how it was verified.

        Optional ``blocks``: structured review sections (what_why, paths, code_peek, flow, sequence).
        Each ``code_peek`` is verified with ``git show`` outside the write transaction (isolated clones via
        ``checkout`` when identity matches the registered repo). Plain text summaries stay valid without blocks.
        """
        summary = _txt(a.get("summary"), 20000, "summary", required=True)
        # Phase 1: authorize under a short write lock (no external I/O).
        with self.db.tx():
            agent, task = self._owned(a, allow_person=False)
            self._touch(agent)
            if task["status"] not in ACTIVE:
                raise AgoraError(f"task {task['id']} is {task['status']}", 409)
            reviewer = str(a.get("reviewer") or "").strip().lower() or None
            if reviewer == agent:
                raise AgoraError("the reviewer must be someone else")
            commits = _list(a.get("commits"), "commits") or list(task["commits"] or [])
            snap = {
                "id": int(task["id"]), "owner": agent, "status": task["status"],
                "submission_revision": int(task.get("submission_revision") or 0),
                "repo": str(task.get("repo") or ""), "thread_id": int(task["thread_id"]),
                "commits": commits, "branch": a.get("branch"), "reviewer": reviewer,
            }
        # Phase 2: verify peeks without holding the database write lock.
        blocks = normalize_submission_blocks(
            a.get("blocks"), repo_name=snap["repo"], commits=snap["commits"],
            repo_lookup=self.repo_lookup, repo_roots=self.repo_roots)
        # Phase 3: re-check owner/status/revision, then publish.
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(snap["id"])
            if task.get("owner") != agent:
                raise AgoraError("task owner changed during code_peek verification", 409,
                                 error_code="submit_conflict")
            if task["status"] not in ACTIVE:
                raise AgoraError(f"task {task['id']} is {task['status']}", 409, error_code="submit_conflict")
            if int(task.get("submission_revision") or 0) != snap["submission_revision"]:
                raise AgoraError("submission changed during code_peek verification", 409,
                                 error_code="submit_conflict",
                                 current_revision=task["submission_revision"],
                                 current_commits=task["commits"])
            stale_note = self._require_fresh(agent, task, a, "submit")
            now = self.clock()
            # A new submission asks for a new verdict: an approval given to earlier commits does not carry over.
            self.db.execute(
                "UPDATE tasks SET status='review', reviewer=?, branch=COALESCE(?, branch), commits=?, "
                "submission_blocks=?, submitted_at=?, updated=?, reviewed=0, reviewed_commits=NULL, "
                "reviewed_submission_revision=NULL, submission_revision=submission_revision+1 WHERE id=?",
                (snap["reviewer"], snap["branch"], _j(snap["commits"] or task["commits"]),
                 json.dumps(blocks, ensure_ascii=False) if blocks is not None else None,
                 now, now, task["id"]))
            task = self._task_row(task["id"])
            revision = task["submission_revision"]
            details = f"\n\nSubmission revision: {revision}\nCommits: {', '.join(task['commits']) or '(none)'}"
            if blocks:
                details += _format_blocks_for_message(blocks)
            if stale_note:
                self._system(task, stale_note, agent)
            self._msg(int(task["thread_id"]), agent, "proposal", summary + details,
                      [snap["reviewer"]] if snap["reviewer"] else [])
        self._bump("agora.task.review", {"task_id": task["id"], "agent": agent, "reviewer": snap["reviewer"],
                                          "submission_revision": revision, "commits": task["commits"],
                                          "blocks": len(blocks or [])})
        self._rearm_dependents(task["id"])          # a new submission is a new cycle for whoever waits on it
        out = {"ok": True, "task": self._task_row(task["id"])}
        warnings = self._quota_warnings([snap["reviewer"]] if snap["reviewer"] else [])
        if warnings:
            out["quota_warnings"] = warnings
        return self._with_unseen(a, out)

    def task_review(self, a: dict[str, Any]) -> dict[str, Any]:
        """Vote on the observed submission revision using expected_submission_revision."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        verdict = str(a.get("verdict") or "").lower()
        if verdict not in ("approve", "changes"):
            raise AgoraError("verdict must be approve or changes")
        body = _txt(a.get("body"), 20000, "body", required=True)
        expected_revision = a.get("expected_submission_revision")
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(a.get("task_id"))
            if task.get("owner") == agent:
                raise AgoraError("you can not review your own task", 403)
            if task["status"] not in ("review", "approved", "changes"):
                raise AgoraError(f"task {task['id']} is not waiting for review ({task['status']})", 409)
            if type(expected_revision) is not int or expected_revision < 1:
                raise AgoraError("expected_submission_revision must be an integer >= 1; read submission_revision from the task you inspected (CLI: task N, then review N --revision R)")
            if expected_revision != task["submission_revision"]:
                raise AgoraError("the submission changed after it was observed", 409, error_code="stale_submission",
                                 current_revision=task["submission_revision"], current_commits=task["commits"])
            stale_note = self._require_fresh(agent, task, a, "review")
            if stale_note:
                self._system(task, stale_note, agent)
            status = "approved" if verdict == "approve" else "changes"
            now = self.clock()
            self.db.execute("UPDATE tasks SET status=?, reviewer=?, reviewed=?, reviewed_commits=?, "
                            "reviewed_submission_revision=?, updated=? WHERE id=?",
                            (status, agent, 1 if verdict == "approve" else 0,
                             _j(task["commits"]) if verdict == "approve" else None, expected_revision, now, task["id"]))
            details = f"Reviewed submission revision: {expected_revision}\nCommits: {', '.join(task['commits']) or '(none)'}"
            block_notes = a.get("block_comments")
            if block_notes:
                if not isinstance(block_notes, list) or len(block_notes) > 40:
                    raise AgoraError("block_comments must be a list of at most 40 objects")
                known = {b.get("id") for b in (task.get("submission_blocks") or []) if isinstance(b, dict)}
                extra = ["", "Per-block notes:"]
                for i, note in enumerate(block_notes):
                    if not isinstance(note, dict):
                        raise AgoraError(f"block_comments[{i}] must be an object")
                    bid = str(note.get("block_id") or note.get("id") or "").strip()
                    text = _txt(note.get("body"), 4000, f"block_comments[{i}].body", required=True)
                    if not bid:
                        raise AgoraError(f"block_comments[{i}].block_id is required")
                    if known and bid not in known:
                        raise AgoraError(f"block_comments[{i}]: unknown block_id {bid!r}")
                    if not known:
                        raise AgoraError("block_comments require a structured submission with blocks")
                    extra.append(f"- [{bid}] {text}")
                details += "\n" + "\n".join(extra)
            self._msg(int(task["thread_id"]), agent, "approve" if verdict == "approve" else "changes",
                      details + "\n\n" + body, [task["owner"]] if task.get("owner") else [])
            if agent == PERSON:
                self.db.execute("UPDATE threads SET status='open' WHERE id=? AND status='escalated'", (task["thread_id"],))
        self._bump("agora.task.reviewed", {"task_id": task["id"], "agent": agent, "verdict": verdict,
                                            "submission_revision": expected_revision, "commits": task["commits"]})
        res = {"ok": True, "task": self._task_row(task["id"])}
        if verdict == "approve":
            res["unblocked"] = self._wake_dependents(task["id"])
        else:
            self._rearm_dependents(task["id"])
        return self._with_unseen(a, res)

    def task_done(self, a: dict[str, Any]) -> dict[str, Any]:
        """Integrated: commits and result. Releases the task's locks. Refused while a review is younger than 2 h
        unless ``force`` with a ``reason``; finishing without any review is recorded as such."""
        with self.db.tx():
            agent, task = self._owned(a)
            self._touch(agent)
            if task["status"] in FINAL:
                raise AgoraError(f"task {task['id']} is already {task['status']}", 409)
            stale_note = self._require_fresh(agent, task, a, "done")
            if stale_note:
                self._system(task, stale_note, agent)
            now = self.clock()
            if task["status"] == "review" and agent != PERSON:
                waited = now - float(task.get("submitted_at") or now)
                if waited < self.review_grace_s and not (a.get("force") and str(a.get("reason") or "").strip()):
                    raise AgoraError(f"task {task['id']} has been waiting for review {int(waited // 60)} min; wait "
                                     f"{int(self.review_grace_s // 60)} min or pass force with a reason", 409)
            if task["status"] == "changes" and not (a.get("force") and str(a.get("reason") or "").strip()):
                raise AgoraError(f"the reviewer asked for changes on task {task['id']}: address them and submit again, "
                                 "or pass force with a reason", 409)
            commits = _list(a.get("commits"), "commits") or task["commits"]
            result = _txt(a.get("result"), 20000, "result")
            exempt = _txt(a.get("exempt"), 300, "exempt")
            seen = task.get("reviewed_commits") or task["commits"] or []
            unreviewed_commits = [c for c in commits if c not in seen]
            if task["status"] == "approved" and task.get("reviewed_submission_revision") != task.get("submission_revision"):
                raise AgoraError(f"task {task['id']} approval does not match its current submission revision", 409)
            if task["status"] == "approved" and unreviewed_commits:
                # Closing with commits the reviewer never saw (an integration that rewrote hashes, or new work): say
                # how they relate to the approved ones, or submit them again.
                if not (a.get("force") and str(a.get("reason") or "").strip()):
                    raise AgoraError(f"task {task['id']} was approved on {', '.join(seen) or 'no commits'}; "
                                     f"{', '.join(unreviewed_commits)} were not reviewed: submit them, or pass force "
                                     "with a reason that says how they relate to the approved ones", 409)
                # The reviewer approved other hashes; the integrator declares these equivalent. Recorded as such, with
                # both lists and the reason, so nobody reads it as an approval of hashes the reviewer never saw.
                review_state = "equivalent"
            elif task["status"] == "approved":
                review_state = "approved"
            elif exempt or (task.get("kind") in EXEMPT_KINDS and task["status"] not in ("review", "changes")):
                if task.get("kind") not in EXEMPT_KINDS:
                    raise AgoraError(f"only {', '.join(EXEMPT_KINDS)} tasks can be exempt from review; task {task['id']} "
                                     f"is {task.get('kind')}: submit it for review or close it with force and a reason", 409)
                review_state = "exempt"
            else:
                review_state = "unreviewed"
            self.db.execute("UPDATE tasks SET status='done', commits=?, result=?, closed_at=?, updated=?, review_state=? "
                            "WHERE id=?", (_j(commits), result, now, now, review_state, task["id"]))
            self.db.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            unreviewed = review_state == "unreviewed"
            if review_state == "exempt":
                note = f"\n\n(Exenta de revisión: {exempt or task.get('kind')})"
            elif unreviewed:
                note = "\n\n(Integrada sin revisión aprobada" + (f": {a.get('reason')}" if a.get("reason") else "") + ")"
            elif review_state == "equivalent":
                note = (f"\n\n(Aprobada por {task.get('reviewer') or 'el revisor'} en {', '.join(seen)}; integrada como "
                        f"{', '.join(commits)}, equivalencia declarada por {agent}: {a.get('reason')})")
            else:
                note = ""
            text = (result or "Hecho.") + note
            self._msg(int(task["thread_id"]), agent, "resolution", text)
            self.db.execute("UPDATE threads SET status='resolved', resolution=?, resolved_by=?, updated=? WHERE id=?",
                            (result or "done", agent, now, task["thread_id"]))
        self._bump("agora.task.done", {"task_id": task["id"], "agent": agent, "commits": commits,
                                       "reviewed": review_state in ("approved", "equivalent"), "review_state": review_state,
                                       "reviewed_commits": seen if review_state in ("approved", "equivalent") else []})
        return self._with_unseen(a, {"ok": True, "task": self._task_row(task["id"]),
                                     "unblocked": self._wake_dependents(task["id"])})

    def task_release(self, a: dict[str, Any]) -> dict[str, Any]:
        """Give the task back (``open``) or drop it (``drop: true``) with a reason. Releases its locks."""
        reason = _txt(a.get("reason"), 5000, "reason")
        with self.db.tx():
            agent, task = self._owned(a)
            if task["status"] in FINAL:
                # A dropped task keeps its owner; releasing it again must not bring it back to open.
                raise AgoraError(f"task {task['id']} is already {task['status']}", 409)
            self._touch(agent)
            now = self.clock()
            drop = bool(a.get("drop"))
            self.db.execute("UPDATE tasks SET status=?, owner=?, updated=?, closed_at=? WHERE id=?",
                            ("dropped" if drop else "open", None if not drop else task.get("owner"), now,
                             now if drop else None, task["id"]))
            self.db.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            self._msg(int(task["thread_id"]), agent, "system" if not reason else "comment",
                      reason or f"{agent} suelta la tarea.")
        self._bump("agora.task.released", {"task_id": task["id"], "agent": agent, "dropped": drop})
        res = {"ok": True, "task": self._task_row(task["id"])}
        if drop:
            res["unblocked"] = self._wake_dependents(task["id"])
        else:
            self._rearm_dependents(task["id"])
        return res

    # -- dependencies --------------------------------------------------------------------------------------------

    @staticmethod
    def _dep_ids(value: Any, field: str) -> list[int]:
        """Task ids from a list, a number or a text like '12, #13 14'."""
        if value is None or value == "":
            return []
        if isinstance(value, int):
            value = [value]
        elif isinstance(value, str):
            value = value.replace(",", " ").split()
        if not isinstance(value, list) or len(value) > 50:
            raise AgoraError(f"{field} must be a list of at most 50 task ids")
        out: list[int] = []
        for v in value:
            try:
                n = int(str(v).strip().lstrip("#"))
            except (TypeError, ValueError):
                raise AgoraError(f"{field}: {v!r} is not a task id") from None
            if n not in out:
                out.append(n)
        return out

    def _dep_loop(self, task_id: int, blocker_id: int) -> Optional[list[int]]:
        """If ``blocker_id`` already waits (directly or through others) for ``task_id``, the chain that proves it."""
        stack = [(blocker_id, [blocker_id])]
        seen: set[int] = set()
        while stack:
            cur, path = stack.pop()
            if cur == task_id:
                return path
            if cur in seen:
                continue
            seen.add(cur)
            for r in self.db.query("SELECT blocker_id FROM agora_task_deps WHERE task_id=?", (cur,)):
                stack.append((int(r["blocker_id"]), path + [int(r["blocker_id"])]))
        return None

    def _dep_key(self, task: dict[str, Any]) -> str:
        return f"deps:{task['id']}:" + ",".join(str(b["id"]) for b in task["blocked_by"])

    def _rearm_dependents(self, blocker_id: int) -> None:
        """A blocker went back to work (claimed, in progress, review, changes, blocked, open): its dependents are
        waiting again, so the next time everything is through is a new cycle and wakes them again. Within one cycle
        (approved → done) nothing is re-armed and the wake-up stays single."""
        row = self.db.one("SELECT status FROM tasks WHERE id=?", (int(blocker_id),))
        if row is None or row["status"] in DEP_CLEAR:
            return
        self.db.execute("DELETE FROM agora_dep_wakeups WHERE task_id IN "
                        "(SELECT task_id FROM agora_task_deps WHERE blocker_id=?)", (int(blocker_id),))

    def _apply_deps(self, agent: str, task: dict[str, Any], *, add: list[int], remove: list[int],
                    replace: Optional[list[int]]) -> dict[str, list[int]]:
        """Change what ``task`` waits for inside the caller's transaction; refuses itself, unknown ids and loops."""
        tid = int(task["id"])
        was_waiting = set(task.get("waiting_on") or [])
        current = {int(r["blocker_id"]) for r in self.db.query("SELECT blocker_id FROM agora_task_deps WHERE task_id=?", (tid,))}
        target = set(replace) if replace is not None else (current | set(add)) - set(remove)
        new, gone = sorted(target - current), sorted(current - target)
        now = self.clock()
        for b in new:
            if b == tid:
                raise AgoraError(f"task {tid} can not wait for itself")
            self._task_row(b)
            loop = self._dep_loop(tid, b)
            if loop:
                chain = " → ".join(f"#{x}" for x in [tid, *loop])
                raise AgoraError(f"#{tid} can not wait for #{b}: that closes a loop ({chain})", 409, cycle=[tid, *loop])
            self.db.execute("INSERT INTO agora_task_deps(task_id, blocker_id, created_by, created) VALUES(?,?,?,?)",
                            (tid, b, agent, now))
        for b in gone:
            self.db.execute("DELETE FROM agora_task_deps WHERE task_id=? AND blocker_id=?", (tid, b))
        if new or gone:
            parts = ([f"espera a {', '.join(f'#{b}' for b in new)}"] if new else []) + \
                    ([f"ya no espera a {', '.join(f'#{b}' for b in gone)}"] if gone else [])
            self._system(task, f"{agent}: #{tid} " + "; ".join(parts) + ".", agent)
            fresh = self._task_row(tid)
            if fresh["blocked_by"] and not fresh["waiting_on"]:
                # Everything it waits for is already through: nothing to wake later for this set.
                self.db.execute("INSERT OR IGNORE INTO agora_dep_wakeups(key, task_id, created) VALUES(?,?,?)",
                                (self._dep_key(fresh), tid, now))
            if was_waiting & set(gone) and not fresh["waiting_on"] and fresh["status"] == "blocked":
                # It was blocked waiting for what was just removed: nothing left to wait for, back to work.
                self.db.execute("UPDATE tasks SET status=?, updated=? WHERE id=?",
                                ("claimed" if fresh.get("owner") else "open", now, tid))
                self._system(fresh, f"#{tid} ya no espera a nada: vuelve a "
                             + ("claimed." if fresh.get("owner") else "open, libre para reclamar."), agent)
        return {"added": new, "removed": gone}

    def task_deps(self, a: dict[str, Any]) -> dict[str, Any]:
        """Say what a task waits for: ``add`` / ``remove`` blockers or ``set`` the whole list ([] clears it). Anyone
        may do it. When every blocker reaches approved, done or dropped the task's owner is woken once (or the task is
        announced as ready to claim)."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        add, remove = self._dep_ids(a.get("add"), "add"), self._dep_ids(a.get("remove"), "remove")
        replace = self._dep_ids(a.get("set"), "set") if a.get("set") is not None else None
        if not add and not remove and replace is None:
            raise AgoraError("pass add, remove or set")
        with self.db.tx():
            self._touch(agent)
            task = self._task_row(a.get("task_id"))
            changed = self._apply_deps(agent, task, add=add, remove=remove, replace=replace)
        if changed["added"] or changed["removed"]:
            self._bump("agora.task.deps", {"task_id": task["id"], "agent": agent, **changed})
        return {"ok": True, **changed, "task": self._task_row(task["id"])}

    def _wake_dependents(self, blocker_id: Optional[int] = None) -> list[int]:
        """Wake each task whose blockers are now all through, once per set of blockers. With ``blocker_id`` only its
        dependents are looked at; without it, all (the periodic backup)."""
        if blocker_id is None:
            rows = self.db.query("SELECT DISTINCT task_id FROM agora_task_deps ORDER BY task_id")
        else:
            rows = self.db.query("SELECT task_id FROM agora_task_deps WHERE blocker_id=? ORDER BY task_id", (int(blocker_id),))
        woke: list[int] = []
        now = self.clock()
        with self.db.tx():
            for r in rows:
                t = self._task_row(r["task_id"])
                if t["waiting_on"]:
                    # Waiting again (a hook may have missed the blocker leaving approved/done): re-arm the next cycle.
                    self.db.execute("DELETE FROM agora_dep_wakeups WHERE task_id=?", (t["id"],))
                    continue
                if t["status"] in FINAL or not t["blocked_by"]:
                    continue
                key = self._dep_key(t)
                if self.db.one("SELECT key FROM agora_dep_wakeups WHERE key=?", (key,)) is not None:
                    continue
                self.db.execute("INSERT INTO agora_dep_wakeups(key, task_id, created) VALUES(?,?,?)", (key, t["id"], now))
                owner = t.get("owner")
                if t["status"] == "blocked":
                    self.db.execute("UPDATE tasks SET status=?, updated=? WHERE id=?",
                                    ("claimed" if owner else "open", now, t["id"]))
                ids = ", ".join(f"#{b['id']}" for b in t["blocked_by"])
                text = f"Desbloqueada: {ids} " + ("ya está lista" if len(t["blocked_by"]) == 1 else "ya están listas") + \
                       (f". {owner}, puedes seguir con #{t['id']}." if owner else f". #{t['id']} queda libre para reclamar.")
                who = owner or t.get("created_by")
                self._msg(int(t["thread_id"]), "system", "system", text, [who] if who else [])
                woke.append(int(t["id"]))
        for tid in woke:
            self._bump("agora.task.unblocked", {"task_id": tid, "by": blocker_id})
        return woke

    # -- handover ------------------------------------------------------------------------------------------------

    @staticmethod
    def _flag(value: Any, default: bool) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @staticmethod
    def _handover_agent(value: Any, field: str) -> str:
        agent = str(value or "").strip().lower()
        if agent == PERSON:
            raise AgoraError(f"{field} must be an agent: {PERSON!r} does not own tasks to hand over")
        if not _AGENT_RE.match(agent) or agent in ("hub", "ui", "system"):
            raise AgoraError(f"{field} is required: the id of an agent such as codex-relevo")
        return agent

    def handover(self, a: dict[str, Any]) -> dict[str, Any]:
        """Move the half-done work of one agent to another one (a chat that died, a context that ran out).

        Moves the ownership of every task of ``from_agent`` that is not done (status, review state, submission
        revision, reviewed commits, checkpoints and history stay as they are), the ``from_agent`` locks attached to
        those tasks (same resources, TTL renewed) and, with ``include_reviews`` (default), the reviewer role of the
        tasks waiting for it (``review`` / ``changes``). ``tasks`` limits the operation to those ids.

        Allowed for the person (the hub's page), for ``from_agent`` itself, and for any registered agent once
        ``from_agent`` has sent no heartbeat for ``handover_idle_s`` (30 min). ``force`` skips that wait for any
        registered agent: Luis decides who works on what, and an agent acting on his order (a source out of quota
        that keeps sending heartbeats, work that must move now) takes the work at once; the forced move is written
        in every task thread and in the coordination thread with its reason. ``to_agent`` must be registered and,
        unless the person asks, have a fresh heartbeat. One transaction; running it again changes nothing."""
        caller = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        source = self._handover_agent(a.get("from_agent"), "from_agent")
        target = self._handover_agent(a.get("to_agent"), "to_agent")
        if source == target:
            raise AgoraError("from_agent and to_agent must be different agents")
        reason = _txt(a.get("reason"), 2000, "reason", required=True)
        include_reviews = self._flag(a.get("include_reviews"), True)
        force = self._flag(a.get("force"), False)
        wanted: Optional[list[int]] = None
        if a.get("tasks") not in (None, "", []):
            wanted = []
            for value in _list(a.get("tasks"), "tasks", 200):
                try:
                    tid = int(str(value).lstrip("#"))
                except ValueError:
                    raise AgoraError(f"tasks must be task numbers: {value!r}") from None
                if tid < 1:
                    raise AgoraError(f"tasks must be task numbers: {value!r}")
                if tid not in wanted:
                    wanted.append(tid)
        final_sql = ",".join("?" * len(FINAL))
        with self.db.tx():
            now = self.clock()
            # -- who may do it
            if caller != PERSON and self.db.one("SELECT id FROM agents WHERE id=?", (caller,)) is None:
                raise AgoraError(f"{caller} is not registered in the Agora: send a heartbeat first", 403)
            forced_active = False
            if caller not in (PERSON, source):
                seen = self.db.one("SELECT last_seen FROM agents WHERE id=?", (source,))
                idle = now - float(seen["last_seen"] or 0) if seen is not None else None
                if force and idle is not None and idle < self.handover_idle_s:
                    forced_active = True
                elif idle is not None and idle < self.handover_idle_s:
                    left = self.handover_idle_s - idle
                    raise AgoraError(
                        f"{source} sent a heartbeat {int(idle // 60)} min ago: another agent can take its work over after "
                        f"{int(self.handover_idle_s // 60)} min without one (about {math.ceil(left / 60)} min from now); "
                        f"{PERSON} can do it right away from the Hub", 409, idle_s=int(idle), retry_after_s=math.ceil(left),
                        required_idle_s=int(self.handover_idle_s))
            # -- who receives it
            row = self.db.one("SELECT last_seen FROM agents WHERE id=?", (target,))
            if row is None:
                raise AgoraError(f"{target} is not registered in the Agora: it must send a heartbeat before it takes work over", 409)
            if caller != PERSON and (now - float(row["last_seen"] or 0)) > STALE_AGENT_S:
                raise AgoraError(f"{target} has no recent heartbeat (more than {int(STALE_AGENT_S // 3600)} h): "
                                 "hand over to an agent that is working now", 409)
            # -- what moves
            if wanted is None:
                review_sql = " OR (reviewer=? AND status IN ('review','changes'))" if include_reviews else ""
                candidates = self.db.query(
                    f"SELECT * FROM tasks WHERE status NOT IN ({final_sql}) AND (owner=?{review_sql}) ORDER BY id",
                    (*FINAL, source, *((source,) if include_reviews else ())))
            else:
                candidates = []
                for tid in wanted:
                    found = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
                    if found is None:
                        raise AgoraError(f"no task {tid}", 404)
                    candidates.append(found)
            plan: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for task in candidates:
                task = dict(task)
                if task["status"] in FINAL:
                    skipped.append({"task_id": task["id"], "reason": f"{task['status']}: final tasks are never touched"})
                    continue
                new_owner = target if task.get("owner") == source else task.get("owner")
                new_reviewer = task.get("reviewer")
                if include_reviews and new_reviewer == source and task["status"] in ("review", "changes"):
                    new_reviewer = target
                if task["status"] in ("review", "changes") and new_reviewer and new_reviewer == new_owner \
                        and (new_owner != task.get("owner") or new_reviewer != task.get("reviewer")):
                    new_reviewer = None                      # an owner can not review its own task: any other agent can
                if new_owner == task.get("owner") and new_reviewer == task.get("reviewer"):
                    if target in (task.get("owner"), task.get("reviewer")):
                        skipped.append({"task_id": task["id"], "reason": f"already with {target}"})
                        continue
                    raise AgoraError(f"task {task['id']} is not held or reviewed by {source} (owner "
                                     f"{task.get('owner') or 'nobody'}, reviewer {task.get('reviewer') or 'nobody'})", 409)
                plan.append({"task": task, "owner": new_owner, "reviewer": new_reviewer,
                             "owner_moved": new_owner != task.get("owner"),
                             "reviewer_moved": new_reviewer != task.get("reviewer")})
            if not plan:
                return {"ok": True, "noop": True, "by": caller, "from": source, "to": target, "reason": reason,
                        "tasks": [], "owner_moved": [], "reviewer_moved": [], "locks_moved": [], "skipped": skipped,
                        "thread_id": None}
            if caller != PERSON:
                self._touch(caller)
            # -- apply
            owner_moved = [p["task"]["id"] for p in plan if p["owner_moved"]]
            reviewer_moved = [p["task"]["id"] for p in plan if p["reviewer_moved"]]
            for p in plan:
                self.db.execute("UPDATE tasks SET owner=?, reviewer=?, updated=? WHERE id=?",
                                (p["owner"], p["reviewer"], now, p["task"]["id"]))
            locks_moved: list[str] = []
            locks_by_task: dict[int, list[str]] = {}
            affected = [p["task"]["id"] for p in plan]          # owner moves and reviewer-only moves alike
            if affected:
                marks = ",".join("?" * len(affected))
                live = self.db.query(
                    f"SELECT resource, ttl_s, task_id FROM locks WHERE owner=? AND expires>? AND task_id IN ({marks}) "
                    "ORDER BY acquired, resource", (source, now, *affected))
                for lk in live:
                    self.db.execute("UPDATE locks SET owner=?, expires=? WHERE resource=?",
                                    (target, now + float(lk["ttl_s"] or DEFAULT_LOCK_TTL_S), lk["resource"]))
                    locks_moved.append(lk["resource"])
                    locks_by_task.setdefault(int(lk["task_id"]), []).append(lk["resource"])
            forced_note = f" (forzado: {source} seguía mandando latidos)" if forced_active else ""
            for p in plan:
                task = p["task"]
                lines = [f"{caller} traspasa de {source} a {target}{forced_note}: {reason}"]
                if p["owner_moved"]:
                    lines.append(f"Propiedad: {source} → {target} (estado {task['status']}, entrega r{task.get('submission_revision', 0)})")
                if p["reviewer_moved"]:
                    lines.append(f"Revisión: {task.get('reviewer')} → {p['reviewer'] or 'cualquier otro agente'}")
                held = locks_by_task.get(task["id"], [])
                if held:
                    lines.append("Bloqueos: " + ", ".join(held))
                self._system(task, "\n".join(lines), caller)
            summary = [f"{caller} traspasa de {source} a {target}{forced_note}: {reason}"]
            if owner_moved:
                summary.append("Tareas con nuevo propietario: " + ", ".join(f"#{i}" for i in owner_moved))
            only_review = [i for i in reviewer_moved if i not in owner_moved]
            if only_review:
                summary.append("Revisiones traspasadas: " + ", ".join(f"#{i}" for i in only_review))
            summary.append(f"Bloqueos movidos: {len(locks_moved)}")
            summary.append(f"@{target}: retoma con sync; los hilos de cada tarea llevan el detalle.")
            body = "\n".join(summary)
            thread_id = self.handover_thread_id
            if thread_id is None or self.db.one("SELECT id FROM threads WHERE id=?", (thread_id,)) is None:
                thread_id = self.db.insert("threads", {"title": f"Traspaso de tareas: {source} → {target}", "kind": "note",
                                                       "task_id": None, "status": "open", "created_by": caller,
                                                       "created": now, "updated": now})
                self._msg(thread_id, caller, "proposal", body, [target])
            else:
                self._msg(thread_id, caller, "comment", body, [target])
            result_tasks = [self._task_dict(self.db.one("SELECT * FROM tasks WHERE id=?", (p["task"]["id"],))) for p in plan]
        self._bump("agora.handover", {"by": caller, "from": source, "to": target, "reason": reason,
                                       "tasks": owner_moved, "reviews": reviewer_moved, "locks": locks_moved,
                                       "thread_id": thread_id, "forced": forced_active})
        out = {"ok": True, "noop": False, "by": caller, "from": source, "to": target, "reason": reason,
               "forced": forced_active, "tasks": result_tasks, "owner_moved": owner_moved, "reviewer_moved": reviewer_moved,
               "locks_moved": locks_moved, "skipped": skipped, "thread_id": thread_id}
        warnings = self._quota_warnings([target])
        if warnings:
            out["quota_warnings"] = warnings
        return out

    def tasks(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        where, params = [], []
        status = a.get("status")
        if status == "active":
            where.append(f"status IN ({','.join('?' * len(ACTIVE))})")
            params.extend(ACTIVE)
        elif status:
            where.append("status=?")
            params.append(str(status))
        for key in ("owner", "repo", "kind"):
            if a.get(key):
                where.append(f"lower({key})=lower(?)")
                params.append(str(a[key]))
        limit = max(1, min(int(a.get("limit") or 200), 500))
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY CASE WHEN status IN ('done','dropped') THEN 1 ELSE 0 END, priority, updated DESC LIMIT ?"
        return {"ok": True, "tasks": [self._task_dict(r) for r in self.db.query(sql, (*params, limit))]}

    def task(self, a: dict[str, Any]) -> dict[str, Any]:
        task = self._task_row(a.get("task_id"))
        msgs = self._messages(int(task["thread_id"])) if task.get("thread_id") else []
        if a.get("agent") and task.get("thread_id"):
            # Reading the task with its whole conversation counts as reading its thread (A1).
            # as the person only from the hub's page, like thread(); agents read with their own id
            reader = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
            with self.db.tx():
                self._ack(reader, int(task["thread_id"]), max((m["id"] for m in msgs), default=0) or None)
        locks = [dict(r) for r in self.db.query("SELECT * FROM locks WHERE task_id=? AND expires>?", (task["id"], self.clock()))]
        return {"ok": True, "task": task, "messages": msgs, "locks": locks,
                "timeline": self.timeline({"task_id": task["id"], "limit": 100})}

    # -- threads -------------------------------------------------------------------------------------------------

    def _messages(self, thread_id: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,)):
            d = dict(r)
            d["mentions"] = _l(d.get("mentions"))
            out.append(d)
        return out

    def thread_open(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        title = _txt(a.get("title"), 200, "title", required=True)
        body = _txt(a.get("body"), 20000, "body", required=True)
        kind = str(a.get("kind") or "debate").lower()
        if kind not in THREAD_KINDS or kind == "task":
            raise AgoraError(f"kind must be one of {', '.join(k for k in THREAD_KINDS if k != 'task')}")
        task_id = None
        if a.get("task_id") not in (None, ""):
            task_id = self._task_row(a.get("task_id"))["id"]
        mentions = _list(a.get("mentions"), "mentions")
        now = self.clock()
        with self.db.tx():
            self._touch(agent)
            th = self.db.insert("threads", {"title": title, "kind": kind, "task_id": task_id, "status": "open",
                                            "created_by": agent, "created": now, "updated": now})
            self._msg(th, agent, "proposal", body, mentions)
        self._bump("agora.thread.opened", {"thread_id": th, "kind": kind, "title": title, "by": agent})
        if a.get("escalate"):
            return self.escalate({"agent": agent, "thread_id": th, "question": body, "_person": a.get("_person")})
        out = {"ok": True, "thread": self._thread_row(th)}
        warnings = self._quota_warnings(mentions)
        if warnings:
            out["quota_warnings"] = warnings
        return out

    def post(self, a: dict[str, Any]) -> dict[str, Any]:
        person = bool(a.get("_person"))
        agent = self._agent(a.get("agent"), allow_person=person)
        body = _txt(a.get("body"), 20000, "body", required=True)
        kind = str(a.get("kind") or "comment").lower()
        if kind not in ("comment", "proposal", "agree", "disagree"):
            raise AgoraError("kind must be comment, proposal, agree or disagree (reviews go through task_review, "
                             "resolutions through resolve)")
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            if th.get("task_id"):
                stale_note = self._require_fresh(agent, self._task_row(th["task_id"]), a, "post")
                if stale_note:
                    self._msg(th["id"], agent, "system", stale_note)
            mid = self._msg(th["id"], agent, kind, body, _list(a.get("mentions"), "mentions"))
            self._ack(agent, th["id"], mid)
            if (th["status"] == "resolved" and kind in ("disagree", "proposal")) or \
                    (th["status"] == "escalated" and agent == PERSON):
                self.db.execute("UPDATE threads SET status='open' WHERE id=?", (th["id"],))
        self._bump("agora.thread.message", {"thread_id": th["id"], "message_id": mid, "author": agent, "kind": kind})
        out = {"ok": True, "message_id": mid, "thread": self._thread_row(th["id"])}
        warnings = self._quota_warnings(_list(a.get("mentions"), "mentions"))
        if warnings:
            out["quota_warnings"] = warnings
        return self._with_unseen(a, out)

    def resolve(self, a: dict[str, Any]) -> dict[str, Any]:
        """Close a thread with the resolution in writing. Resolved debates and decisions are the decision log."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        resolution = _txt(a.get("resolution"), 20000, "resolution", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            if th["kind"] == "task":
                raise AgoraError("a task thread closes with task_done / task_release", 409)
            now = self.clock()
            self._msg(th["id"], agent, "resolution", resolution)
            self.db.execute("UPDATE threads SET status='resolved', resolution=?, resolved_by=?, updated=? WHERE id=?",
                            (resolution, agent, now, th["id"]))
        self._bump("agora.thread.resolved", {"thread_id": th["id"], "by": agent, "title": th["title"]})
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def escalate(self, a: dict[str, Any]) -> dict[str, Any]:
        """No agreement (or it is the person's call): the thread waits for the person, who is notified."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        question = _txt(a.get("question"), 5000, "question", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            now = self.clock()
            self._msg(th["id"], agent, "escalation", question, [PERSON])
            self.db.execute("UPDATE threads SET status='escalated', updated=? WHERE id=?", (now, th["id"]))
        self._bump("agora.thread.escalated", {"thread_id": th["id"], "by": agent, "title": th["title"]})
        if self.escalate_hook:
            try:
                self.escalate_hook(th, agent, question)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def reopen(self, a: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        reason = _txt(a.get("reason"), 5000, "reason", required=True)
        with self.db.tx():
            self._touch(agent)
            th = self._thread_row(a.get("thread_id"))
            self._msg(th["id"], agent, "comment", reason)
            self.db.execute("UPDATE threads SET status='open', updated=? WHERE id=?", (self.clock(), th["id"]))
        self._bump("agora.thread.reopened", {"thread_id": th["id"], "by": agent})
        return {"ok": True, "thread": self._thread_row(th["id"])}

    def threads(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        where, params = [], []
        if a.get("status"):
            where.append("t.status=?")
            params.append(str(a["status"]))
        if a.get("kind"):
            where.append("t.kind=?")
            params.append(str(a["kind"]))
        elif not a.get("include_tasks"):
            where.append("(t.kind<>'task' OR t.status='escalated')")
        limit = max(1, min(int(a.get("limit") or 100), 500))
        # With an agent, each thread also says what that agent has not seen: messages by others (bookkeeping lines
        # aside) newer than its ack of the thread and its inbox read mark. Reading the list marks nothing.
        viewer = str(a.get("agent") or "").strip().lower()
        unread_sql, unread_params = "", []
        if viewer and _AGENT_RE.match(viewer):
            mark = self.db.one("SELECT last_msg_id FROM reads WHERE agent=?", (viewer,))
            seen = ("MAX(COALESCE((SELECT upto FROM acks k WHERE k.agent=? AND k.thread_id=t.id), 0), ?)")
            unread_sql = (f", {seen} AS seen_upto, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id "
                          f"AND m.author<>? AND m.kind<>'system' AND m.id > {seen}) AS unread")
            last = int(mark["last_msg_id"]) if mark else 0
            unread_params = [viewer, last, viewer, viewer, last]
        sql = ("SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS messages, "
               "(SELECT author FROM messages m WHERE m.thread_id=t.id ORDER BY id DESC LIMIT 1) AS last_author"
               + unread_sql + " FROM threads t" + (" WHERE " + " AND ".join(where) if where else "") +
               " ORDER BY CASE t.status WHEN 'escalated' THEN 0 WHEN 'open' THEN 1 ELSE 2 END, t.updated DESC LIMIT ?")
        return {"ok": True, "threads": [dict(r) for r in self.db.query(sql, (*unread_params, *params, limit))]}

    def ack(self, a: dict[str, Any]) -> dict[str, Any]:
        """Mark a thread (or every thread with ``all``) as seen: its mentions of the agent stop being pending."""
        agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
        if a.get("all"):
            ids = [r["thread_id"] for r in self.db.query(
                "SELECT DISTINCT thread_id FROM messages WHERE mentions LIKE ?", (f'%"{agent}"%',))]
        else:
            ids = [self._thread_row(a.get("thread_id"))["id"]]
        with self.db.tx():
            for tid in ids:
                self._ack(agent, tid)
        return {"ok": True, "acked": ids}

    def thread(self, a: dict[str, Any]) -> dict[str, Any]:
        th = self._thread_row(a.get("thread_id"))
        seen_before = None
        if a.get("agent"):
            reader = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
            # what the reader had seen before this read, so a page can mark what is new to it
            ack = self.db.one("SELECT upto FROM acks WHERE agent=? AND thread_id=?", (reader, th["id"]))
            mark = self.db.one("SELECT last_msg_id FROM reads WHERE agent=?", (reader,))
            seen_before = max(int(ack["upto"]) if ack else 0, int(mark["last_msg_id"]) if mark else 0)
            self._ack(reader, th["id"])
        stances: dict[str, str] = {}
        msgs = self._messages(th["id"])
        for m in msgs:
            if m["kind"] in ("agree", "disagree", "proposal"):
                stances[m["author"]] = m["kind"]
        out = {"ok": True, "thread": th, "messages": msgs, "stances": stances,
               "task": self._task_row(th["task_id"]) if th.get("task_id") else None}
        if seen_before is not None:
            out["seen_upto"] = seen_before
        if out["task"] and out["task"]["thread_id"] == th["id"]:
            out["timeline"] = self.timeline({"task_id": th["task_id"], "limit": 100})
        return out

    def thread_recent(self, a: dict[str, Any]) -> dict[str, Any]:
        """A bounded newest-first page, returned chronologically, without ACK.

        This is separate from the legacy full-thread reader: callers must not
        mistake a recent page for coverage of the unrequested older history.
        """
        if not isinstance(a, dict) or set(a) - {"thread_id", "limit", "before_id"}:
            raise AgoraError("recent thread reading accepts only thread_id, limit and before_id")
        thread_id = a.get("thread_id")
        limit = a.get("limit", 20)
        before = a.get("before_id")
        if type(thread_id) is not int or thread_id < 1:
            raise AgoraError("thread_id must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AgoraError("limit must be an integer between 1 and 200")
        if before is not None and (type(before) is not int or before < 1):
            raise AgoraError("before_id must be a positive integer")
        th = self._thread_row(thread_id)
        where, params = "thread_id=?", [thread_id]
        if before is not None:
            where += " AND id<?"
            params.append(before)
        rows = self.db.query("SELECT * FROM messages WHERE " + where + " ORDER BY id DESC LIMIT ?",
                             (*params, limit + 1))
        has_more = len(rows) > limit
        import hashlib
        msgs, stances = [], {}
        body_budget = 200_000
        for row in rows[:limit]:
            message = dict(row)
            message["mentions"] = _l(message.get("mentions"))
            body = str(message.get("body") or "")
            body_limit = min(20_000, body_budget)
            message.update(body=body[:body_limit], body_chars=len(body),
                           body_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                           body_truncated=len(body) > body_limit)
            body_budget -= len(message["body"])
            msgs.append(message)
        msgs.reverse()
        for message in msgs:
            if message["kind"] in ("agree", "disagree", "proposal"):
                stances[message["author"]] = message["kind"]
        return {"ok": True, "thread": th, "messages": msgs, "stances": stances, "stances_scope": "page",
                "task": self._task_row(th["task_id"]) if th.get("task_id") else None,
                "pagination": {"limit": limit, "before_id": before, "has_more": has_more,
                               "next_before_id": msgs[0]["id"] if has_more else None,
                               "body_char_limit": 20_000, "page_body_char_limit": 200_000},
                "hub_acknowledged": False}

    def decisions(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        limit = max(1, min(int((a or {}).get("limit") or 50), 500))
        rows = self.db.query("SELECT id, title, kind, resolution, resolved_by, updated FROM threads "
                             "WHERE status='resolved' AND kind<>'task' ORDER BY updated DESC LIMIT ?", (limit,))
        return {"ok": True, "decisions": [dict(r) for r in rows]}

    # -- inbox ---------------------------------------------------------------------------------------------------

    def inbox(self, a: dict[str, Any]) -> dict[str, Any]:
        """What is new for this agent: messages from others since the last read (``for_you`` when it is mentioned,
        owns the task or took part), reviews waiting for it, changes asked of it, escalations answered.
        ``wait_s`` (≤120) waits for something new; ``peek`` does not move the read mark."""
        agent = self._agent(a.get("agent"), allow_person=True)
        wait = 0.0
        try:
            wait = max(0.0, min(float(a.get("wait_s") or 0), MAX_WAIT_S))
        except (TypeError, ValueError):
            pass
        deadline = self.clock() + wait
        while True:
            with self._changed:
                version = self._version
            out = self._inbox_once(agent, a)
            if out["counts"]["messages"] or wait <= 0 or self.clock() >= deadline:
                break
            with self._changed:
                if self._version == version:
                    self._changed.wait(timeout=max(0.05, min(5.0, deadline - self.clock())))
        if a.get("metadata_only"):
            # A2: who wrote, where and what kind, without bodies; the body is read with thread/task (which marks it
            # seen). The read mark does not move: nothing was delivered.
            return project_inbox_metadata(out)
        if not a.get("peek") and out["messages"]:
            upto = max(m["id"] for m in out["messages"])
            self.db.execute("INSERT INTO reads(agent, last_msg_id, updated) VALUES(?,?,?) ON CONFLICT(agent) DO "
                            "UPDATE SET last_msg_id=MAX(reads.last_msg_id, excluded.last_msg_id), updated=excluded.updated",
                            (agent, upto, self.clock()))
        return out

    def _inbox_once(self, agent: str, a: dict[str, Any]) -> dict[str, Any]:
        row = self.db.one("SELECT last_msg_id FROM reads WHERE agent=?", (agent,))
        since = int(a.get("since_id") or (row["last_msg_id"] if row else 0))
        limit = max(1, min(int(a.get("limit") or 100), 500))
        msgs = []
        rows = self.db.query(
            "SELECT m.*, t.title AS thread_title, t.kind AS thread_kind, t.status AS thread_status, t.task_id AS task_id, "
            "t.created_by AS thread_by, k.owner AS task_owner FROM messages m JOIN threads t ON t.id=m.thread_id "
            "LEFT JOIN tasks k ON k.id=t.task_id WHERE m.id>? AND m.author<>? ORDER BY m.id LIMIT ?",
            (since, agent, limit))
        took_part = {r["thread_id"] for r in self.db.query("SELECT DISTINCT thread_id FROM messages WHERE author=?", (agent,))}
        for r in rows:
            d = dict(r)
            d["mentions"] = _l(d.get("mentions"))
            d["for_you"] = (agent in d["mentions"] or d.get("task_owner") == agent or d["thread_id"] in took_part
                            or d.get("thread_by") == agent or d["thread_kind"] in ("debate", "question", "decision", "handoff")
                            or d["author"] == PERSON)
            msgs.append(d)
        if a.get("mine_only"):
            msgs = [m for m in msgs if m["for_you"]]
        reviews = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE status='review' AND owner<>? AND (reviewer IS NULL OR reviewer='' OR reviewer=?) "
            "ORDER BY submitted_at", (agent, agent))]
        changes = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE status='changes' AND owner=?", (agent,))]
        escalated = [dict(r) for r in self.db.query(
            "SELECT t.*, (SELECT body FROM messages m WHERE m.thread_id=t.id AND m.kind='escalation' ORDER BY m.id DESC LIMIT 1) "
            "AS question, (SELECT author FROM messages m WHERE m.thread_id=t.id AND m.kind='escalation' ORDER BY m.id DESC "
            "LIMIT 1) AS escalated_by FROM threads t WHERE t.status='escalated' ORDER BY t.updated")] if agent == PERSON else []
        pending = []
        for r in self.db.query(
                "SELECT m.*, t.title AS thread_title, t.kind AS thread_kind, t.task_id AS task_id FROM messages m "
                "JOIN threads t ON t.id=m.thread_id LEFT JOIN acks k ON k.agent=? AND k.thread_id=m.thread_id "
                "WHERE m.author<>? AND m.mentions LIKE ? AND m.id>COALESCE(k.upto, 0) ORDER BY m.id LIMIT 200",
                (agent, agent, f'%"{agent}"%')):
            d = dict(r)
            d["mentions"] = _l(d.get("mentions"))
            if agent in d["mentions"]:
                pending.append(d)
        counts = {"messages": len(msgs), "for_you": sum(1 for m in msgs if m["for_you"]), "reviews": len(reviews),
                  "changes": len(changes), "escalated": len(escalated), "pending": len(pending)}
        counts["total"] = counts["messages"] + counts["reviews"] + counts["changes"] + counts["escalated"]
        progress_alerts = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE created_by=? AND status IN ('claimed','in_progress','changes')", (agent,))]
        progress_alerts = [t for t in progress_alerts if t["activity"]["state"] == "stalled"]
        counts["progress_alerts"] = len(progress_alerts)
        pools = self._quota_pools()
        out_of_quota = []
        for r in self.db.query("SELECT id FROM agents ORDER BY id"):
            state = self._quota_state_from(r["id"], pools)
            if state["state"] == "exhausted":
                out_of_quota.append({"agent": r["id"], "reset_at": state["reset_at"], "note": state["note"]})
        return {"ok": True, "agent": agent, "since_id": since, "counts": counts, "messages": msgs,
                "reviews": reviews, "changes": changes, "escalated": escalated, "pending": pending,
                "progress_alerts": progress_alerts, "out_of_quota": out_of_quota}

    # -- board ---------------------------------------------------------------------------------------------------

    def board(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self.db.tx():
            live = self._live_locks()
        counts = {s: 0 for s in TASK_STATUSES}
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"):
            counts[r["status"]] = r["n"]
        active = self.tasks({"limit": 300})["tasks"]
        tasks = [t for t in active if t["status"] not in FINAL] + \
            [self._task_dict(r) for r in self.db.query(
                "SELECT * FROM tasks WHERE status IN ('done','dropped') ORDER BY closed_at DESC LIMIT 15")]
        return {"ok": True, "agents": self.agents()["agents"], "counts": counts, "tasks": tasks, "locks": live,
                "orphans": self.orphans()["findings"],
                "lock_groups": group_locks(live),
                "threads": self.threads({"limit": 40})["threads"],
                "escalated": self.db.scalar("SELECT COUNT(*) FROM threads WHERE status='escalated'", default=0),
                "decisions": self.decisions({"limit": 8})["decisions"]}


    def digest(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """What happened in the last ``hours`` (default 12): per agent, tasks opened, claimed, sent to review and
        finished (with how they were reviewed), reviews given, decisions taken and escalations, plus what waits now."""
        a = a or {}
        try:
            hours = max(0.25, min(float(a.get("hours") or 12), 24 * 14))
        except (TypeError, ValueError):
            raise AgoraError("hours must be a number") from None
        since = self.clock() - hours * 3600
        per: dict[str, dict[str, int]] = {}

        def bump(agent: Optional[str], key: str) -> None:
            if agent:
                per.setdefault(agent, {"opened": 0, "claimed": 0, "submitted": 0, "done": 0, "reviews": 0, "messages": 0})
                per[agent][key] += 1

        for r in self.db.query("SELECT created_by FROM tasks WHERE created>=?", (since,)):
            bump(r["created_by"], "opened")
        for r in self.db.query("SELECT owner FROM tasks WHERE claimed_at>=?", (since,)):
            bump(r["owner"], "claimed")
        for r in self.db.query("SELECT owner FROM tasks WHERE submitted_at>=?", (since,)):
            bump(r["owner"], "submitted")
        for r in self.db.query("SELECT author, kind FROM messages WHERE created>=? AND author NOT IN ('system')", (since,)):
            bump(r["author"], "reviews" if r["kind"] in ("approve", "changes") else "messages")
        done = [self._task_dict(r) for r in self.db.query(
            "SELECT * FROM tasks WHERE status='done' AND closed_at>=? ORDER BY closed_at", (since,))]
        for t in done:
            bump(t.get("owner"), "done")
        decisions = [dict(r) for r in self.db.query(
            "SELECT id, title, resolution, resolved_by, updated FROM threads WHERE status='resolved' AND kind<>'task' "
            "AND updated>=? ORDER BY updated", (since,))]
        waiting = {
            "reviews": [self._task_dict(r) for r in self.db.query("SELECT * FROM tasks WHERE status='review' ORDER BY submitted_at")],
            "changes": [self._task_dict(r) for r in self.db.query("SELECT * FROM tasks WHERE status='changes'")],
            "escalated": [dict(r) for r in self.db.query("SELECT id, title, updated FROM threads WHERE status='escalated'")],
            "progress": [t for t in self.tasks({"status": "active"})["tasks"] if t["activity"]["state"] == "stalled"],
        }
        by_state = {k: sum(1 for t in done if (t.get("review_state") or ("approved" if t["reviewed"] else "unreviewed")) == k)
                    for k in ("approved", "equivalent", "exempt", "unreviewed")}
        return {"ok": True, "hours": hours, "since": since, "agents": per, "done": done, "done_by_review": by_state,
                "decisions": decisions, "waiting": waiting}


# ---- facet ---------------------------------------------------------------------------------------------------

_A = {"type": "string", "description": "Your agent id, the same in every call: builder, reviewer, agent-2… (never the person's)"}
_TASK = {"type": "integer"}
_THREAD = {"type": "integer"}
_STRS = {"type": "array", "items": {"type": "string"}}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_CHECKPOINT_SCHEMA = _schema({
    **{key: {"type": "string", "maxLength": limit, **({"minLength": 1} if key == "summary" else {}),
              **({"pattern": "^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"} if key in ("base_head", "head") else {})}
       for key, limit in CHECKPOINT_FIELDS.items()},
    "next_steps": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 2000}},
    "artifacts": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 2000}},
    "tests": {"type": "array", "maxItems": 50, "items": _schema({
        "name": {"type": "string", "minLength": 1, "maxLength": 300},
        "status": {"type": "string", "enum": ["passed", "failed", "not_run"]},
        "evidence": {"type": "string", "maxLength": 4000}}, ["name", "status", "evidence"])}}, ["summary"])


TOOLS: list[dict[str, Any]] = [
    {"name": "hub_agora_activity", "read": True,
     "description": "Task progress evidence: task notes/checkpoints versus heartbeat and agent observation. Read-only inference; never changes status or wakes agents.",
     "inputSchema": _schema({"task_id": _TASK, "idle_after_s": {"type": "integer", "minimum": 60, "maximum": 604800}}, ["task_id"])},
    {"name": "hub_agora_timeline", "read": True,
     "description": "Durable task chronology including associated thread messages, state/lock/review records and versioned checkpoints. Oldest first; pass next_after_id as after_id.",
     "inputSchema": _schema({"task_id": _TASK, "after_id": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["task_id"])},
    {"name": "hub_agora_checkpoint",
     "description": "Append declared task progress for its current owner. expected_revision is required (0 initially); "
                    "stale writes conflict, exact immediate retries are idempotent. No paths or tests are verified, "
                    "no locks renewed or task/review state changed. Payload <=65536 UTF-8 JSON bytes.",
     "inputSchema": _schema({"agent": _A, "task_id": {"type": "integer", "minimum": 1},
                             "expected_revision": {"type": "integer", "minimum": 0, "maximum": 9_223_372_036_854_775_806},
                             "payload": _CHECKPOINT_SCHEMA}, ["agent", "task_id", "expected_revision", "payload"])},
    {"name": "hub_agora_checkpoints", "read": True,
     "description": "Read append-only task checkpoints after a revision, oldest first, with current lock warnings. "
                    "Persist next_after_revision for pagination. Evidence is declared by the author.",
     "inputSchema": _schema({"task_id": {"type": "integer", "minimum": 1},
                             "after_revision": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["task_id"])},
    {"name": "hub_agora_board", "read": True,
     "description": "Ágora (shared workspace of the coding agents and the person): agents and what they do, tasks by status, "
                    "active locks, open/escalated threads and recent decisions. Read this when you start a session.",
     "inputSchema": _schema({}, [])},
    {"name": "hub_agora_usage", "read": True,
     "description": "Read the latest sanitized quota observations for the four provider leaders. Pools shared by multiple agents are returned once, with reporter identities; expired observations are marked stale and their percentages become unknown.",
     "inputSchema": _schema({}, [])},
    {"name": "hub_agora_agent_quota", "read": True,
     "description": "Quota state of one agent from its provider's latest report: ok, low (<15 % left), exhausted (with "
                    "reset_at and a note such as 'cursor-agora sin cuota hasta 15-10 11:40'), unknown, stale or none. "
                    "Mentions, handovers and reviewer choices already warn about exhausted agents (quota_warnings).",
     "inputSchema": _schema({"agent": {"type": "string", "description": "agent id, e.g. cursor-agora"}}, ["agent"])},
    {"name": "hub_agora_orphans", "read": True,
     "description": "Work nobody is moving: tasks in review with no reviewer for 30 min or a silent / out-of-quota reviewer, "
                    "and claimed tasks whose owner is silent or out of quota. Each finding names a free agent and the "
                    "handover command that moves it. The board carries the same list.",
     "inputSchema": _schema({}, [])},
    {"name": "hub_agora_orphan_sweep",
     "description": "Announce new orphans now (heartbeats already do it every five minutes): a note in each task thread "
                    "and one summary in the coordination thread mentioning the suggested agents. Each finding once.",
     "inputSchema": _schema({"agent": _A, "force": {"type": "boolean", "description": "skip the five-minute spacing"}}, [])},
    {"name": "hub_agora_usage_report",
     "description": "Report a sanitized provider account-pool snapshot. pool_id must be an opaque local label, never an account id, email or credential. Unknown windows use null percentages; reported_at is assigned by the Hub.",
     "inputSchema": _schema({"agent": _A, "provider_id": {"type": "string", "enum": ["claude", "codex", "cursor", "faustus"]},
                             "pool_id": {"type": "string", "minLength": 1, "maxLength": 80},
                             "pool_label": {"type": "string", "minLength": 1, "maxLength": 80},
                             "source": {"type": "string", "minLength": 1, "maxLength": 120},
                             "windows": {"type": "array", "minItems": 1, "maxItems": 20, "items": _schema({
                                 "window_id": {"type": "string", "minLength": 1, "maxLength": 64},
                                 "label": {"type": "string", "maxLength": 100},
                                 "measurement": {"type": "string", "enum": ["measured", "declared", "unknown"]},
                                 "used_percent": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                                 "remaining_percent": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                                 "reset_at": {"type": ["string", "null"]},
                                 "source": {"type": "string", "maxLength": 120}}, ["window_id", "measurement"])}},
                            ["agent", "provider_id", "pool_id", "pool_label", "source", "windows"])},
    {"name": "hub_agora_inbox", "read": True,
     "description": "Your Ágora inbox: messages from the others since your last read (for_you when it concerns you), "
                    "reviews waiting for you and changes asked of you. wait_s (<=60 here) waits for something new; "
                    "peek keeps the read mark. metadata_only returns who/where/what kind without bodies and keeps "
                    "the read mark: read the bodies with hub_agora_thread or hub_agora_task (with agent).",
     "inputSchema": _schema({"agent": _A, "wait_s": {"type": "number"}, "peek": {"type": "boolean"},
                             "metadata_only": {"type": "boolean"}, "mine_only": {"type": "boolean"}, "since_id": {"type": "integer"}}, ["agent"])},
    {"name": "hub_agora_heartbeat",
     "description": "Tell the Ágora you are here and what you are doing (state working|idle|waiting|away). "
                    "Renews every owned lock by default. Optional renew_resources selects exact live owned locks; [] reports state without renewal.",
      "inputSchema": _schema({"agent": _A, "doing": {"type": "string"}, "state": {"type": "string"},
                              "name": {"type": "string"}, "note": {"type": "string"},
                              "renew_resources": {"type": "array", "items": {"type": "string", "minLength": 1}}}, ["agent"])},
    {"name": "hub_agora_sync",
     "description": "Resume the Ágora session / retomar Ágora: heartbeat, inbox peek, board and posts after since_id.\n"
                    "Does not acknowledge. Store next_since_id and pass it back; thread_ids scopes the cursor.",
     "inputSchema": _schema({"agent": _A, "doing": {"type": "string"}, "state": {"type": "string"},
                             "name": {"type": "string"}, "note": {"type": "string"},
                             "renew_resources": {"type": "array", "items": {"type": "string", "minLength": 1}},
                             "since_id": {"type": "integer", "minimum": 0},
                             "thread_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                            "maxItems": 100},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["agent"])},
    {"name": "hub_agora_task_add",
     "description": "Propose a task (kind feature|bug|research|review|chore|eval|docs, priority 0 urgent..3 someday, "
                    "repo and paths it touches). blocked_by lists task ids it waits for. claim=true also claims it.",
     "inputSchema": _schema({"agent": _A, "title": {"type": "string"}, "body": {"type": "string"},
                             "repo": {"type": "string"}, "paths": _STRS, "kind": {"type": "string"},
                             "priority": {"type": "integer"}, "mentions": _STRS, "claim": {"type": "boolean"},
                             "blocked_by": {"type": "array", "items": {"type": "integer"}},
                             "locks": _STRS, "ttl_s": {"type": "integer"}}, ["agent", "title"])},
    {"name": "hub_agora_task_deps",
     "description": "Set what a task waits for: add / remove blocker task ids, or set the whole list ([] clears). "
                    "Loops are refused. When every blocker is approved, done or dropped the owner is woken once "
                    "(a blocked task goes back to claimed; an unowned one is announced as ready to claim).",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "add": {"type": "array", "items": {"type": "integer"}},
                             "remove": {"type": "array", "items": {"type": "integer"}},
                             "set": {"type": "array", "items": {"type": "integer"}}}, ["agent", "task_id"])},
    {"name": "hub_agora_task_claim",
     "description": "Claim a task and take its locks all-or-nothing (path:Repo/file-or-dir, repo:Repo, merge:Repo, "
                    "model:principal, gpu:N, port:N, app:id). Default locks: path:<repo>/<each path>. On conflict "
                    "you get who holds what. force takes a task whose owner has been silent for 6 h.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "locks": _STRS, "ttl_s": {"type": "integer"},
                             "force": {"type": "boolean"}}, ["agent", "task_id"])},
    {"name": "hub_agora_task_update",
     "description": "Progress on your task: note, status in_progress|blocked, branch, more paths and locks.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "note": {"type": "string"}, "status": {"type": "string"},
                             "branch": {"type": "string"}, "paths": _STRS, "locks": _STRS, "ttl_s": {"type": "integer"}},
                            ["agent", "task_id"])},
    {"name": "hub_agora_task_submit",
     "description": "Ask for review of your task before integrating it: summary of what changed, how it was "
                    "verified, branch/commits; optionally name the reviewer. Optional blocks: structured review "
                    "(what_why, paths, code_peek with path#Lx-Ly@commit verified against the task repo or an "
                    "identity-checked absolute checkout for isolated clones, flow, sequence). Block ids must be unique.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "summary": {"type": "string"}, "branch": {"type": "string"},
                             "commits": _STRS, "reviewer": {"type": "string"},
                             "blocks": {"type": "array", "items": {"type": "object"}, "maxItems": 40}},
                            ["agent", "task_id", "summary"])},
    {"name": "hub_agora_task_review",
     "description": "Review someone else's observed submission: read its submission_revision, inspect and test that "
                    "revision, then send expected_submission_revision with verdict approve|changes and the reasons. "
                    "Optional block_comments: [{block_id, body}] for per-block notes on structured submissions.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "expected_submission_revision": {"type": "integer", "minimum": 1},
                             "verdict": {"type": "string"}, "body": {"type": "string"},
                             "block_comments": {"type": "array", "items": {"type": "object"}, "maxItems": 40}},
                            ["agent", "task_id", "expected_submission_revision", "verdict", "body"])},
    {"name": "hub_agora_task_done",
     "description": "Your task is integrated: commits and result. Releases its locks. Refused while a review is "
                    "younger than 2 h or changes were requested, unless force with a reason. docs, eval, research and "
                    "chore tasks may close without review (exempt, with the reason); code may not.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "result": {"type": "string"}, "commits": _STRS,
                             "force": {"type": "boolean"}, "reason": {"type": "string"}, "exempt": {"type": "string"}},
                            ["agent", "task_id"])},
    {"name": "hub_agora_task_release",
     "description": "Give your task back (open again) or drop it (drop=true), with the reason. Releases its locks.",
     "inputSchema": _schema({"agent": _A, "task_id": _TASK, "reason": {"type": "string"}, "drop": {"type": "boolean"}},
                            ["agent", "task_id"])},
    {"name": "hub_agora_handover",
     "description": "Move the unfinished work of an agent that died (its context ran out, its chat closed) to its successor: "
                    "tasks it owns that are not done (status, review state, submission revision and history stay), their live "
                    "locks (renewed) and, with include_reviews (default), the reviewer role of tasks in review/changes. "
                    "tasks limits it to those ids; reason is required. Allowed for from_agent itself, and for any registered "
                    "agent once from_agent sent no heartbeat for 30 min, or right away with force=true (Luis's order: "
                    "an agent out of quota that keeps sending heartbeats, work that must move now); to_agent must have a "
                    "recent heartbeat. Announced in the tasks' threads and thread 32. Running it twice changes nothing.",
     "inputSchema": _schema({"agent": _A, "from_agent": {"type": "string", "description": "the agent that stopped"},
                             "to_agent": {"type": "string", "description": "its successor, with a recent heartbeat"},
                             "reason": {"type": "string", "minLength": 1},
                             "tasks": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 200},
                             "include_reviews": {"type": "boolean"},
                             "force": {"type": "boolean", "description": "take it now even if from_agent still sends heartbeats"}},
                            ["agent", "from_agent", "to_agent", "reason"])},
    {"name": "hub_agora_tasks", "read": True,
     "description": "List tasks: status (or 'active'), owner, repo, kind.",
     "inputSchema": _schema({"status": {"type": "string"}, "owner": {"type": "string"}, "repo": {"type": "string"},
                             "kind": {"type": "string"}, "limit": {"type": "integer"}}, [])},
    {"name": "hub_agora_task", "read": True,
     "description": "One task with its whole conversation and its locks.",
     "inputSchema": _schema({"task_id": _TASK, "agent": {"type": "string", "description": "who reads; marks the task thread as read"}}, ["task_id"])},
    {"name": "hub_agora_lock",
     "description": "Take or renew locks outside a task: model:principal before a live evaluation, merge:Repo before "
                    "integrating into the shared checkout. Conflicts say who holds what.",
     "inputSchema": _schema({"agent": _A, "resources": _STRS, "task_id": _TASK, "ttl_s": {"type": "integer"},
                             "note": {"type": "string"}}, ["agent", "resources"])},
    {"name": "hub_agora_unlock",
     "description": "Release locks you hold.",
     "inputSchema": _schema({"agent": _A, "resources": _STRS}, ["agent", "resources"])},
    {"name": "hub_agora_locks", "read": True,
     "description": "Active locks; check=<resource> tells whether that resource is free and who holds it otherwise.",
     "inputSchema": _schema({"check": {"type": "string"}}, [])},
    {"name": "hub_agora_thread_open",
     "description": "Open a thread: debate (a proposal to argue), question, decision, review, handoff or note. "
                    "mentions names who should answer; escalate=true sends it straight to the person.",
     "inputSchema": _schema({"agent": _A, "title": {"type": "string"}, "body": {"type": "string"},
                             "kind": {"type": "string"}, "task_id": _TASK, "mentions": _STRS,
                             "escalate": {"type": "boolean"}}, ["agent", "title", "body"])},
    {"name": "hub_agora_post",
     "description": "Reply in a thread: kind comment|proposal|agree|disagree (state your position with arguments).",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "body": {"type": "string"}, "kind": {"type": "string"},
                             "mentions": _STRS}, ["agent", "thread_id", "body"])},
    {"name": "hub_agora_resolve",
     "description": "Close a thread with the agreed resolution in writing (it enters the decision log).",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "resolution": {"type": "string"}},
                            ["agent", "thread_id", "resolution"])},
    {"name": "hub_agora_escalate",
     "description": "No agreement after two rounds, or it is the person's call: the thread waits for them and they are notified. "
                    "question = the concrete choice they have to make, with the options.",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "question": {"type": "string"}},
                            ["agent", "thread_id", "question"])},
    {"name": "hub_agora_thread", "read": True,
     "description": "One thread with its messages, each participant's stance and its task.",
     "inputSchema": _schema({"thread_id": _THREAD, "agent": {"type": "string", "description":
                                                               "your id: marks the thread as seen for you"}},
                            ["thread_id"])},
    {"name": "hub_agora_thread_recent", "read": True,
     "description": "Read the latest bounded page of a thread, in chronological order, without marking it seen. "
                    "limit defaults to 20 (maximum 200); before_id reads strictly older messages. "
                    "Follow pagination.next_before_id while has_more to read older history. "
                    "Bodies are capped at 20000 characters each and 200000 per page; body_chars, "
                    "body_digest and body_truncated describe the original body. A truncated body is not a complete read. "
                    "A recent page never acknowledges unseen messages; stances describe only this page.",
     "inputSchema": _schema({"thread_id": {"type": "integer", "minimum": 1},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                             "before_id": {"type": "integer", "minimum": 1}}, ["thread_id"])},
    {"name": "hub_agora_ack",
     "description": "Mark a thread as seen (or all=true): its mentions of you stop being pending in your inbox. Opening "
                    "a thread with hub_agora_thread and your agent id, or replying in it, does the same.",
     "inputSchema": _schema({"agent": _A, "thread_id": _THREAD, "all": {"type": "boolean"}}, ["agent"])},
    {"name": "hub_agora_digest", "read": True,
     "description": "What happened in the last hours (default 12): per agent tasks opened, claimed, sent to review and "
                    "finished (approved, exempt or unreviewed), reviews given, decisions, and what waits now.",
     "inputSchema": _schema({"hours": {"type": "number"}}, [])},
    {"name": "hub_agora_decisions", "read": True,
     "description": "The decision log: resolved threads with their resolution, newest first.",
     "inputSchema": _schema({"limit": {"type": "integer"}}, [])},
    {"name": "hub_agora_threads", "read": True,
     "description": "List threads (escalated first, then open, newest first), filtered by status or kind; task threads "
                    "only with include_tasks. With agent, each thread says how many messages by others that agent "
                    "has not seen (unread) and up to which message id it has (seen_upto). Listing marks nothing read.",
     "inputSchema": _schema({"agent": _A, "status": {"type": "string"}, "kind": {"type": "string"},
                             "include_tasks": {"type": "boolean"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, [])},
]

_WRITE_OPS = ("checkpoint", "heartbeat", "sync", "task_add", "task_claim", "task_update", "task_submit", "task_review", "task_done",
              "task_release", "handover", "lock", "unlock", "thread_open", "post", "resolve", "escalate", "reopen", "read", "ack",
              "usage_report", "orphan_sweep", "task_deps")


def _observed(rec: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The few fields of an agent's best observed session that the board carries."""
    if not rec:
        return None
    return {k: rec.get(k) for k in ("state", "since", "tool", "title", "engine", "session_key", "last_activity",
                                    "binding", "questions")}


class AgoraFacet(Facet):
    id = "agora"
    # agora_blocks.js paints submission_blocks / per-block comments without editing agora.js (#116 lock).
    ui_scripts = ("agora.js", "agora_blocks.js")

    def __init__(self, hub: Any):
        super().__init__(hub)
        events = getattr(hub, "events", None)

        def repo_lookup(name: str) -> Optional[Path]:
            repos = getattr(hub, "repos", None)
            if repos is None:
                return None
            try:
                detail = repos.detail(name)
            except Exception:
                return None
            if not detail.get("ok"):
                return None
            rec = detail.get("repo") or {}
            path = rec.get("path")
            return Path(path) if path else None

        def repo_roots(name: str) -> list[Path]:
            primary = repo_lookup(name)
            if primary is None:
                return []
            roots = [Path(primary).resolve()]
            roots.extend(_git_worktrees(Path(primary)))
            # Deduplicate
            seen: set[str] = set()
            out: list[Path] = []
            for r in roots:
                k = str(r)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
            return out

        watch_cfg = getattr(hub.config, "agent_watch", None)
        task_idle_s = watch_cfg.get("task_idle_s", 3600) if isinstance(watch_cfg, dict) else 3600
        agora_cfg = getattr(hub.config, "agora", None)
        freshness = agora_cfg.get("freshness") if isinstance(agora_cfg, dict) else None
        self.agora = Agora(Path(hub.config.data_dir) / "agora.db", task_idle_s=task_idle_s, freshness=freshness,
                           emit=(lambda t, d: events.emit(t, d, source="hub")) if events is not None else None,
                           escalate_hook=self._notify_person, repo_lookup=repo_lookup, repo_roots=repo_roots)

        self._watch: Optional[agent_watch.AgentWatch] = None
        self._watch_lock = threading.Lock()

    def close(self) -> None:
        self.agora.close()

    @property
    def watch(self) -> agent_watch.AgentWatch:
        """The transcript observer (built on first use: it needs no thread, reads refresh it at most every 5 s)."""
        with self._watch_lock:
            if self._watch is None:
                cfg = getattr(self.hub.config, "agent_watch", None)
                cfg = cfg if isinstance(cfg, dict) else {}
                kw: dict[str, Any] = {}
                for key, cast in (("hours", float), ("max_files", int)):
                    try:
                        if cfg.get(key) is not None:
                            kw[key] = cast(cfg[key])
                    except (TypeError, ValueError):
                        pass
                self._watch = agent_watch.AgentWatch(
                    roots=cfg.get("roots") if isinstance(cfg.get("roots"), dict) else None,
                    store_path=Path(self.hub.config.data_dir) / "agent_watch.json", **kw)
            return self._watch

    def _watch_enabled(self) -> bool:
        cfg = getattr(self.hub.config, "agent_watch", None)
        return not (isinstance(cfg, dict) and cfg.get("enabled") is False)

    def _watch_view(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        a = a or {}
        if not self._watch_enabled():
            return {"ok": False, "status": 404, "error": "the agent observer is disabled (hub.json agent_watch.enabled)"}
        agent = str(a.get("agent") or "").strip().lower() or None
        try:
            limit = int(a.get("limit") or agent_watch.MAX_SESSIONS_OUT)
        except (TypeError, ValueError):
            limit = agent_watch.MAX_SESSIONS_OUT
        force = str(a.get("refresh", "")).lower() in ("1", "true", "yes")
        return {"ok": True, **self.watch.view(force=force, agent=agent, limit=max(1, min(limit, agent_watch.MAX_SESSIONS_OUT)))}

    def _watch_bind(self, a: dict[str, Any]) -> dict[str, Any]:
        if not self._watch_enabled():
            return {"ok": False, "status": 404, "error": "the agent observer is disabled (hub.json agent_watch.enabled)"}
        try:
            return self.watch.bind(a.get("session_key"), a.get("agent"))
        except agent_watch.WatchError as exc:
            return {"ok": False, "status": exc.status, "error": str(exc)}

    def _board(self, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """The Ágora board plus, per agent, what its transcripts say it is doing (``observed``)."""
        out = self._run(self.agora.board, a or {})
        if not out.get("ok") or not self._watch_enabled():
            return out
        try:
            view = self.watch.view()
        except Exception as exc:  # noqa: BLE001 - the board must not depend on the filesystem being readable
            out["watch_error"] = str(exc)[:200]
            return out
        for ag in out.get("agents", []):
            ag["observed"] = _observed(view["agents"].get(ag.get("id")))
        self._apply_observations(out.get("tasks", []), view)
        known = {x.get("id") for x in out.get("agents", [])}
        out["observed_only"] = {k: _observed(v) for k, v in view["agents"].items() if k not in known}
        out["watch"] = {"questions": len(view["questions"]), "unbound": len(view["unbound"]),
                        "sessions": len(view["sessions"])}
        return out

    def _apply_observations(self, tasks: list[dict[str, Any]], view: dict[str, Any], *, volatile: bool = False) -> None:
        for task in tasks:
            activity = task.get("activity") or {}
            task["activity"] = self.agora._task_activity(
                task, idle_s=activity.get("idle_after_s"), observed=view.get("agents", {}).get(task.get("owner")),
                volatile=volatile)

    def _enrich(self, out: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if out.get("ok") and self._watch_enabled():
            try:
                self._apply_observations(tasks, self.watch.view())
            except Exception as exc:
                out["watch_error"] = str(exc)[:200]
        return out

    def _task(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.task(a)
        return self._enrich(out, [out["task"]])

    def _tasks(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.tasks(a)
        return self._enrich(out, out["tasks"])

    def _sync(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.sync(a)
        inbox = out["inbox"]
        self._enrich(out, out["leased_tasks"] + out["board"].get("tasks", []) + inbox["progress_alerts"])
        inbox["progress_alerts"] = [t for t in inbox["progress_alerts"] if t["activity"]["state"] == "stalled"]
        inbox["counts"]["progress_alerts"] = len(inbox["progress_alerts"])
        return out

    def _thread(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.thread(a)
        return self._enrich(out, [out["task"]] if out.get("task") else [])

    def _activity(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.activity(a)
        task = self.agora._task_row(out["task_id"])
        task["activity"] = out["activity"]
        if self._watch_enabled():
            try:
                self._apply_observations([task], self.watch.view(), volatile=True)
            except Exception as exc:
                out["watch_error"] = str(exc)[:200]
        out["activity"] = task["activity"]
        return out

    def _inbox(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.inbox(a)
        self._enrich(out, out["progress_alerts"])
        out["progress_alerts"] = [t for t in out["progress_alerts"] if (t.get("activity") or {}).get("state") == "stalled"]
        out["counts"]["progress_alerts"] = len(out["progress_alerts"])
        # the enrichment above may add text again: project last
        return project_inbox_metadata(out) if a.get("metadata_only") else out

    def _digest(self, a: dict[str, Any]) -> dict[str, Any]:
        out = self.agora.digest(a)
        self._enrich(out, out["waiting"]["progress"])
        out["waiting"]["progress"] = [t for t in out["waiting"]["progress"] if t["activity"]["state"] == "stalled"]
        return out

    def _notify_person(self, thread: dict[str, Any], agent: str, question: str) -> None:
        notify = self.hub.facet("notify") if hasattr(self.hub, "facet") else None
        if notify is None:
            return
        url = str(getattr(self.hub.config, "url", "") or "").rstrip("/")
        notify.send(f"Ágora: {agent} necesita tu decisión", f"{thread['title']}\n\n{question}"[:1900],
                    app="hub", priority="high", url=f"{url}/#agora-{thread['id']}" if url else "",
                    dedupe_key=f"agora-escalation-{thread['id']}-{int(time.time() // 60)}", tags=["agora"])

    def _run(self, fn: Callable[[dict[str, Any]], dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn(args)
        except AgoraError as exc:
            return {"ok": False, "status": exc.status, "error": str(exc), **exc.extra}

    def get(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/agora"):
            return None
        q = {k: v[0] for k, v in req.query.items() if v}
        ag = self.agora
        if p in ("/api/agora", "/api/agora/board"):
            return self._board(q)
        if p == "/api/agora/watch":
            return self._watch_view(q)
        if p == "/api/agora/inbox":
            if q.get("agent", "").lower() == PERSON and req.caller() != "ui":
                q["peek"] = "1"
            q["peek"] = str(q.get("peek", "")).lower() in ("1", "true", "yes")
            q["mine_only"] = str(q.get("mine_only", "")).lower() in ("1", "true", "yes")
            q["metadata_only"] = str(q.get("metadata_only", "")).lower() in ("1", "true", "yes")
            return self._run(self._inbox, q)
        if p == "/api/agora/tasks":
            return self._run(self._tasks, q)
        if p in ("/api/agora/activity", "/api/agora/timeline"):
            for key in ("task_id", "after_id", "idle_after_s", "limit"):
                if key in q and re.fullmatch(r"[0-9]+", q[key]):
                    try:
                        q[key] = int(q[key])
                    except ValueError:
                        pass
            return self._run(self._activity if p.endswith("/activity") else ag.timeline, q)
        if p == "/api/agora/checkpoints":
            # HTTP query parameters are text; the store/MCP contract remains strictly typed.
            for key in ("task_id", "after_revision", "limit"):
                if key in q and re.fullmatch(r"[0-9]+", q[key]):
                    try:
                        q[key] = int(q[key])
                    except ValueError:
                        pass
            return self._run(ag.checkpoints, q)
        if p.startswith("/api/agora/tasks/"):
            who = q.get("agent", "").lower()
            person = who == PERSON and req.caller() == "ui"
            if who == PERSON and not person:
                who = ""   # the person's read is recorded only from the hub's page
            return self._run(self._task, {"task_id": p.rsplit("/", 1)[1], **({"agent": who, "_person": person} if who else {})})
        if p == "/api/agora/threads":
            q["include_tasks"] = str(q.get("include_tasks", "")).lower() in ("1", "true")
            return self._run(ag.threads, q)
        recent = re.fullmatch(r"/api/agora/threads/([0-9]+)/recent", p)
        if recent:
            q["thread_id"] = int(recent.group(1))
            for key in ("limit", "before_id"):
                if key in q and re.fullmatch(r"[0-9]+", q[key]):
                    try:
                        q[key] = int(q[key])
                    except ValueError:
                        pass
            return self._run(ag.thread_recent, q)
        if p.startswith("/api/agora/threads/"):
            who = q.get("agent", "").lower()
            person = who == PERSON and req.caller() == "ui"
            if who == PERSON and not person:
                who = ""
            return self._run(self._thread, {"thread_id": p.rsplit("/", 1)[1], "agent": who, "_person": person})
        if p == "/api/agora/locks":
            return self._run(ag.locks, q)
        if p == "/api/agora/decisions":
            return self._run(ag.decisions, q)
        if p == "/api/agora/digest":
            return self._run(self._digest, q)
        if p == "/api/agora/agents":
            return self._run(ag.agents, q)
        if p in ("/api/agora/usage", "/api/agora/console/usage"):
            return self._run(ag.usage, q)
        if p == "/api/agora/quota":
            return self._run(ag.agent_quota_state, q)
        if p == "/api/agora/orphans":
            return self._run(ag.orphans, q)
        return {"ok": False, "status": 404, "error": f"unknown Ágora route {p}"}

    def post(self, req: Request) -> Optional[Any]:
        p = req.path
        if not p.startswith("/api/agora/"):
            return None
        op = p[len("/api/agora/"):].strip("/")
        if op == "usage/report":
            op = "usage_report"
        if op == "watch/bind":
            if not req.caller():
                return {"ok": False, "status": 401, "error": "send the hub's bearer token (data/mcp-token) or use the hub's page"}
            return self._watch_bind(dict(req.body or {}))
        if op not in _WRITE_OPS:
            return {"ok": False, "status": 404, "error": f"unknown Ágora operation {op}"}
        caller = req.caller()
        if not caller:
            return {"ok": False, "status": 401, "error": "send the hub's bearer token (data/mcp-token) or use the hub's page"}
        args = dict(req.body or {})
        args.pop("_person", None)
        if caller == "ui":
            args.setdefault("agent", PERSON)
            args["_person"] = str(args.get("agent", "")).lower() == PERSON
        if op == "checkpoint":
            args.pop("_person", None)
        if op == "read":
            args["peek"] = False
            return self._run(self._inbox, args)
        if op == "sync":
            return self._run(self._sync, args)
        return self._run(getattr(self.agora, op), args)

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        out = []
        for t in [*TOOLS, *agent_watch.TOOLS]:
            d = {k: v for k, v in t.items() if k != "read"}
            d["annotations"] = {"readOnlyHint": bool(t.get("read"))}
            out.append(d)
        return out

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        ag = self.agora

        def wrap(fn: Callable[[dict[str, Any]], dict[str, Any]], *, cap_wait: bool = False) -> Callable[[dict[str, Any]], Any]:
            def call(args: dict[str, Any]) -> Any:
                args = dict(args or {})
                args.pop("_person", None)
                if cap_wait and args.get("wait_s"):
                    try:
                        args["wait_s"] = min(float(args["wait_s"]), 60.0)
                    except (TypeError, ValueError):
                        args["wait_s"] = 0
                return self._run(fn, args)
            return call

        return {
            "hub_agora_checkpoint": wrap(ag.checkpoint), "hub_agora_checkpoints": wrap(ag.checkpoints),
            "hub_agora_usage": wrap(ag.usage), "hub_agora_usage_report": wrap(ag.usage_report),
            "hub_agora_agent_quota": wrap(ag.agent_quota_state),
            "hub_agora_orphans": wrap(ag.orphans), "hub_agora_orphan_sweep": wrap(ag.orphan_sweep),
            "hub_agora_activity": wrap(self._activity), "hub_agora_timeline": wrap(ag.timeline),
            "hub_agora_board": wrap(self._board), "hub_agora_inbox": wrap(self._inbox, cap_wait=True),
            "hub_agora_heartbeat": wrap(ag.heartbeat), "hub_agora_sync": wrap(self._sync), "hub_agora_task_add": wrap(ag.task_add),
            "hub_agora_task_claim": wrap(ag.task_claim), "hub_agora_task_update": wrap(ag.task_update),
            "hub_agora_task_deps": wrap(ag.task_deps),
            "hub_agora_task_submit": wrap(ag.task_submit), "hub_agora_task_review": wrap(ag.task_review),
            "hub_agora_task_done": wrap(ag.task_done), "hub_agora_task_release": wrap(ag.task_release),
            "hub_agora_handover": wrap(ag.handover),
            "hub_agora_tasks": wrap(self._tasks), "hub_agora_task": wrap(self._task), "hub_agora_lock": wrap(ag.lock),
            "hub_agora_unlock": wrap(ag.unlock), "hub_agora_locks": wrap(ag.locks),
            "hub_agora_thread_open": wrap(ag.thread_open), "hub_agora_post": wrap(ag.post),
            "hub_agora_resolve": wrap(ag.resolve), "hub_agora_escalate": wrap(ag.escalate),
            "hub_agora_thread": wrap(self._thread), "hub_agora_thread_recent": wrap(ag.thread_recent),
            "hub_agora_decisions": wrap(ag.decisions), "hub_agora_threads": wrap(ag.threads),
            "hub_agora_ack": wrap(ag.ack), "hub_agora_digest": wrap(self._digest),
            "hub_agora_watch": wrap(self._watch_view), "hub_agora_watch_bind": wrap(self._watch_bind),
        }
