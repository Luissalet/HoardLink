"""Ágora delivery review: the diff of a submission, comments on its lines and a guided reading of it.

A task in review lists the repository and the commits of its submission. This module reads those commits from the
registered checkouts (and their worktrees) and turns them into a diff the person and the agents can comment on:

* **diff** — every commit as files → hunks → lines. Each line carries its old and new number, so a comment is
  anchored by ``(commit, path, side, line)`` and checked against the real diff before it is stored;
* **comments** — a batch of line comments (plus an optional general note) is written as ONE message in the task's
  thread, mentioning the agent that delivered, so its inbox wakes with every comment and the code line each refers
  to. The comments are also stored by submission revision and listed again later (``outdated`` once the delivery is
  resubmitted);
* **guide** — the family's language model (Hoard Link, the Sparks) orders the diff into chapters, each with a short
  summary and the files it covers, to read next to the real diff. It is kept per submission revision.

``agora.py`` binds the operations to :class:`Agora` (``review_*``), the REST routes
(``GET /api/agora/review_diff|review_comments``, ``POST /api/agora/review_comment_send|review_guide``) and the MCP
tools ``hub_agora_review_*``; ``ui/agora_review.js`` paints it inside the task of the Hub's Ágora page.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

SIDES = ("new", "old")
MAX_COMMITS = 20
MAX_FILES = 300
MAX_LINES_PER_FILE = 4000
MAX_COMMENTS = 60
MAX_COMMENT_CHARS = 4000
MAX_GUIDE_CHARS = 56000
MAX_CHAPTERS = 12
GUIDE_TIMEOUT_S = 240.0

#: Appended to ``agora.MIGRATIONS`` in order (see the migrations block there).
REVIEW_MIGRATION = """
    CREATE TABLE agora_review_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL DEFAULT 'ecosystem',
        task_id INTEGER NOT NULL, submission_revision INTEGER NOT NULL, author TEXT NOT NULL,
        commit_sha TEXT NOT NULL, path TEXT NOT NULL, side TEXT NOT NULL DEFAULT 'new', line INTEGER NOT NULL,
        end_line INTEGER, code TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, message_id INTEGER, created REAL NOT NULL);
    CREATE INDEX agora_review_comments_task ON agora_review_comments(space_id, task_id, id);
    CREATE TABLE agora_review_guides (id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL DEFAULT 'ecosystem',
        task_id INTEGER NOT NULL, submission_revision INTEGER NOT NULL, commits_sig TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', chapters TEXT NOT NULL DEFAULT '[]',
        requested_by TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        UNIQUE(space_id, task_id, submission_revision));
"""

_HEX = re.compile(r"[0-9a-fA-F]{7,64}")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_FIELD = "\x1f"


def _ag():
    """The agora module, imported lazily (it imports this one)."""
    from . import agora
    return agora


def _err(message: str, status: int = 400, **extra: Any):
    return _ag().AgoraError(message, status, **extra)


# ---- reading the commits --------------------------------------------------------------------------------------

def _git(root: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    argv = ["git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", str(root), *args]
    kw: dict[str, Any] = {"capture_output": True, "timeout": timeout,
                          "env": {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LANG": "C.UTF-8"}}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **kw)


def _text(raw: bytes) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def is_commit_hash(ref: str) -> bool:
    return _HEX.fullmatch(ref) is not None


def parse_patch(text: str) -> list[dict[str, Any]]:
    """A unified diff (several files) as ``[{path, old_path, status, binary, additions, deletions, hunks}]``.

    A hunk is ``{header, old_start, new_start, lines: [{t: ctx|add|del, old, new, text}]}``: ``old`` is None on added
    lines and ``new`` is None on deleted ones. A file past ``MAX_LINES_PER_FILE`` keeps its first lines and says so
    in ``truncated``."""
    files: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    hunk: Optional[dict[str, Any]] = None
    old_n = new_n = 0
    shown = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"path": "", "old_path": None, "status": "modified", "binary": False, "additions": 0,
                   "deletions": 0, "hunks": [], "truncated": False}
            m = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
            if m:
                cur["path"] = m.group(2)
                if m.group(1) != m.group(2):
                    cur["old_path"] = m.group(1)
            files.append(cur)
            hunk = None
            shown = 0
            continue
        if cur is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                cur["status"] = "added"
            elif line.startswith("deleted file mode"):
                cur["status"] = "deleted"
            elif line.startswith("rename from "):
                cur["status"] = "renamed"
                cur["old_path"] = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur["path"] = line[len("rename to "):]
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("--- "):
                if line[4:] != "/dev/null" and line.startswith("--- a/") and cur["status"] == "deleted":
                    cur["path"] = line[6:]
            elif line.startswith("+++ "):
                if line.startswith("+++ b/"):
                    cur["path"] = line[6:]
        m = _HUNK.match(line)
        if m:
            old_n, new_n = int(m.group(1)), int(m.group(3))
            hunk = {"header": line, "section": m.group(5).strip(), "old_start": old_n, "new_start": new_n, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None or not line[:1] in (" ", "+", "-", "\\"):
            continue
        if line.startswith("\\"):
            continue              # "\ No newline at end of file"
        if shown >= MAX_LINES_PER_FILE:
            cur["truncated"] = True
            continue
        shown += 1
        body = line[1:]
        if line[0] == "+":
            hunk["lines"].append({"t": "add", "old": None, "new": new_n, "text": body})
            new_n += 1
            cur["additions"] += 1
        elif line[0] == "-":
            hunk["lines"].append({"t": "del", "old": old_n, "new": None, "text": body})
            old_n += 1
            cur["deletions"] += 1
        else:
            hunk["lines"].append({"t": "ctx", "old": old_n, "new": new_n, "text": body})
            old_n += 1
            new_n += 1
    return files


class _DiffCache:
    """Commits never change, so their parsed diffs are kept (a small LRU) instead of asking git on every click."""

    def __init__(self, size: int = 64) -> None:
        self.size = size
        self._items: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str]) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple[str, str], value: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


_CACHE = _DiffCache()


def _roots(ag: Any, repo: str) -> list[Path]:
    agora = _ag()
    primary = ag.repo_lookup(repo) if ag.repo_lookup else None
    try:
        return agora.resolve_code_peek_roots(repo, primary=primary, checkout=None, repo_roots=ag.repo_roots)
    except agora.AgoraError as exc:
        raise _err(f"the repository {repo!r} is not registered in this Hub: {exc}", 404) from exc


def commit_diff(roots: list[Path], commit: str) -> dict[str, Any]:
    """One commit read from the first checkout that has it: ``{sha, short, subject, author, date, files, stats}``
    or ``{sha, error}`` when no registered checkout knows it."""
    sha = str(commit or "").strip()
    if not is_commit_hash(sha):
        return {"sha": sha, "short": sha[:10], "error": "not a commit hash"}
    last = ""
    for root in roots:
        key = (str(root), sha.lower())
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        try:
            res = _git(root, "show", "--no-color", "--no-ext-diff", "-M", "--first-parent", "-m", "--unified=3",
                       f"--format=%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s", f"{sha}^{{commit}}", "--")
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            continue
        if res.returncode != 0:
            last = _text(res.stderr).strip().splitlines()[-1] if res.stderr else "git show failed"
            continue
        out = _text(res.stdout)
        head, _, rest = out.partition("\n")
        parts = head.split(_FIELD)
        files = parse_patch(rest)
        value = {"sha": parts[0] if parts and parts[0] else sha, "short": (parts[0] or sha)[:10],
                 "author": parts[1] if len(parts) > 1 else "", "date": parts[2] if len(parts) > 2 else "",
                 "subject": parts[3] if len(parts) > 3 else "", "files": files,
                 "stats": {"files": len(files), "additions": sum(f["additions"] for f in files),
                           "deletions": sum(f["deletions"] for f in files)}}
        _CACHE.put(key, value)
        return value
    return {"sha": sha, "short": sha[:10], "error": f"no registered checkout has this commit ({last or 'not found'})"}


def _task_diff(ag: Any, task: dict[str, Any]) -> list[dict[str, Any]]:
    from .integration_git import repository_commit_index

    commits = task["commits"][:MAX_COMMITS]
    if not commits:
        raise _err(f"task {task['id']} lists no commits: submit it with its commit hashes to review the diff", 409)
    names = task["repositories"]
    assigned = {ref: name for name, refs in task["commits_by_repo"].items() for ref in refs}
    primary_reads = {}
    # Older deliveries could name one repository but submit a flat list from several.
    if len(names) == 1 and not assigned:
        roots = _roots(ag, names[0])
        primary_reads = {ref: {**commit_diff(roots, ref), "repo": names[0]} for ref in commits}

    unassigned = [ref for ref in commits if ref not in assigned and is_commit_hash(ref)
                  and (not primary_reads or primary_reads[ref].get("error"))]
    matches: dict[str, list[str]] = {}
    roots_by_name: dict[str, list[Path]] = {}
    registry: dict[str, str] = {}
    if unassigned:
        if names and not primary_reads:
            roots_by_name = {name: _registered_roots(ag, name) for name in names}
            registry = {str(roots[0]): name for name, roots in roots_by_name.items() if roots}
        else:
            registry = {str(entry["path"]): entry["name"] for entry in
                        getattr(ag, "registered_repositories", lambda: [])()}
        # Keep the path that matched. Different registered checkouts can have the same folder name.
        matches = repository_commit_index(({"name": path, "path": path} for path in registry), unassigned)

    result = []
    for ref in commits:
        paths = matches.get(ref, [])
        if ref in primary_reads and not paths:
            result.append(primary_reads[ref])
            continue
        if ref in assigned:
            name = assigned[ref]
            if name not in roots_by_name:
                roots_by_name[name] = _registered_roots(ag, name)
            result.append({**commit_diff(roots_by_name[name], ref), "repo": name})
            continue
        if len(paths) == 1:
            result.append({**commit_diff([Path(paths[0])], ref), "repo": registry[paths[0]]})
            continue
        # A shared commit has the same diff everywhere. An abbreviated hash can instead match different commits.
        reads = [{**item, "repo": registry[path]} for path in paths
                 if not (item := commit_diff([Path(path)], ref)).get("error")]
        if reads and len({item["sha"] for item in reads}) == 1:
            result.append({**reads[0], "repository_candidates": list(dict.fromkeys(item["repo"] for item in reads))})
            continue
        error = ("hash matches different commits; submit the full hash and its repository" if reads else
                 "no registered checkout has this commit")
        result.append({"sha": ref, "short": ref[:10], "repo": None, "error": error,
                       "repository_candidates": list(dict.fromkeys(registry[path] for path in paths))})
    if not primary_reads and all(item.get("error") for item in result):
        hashes = ", ".join(item["short"] for item in result)
        raise _err(f"No commit can be resolved for task {task['id']} ({hashes}). "
                   "Make the full hashes available in a registered checkout. Set the task's repository with "
                   f"agora update {task['id']} --repo REPOSITORY, or submit with --repo-commits (commits_by_repo in MCP) "
                   "to identify the repositories per commit.", 409, error_code="delivery_repository_unresolved",
                   commits=result)
    return result


def _registered_roots(ag: Any, name: str) -> list[Path]:
    try:
        return _roots(ag, name)
    except _ag().AgoraError as exc:
        if exc.status != 404:
            raise
        return []


def _find_line(commit: dict[str, Any], path: str, side: str, number: int) -> Optional[dict[str, Any]]:
    for f in commit.get("files") or []:
        if path not in (f["path"], f.get("old_path")):
            continue
        for h in f["hunks"]:
            for ln in h["lines"]:
                if side == "new" and ln["new"] == number and ln["t"] in ("add", "ctx"):
                    return ln
                if side == "old" and ln["old"] == number and ln["t"] in ("del", "ctx"):
                    return ln
    return None


def _match_commit(commits: list[dict[str, Any]], wanted: Any) -> Optional[dict[str, Any]]:
    w = str(wanted or "").strip().lower()
    if not w:
        return commits[-1] if len(commits) == 1 else None
    hits = [c for c in commits if str(c.get("sha", "")).lower().startswith(w) or w.startswith(str(c.get("sha", "")).lower())]
    return hits[0] if len(hits) == 1 else None


# ---- operations -----------------------------------------------------------------------------------------------

def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _err(f"{name} must be a number") from None


def _comment_view(row: Any, current: int) -> dict[str, Any]:
    d = dict(row)
    d["commit"] = d.pop("commit_sha")
    d["outdated"] = int(d["submission_revision"]) != current
    return d


def _comments(ag: Any, task: dict[str, Any], *, revision: Any = None, commit: str = "", path: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM agora_review_comments WHERE space_id=? AND task_id=?"
    args: list[Any] = [ag.space_id, task["id"]]
    if revision not in (None, "", "all"):
        sql += " AND submission_revision=?"
        args.append(_int(revision, "revision"))
    if commit:
        sql += " AND commit_sha LIKE ?"
        args.append(commit.lower() + "%")
    if path:
        sql += " AND path=?"
        args.append(path)
    current = int(task.get("submission_revision") or 0)
    return [_comment_view(r, current) for r in ag.db.query(sql + " ORDER BY id", tuple(args))]


def review_diff(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The diff of a task's submission, commit by commit. ``commit`` and ``path`` narrow it; ``summary_only`` drops
    the lines and keeps the per-file counts (a long delivery is read file by file); ``max_lines`` caps the lines of
    each file in the answer. The comments already left on it come with it."""
    task = self._task_row(a.get("task_id"))
    commits = _task_diff(self, task)
    want_commit = str(a.get("commit") or "").strip()
    want_path = str(a.get("path") or "").strip().replace("\\", "/")
    summary_only = bool(a.get("summary_only"))
    cap = a.get("max_lines")
    cap = None if cap in (None, "") else max(1, _int(cap, "max_lines"))
    shown: list[dict[str, Any]] = []
    for c in commits:
        if want_commit and not (c.get("sha", "").lower().startswith(want_commit.lower())):
            continue
        item = {k: v for k, v in c.items() if k != "files"}
        files = []
        for f in (c.get("files") or [])[:MAX_FILES]:
            if want_path and want_path not in (f["path"], f.get("old_path")):
                continue
            g = {k: v for k, v in f.items() if k != "hunks"}
            g["hunks"] = []
            if not summary_only:
                left = cap
                for h in f["hunks"]:
                    lines = h["lines"] if left is None else h["lines"][:left]
                    if left is not None:
                        left -= len(lines)
                    if lines:
                        g["hunks"].append({**h, "lines": lines})
                    if left is not None and left <= 0:
                        g["truncated"] = True
                        break
            files.append(g)
        item["files"] = files
        item["files_total"] = len(c.get("files") or [])
        shown.append(item)
    if want_commit and not shown:
        raise _err(f"task {task['id']} does not list a commit starting with {want_commit!r}", 404)
    comments = _comments(self, task)
    guide = _guide_row(self, task)
    return {"ok": True, "task_id": task["id"], "title": task["title"], "repo": task.get("repo"),
            "repositories": list(dict.fromkeys(name for c in commits if not c.get("error")
                                               for name in c.get("repository_candidates", [c["repo"]]))),
            "status": task["status"], "owner": task.get("owner"), "reviewer": task.get("reviewer"),
            "submission_revision": int(task.get("submission_revision") or 0), "commits": shown,
            "comments": comments, "has_guide": guide is not None,
            "thread_id": task.get("thread_id")}


def review_comments(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The line comments left on a task's delivery (all revisions unless ``revision`` is given)."""
    task = self._task_row(a.get("task_id"))
    rows = _comments(self, task, revision=a.get("revision"), commit=str(a.get("commit") or "").strip(),
                     path=str(a.get("path") or "").strip().replace("\\", "/"))
    return {"ok": True, "task_id": task["id"], "submission_revision": int(task.get("submission_revision") or 0),
            "comments": rows, "count": len(rows)}


def _quote(text: str, limit: int = 160) -> str:
    text = text.replace("\t", "    ").rstrip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def review_comment_send(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Send a review's comments to whoever delivered: each ``{commit, path, line, side?, end_line?, body}`` is checked
    against the diff (the line must be in it), stored, and the whole batch (with an optional general ``body``) goes
    to the task's thread as one message that mentions the owner, with the code line each comment refers to."""
    raw = a.get("comments") or []
    general = str(a.get("body") or "").strip()
    if not isinstance(raw, list) or len(raw) > MAX_COMMENTS:
        raise _err(f"comments must be a list of at most {MAX_COMMENTS} objects")
    if not raw and not general:
        raise _err("send at least one comment or a general body")
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    task = self._task_row(a.get("task_id"))
    revision = int(task.get("submission_revision") or 0)
    expected = a.get("expected_submission_revision")
    if expected not in (None, ""):
        if type(expected) is not int or expected < 1:
            raise _err("expected_submission_revision must be an integer >= 1")
        if expected != revision:
            raise _err("the submission changed after it was observed", 409, error_code="stale_submission",
                       current_revision=revision, current_commits=task["commits"])
    if raw and revision < 1:
        raise _err(f"task {task['id']} has no submission to comment on", 409)
    commits = _task_diff(self, task) if raw else []
    checked: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _err(f"comments[{i}] must be an object")
        text = str(item.get("body") or "").strip()
        if not text:
            raise _err(f"comments[{i}].body is required")
        if len(text) > MAX_COMMENT_CHARS:
            raise _err(f"comments[{i}].body is longer than {MAX_COMMENT_CHARS} characters")
        side = str(item.get("side") or "new").strip().lower()
        if side not in SIDES:
            raise _err(f"comments[{i}].side must be new or old")
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            raise _err(f"comments[{i}].path is required")
        commit = _match_commit(commits, item.get("commit"))
        if commit is None:
            raise _err(f"comments[{i}].commit must name exactly one commit of the delivery "
                       f"({', '.join(c['short'] for c in commits)})")
        if commit.get("error"):
            raise _err(f"comments[{i}]: commit {commit['short']} cannot be read: {commit['error']}", 409)
        line = _int(item.get("line"), f"comments[{i}].line")
        end = item.get("end_line")
        end = None if end in (None, "") else _int(end, f"comments[{i}].end_line")
        if end is not None and end < line:
            raise _err(f"comments[{i}].end_line must not be before line")
        first = _find_line(commit, path, side, line)
        if first is None:
            raise _err(f"comments[{i}]: {path} {side} line {line} is not in the diff of {commit['short']}", 404)
        if end is not None and _find_line(commit, path, side, end) is None:
            raise _err(f"comments[{i}]: {path} {side} line {end} is not in the diff of {commit['short']}", 404)
        checked.append({"commit": commit["sha"], "short": commit["short"], "path": path, "side": side, "line": line,
                        "end_line": end, "code": _quote(first["text"]), "body": text})
    owner = task.get("owner")
    mentions = [owner] if owner and owner != agent else []
    lines = [f"Revisión de la entrega r{revision}: {len(checked)} comentario(s) en línea"
             + (" y una nota general" if general and checked else "") if checked else
             f"Revisión de la entrega r{revision}: nota general"]
    if checked:
        lines.append("Commits: " + ", ".join(c["short"] for c in commits if not c.get("error")))
    for c in checked:
        span = f"{c['line']}" if not c["end_line"] else f"{c['line']}-{c['end_line']}"
        lines += ["", f"[{c['short']} {c['path']}:{span} ({'nuevo' if c['side'] == 'new' else 'antes'})]",
                  f"    > {c['code']}", f"    {c['body']}".replace("\n", "\n    ")]
    if general:
        lines += ["", general]
    with self.db.tx():
        self._touch(agent)
        mid = self._msg(int(task["thread_id"]), agent, "comment", "\n".join(lines), mentions)
        now = self.clock()
        ids = [self.db.insert("agora_review_comments", {
            "space_id": self.space_id, "task_id": task["id"], "submission_revision": revision, "author": agent,
            "commit_sha": c["commit"], "path": c["path"], "side": c["side"], "line": c["line"],
            "end_line": c["end_line"], "code": c["code"], "body": c["body"], "message_id": mid, "created": now})
            for c in checked]
    self._bump("agora.review.comments", {"task_id": task["id"], "thread_id": task["thread_id"], "by": agent,
                                          "count": len(checked), "submission_revision": revision})
    session = None
    lookup = getattr(self, "review_session_hook", None)
    if owner and lookup is not None:
        try:
            session = lookup(owner)
        except Exception:  # noqa: BLE001 - a missing console never stops the comments from being delivered
            session = None
    delivery = {"thread_id": task["thread_id"], "message_id": mid, "mentioned": mentions,
                "session": session,
                "detail": ("Delivered in the task's thread, mentioning " + owner + "; the agent reads it on its next "
                           "inbox check" if mentions else "Written in the task's thread (nobody else to mention)")}
    return {"ok": True, "task_id": task["id"], "submission_revision": revision, "message_id": mid,
            "comment_ids": ids, "count": len(checked), "delivery": delivery}


# ---- the guided review ----------------------------------------------------------------------------------------

def _guide_row(ag: Any, task: dict[str, Any]) -> Optional[dict[str, Any]]:
    row = ag.db.one("SELECT * FROM agora_review_guides WHERE space_id=? AND task_id=? AND submission_revision=?",
                    (ag.space_id, task["id"], int(task.get("submission_revision") or 0)))
    if row is None:
        return None
    d = dict(row)
    try:
        d["chapters"] = json.loads(d.get("chapters") or "[]")
    except ValueError:
        d["chapters"] = []
    return d


def _commits_sig(commits: list[dict[str, Any]]) -> str:
    return hashlib.sha256("|".join(str(c.get("sha")) for c in commits).encode()).hexdigest()[:16]


def _diff_for_prompt(commits: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """The diff as compact text for the model (every file with its hunks), shortened evenly past the budget."""
    blocks: list[tuple[str, str]] = []
    for c in commits:
        for f in c.get("files") or []:
            lines = [f"### {f['path']} ({f['status']}, +{f['additions']} -{f['deletions']})"]
            if f["binary"]:
                lines.append("(binary file)")
            for h in f["hunks"]:
                lines.append(h["header"])
                for ln in h["lines"]:
                    lines.append({"add": "+", "del": "-", "ctx": " "}[ln["t"]] + ln["text"][:240])
            blocks.append((f["path"], "\n".join(lines)))
    total = sum(len(b) for _p, b in blocks)
    budget = MAX_GUIDE_CHARS
    out: list[str] = []
    cut: list[str] = []
    for path, block in blocks:
        share = max(1200, int(budget * len(block) / total)) if total > budget else len(block)
        if len(block) > share:
            block = block[:share] + "\n… (shortened)"
            cut.append(path)
        out.append(block)
    return "\n\n".join(out), cut


_GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "chapters": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
                           "files": {"type": "array", "items": {"type": "string"}},
                           "check": {"type": "string"}},
            "required": ["title", "summary", "files"]}},
    },
    "required": ["summary", "chapters"],
}


def build_chapters(raw: Any, known_paths: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """The model's answer as ``(summary, chapters)``: only paths that are in the diff survive, and a file the model
    left out goes to a last chapter so the guide always covers the whole diff."""
    if not isinstance(raw, dict):
        raise _err("the model did not answer with a guide", 502)
    known = set(known_paths)
    chapters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        files = [str(p).strip().replace("\\", "/") for p in (item.get("files") or [])]
        files = [p for p in dict.fromkeys(files) if p in known]
        title = str(item.get("title") or "").strip()[:160]
        if not title or not files:
            continue
        chapters.append({"title": title, "summary": str(item.get("summary") or "").strip()[:1200],
                         "check": str(item.get("check") or "").strip()[:600], "files": files})
        seen.update(files)
        if len(chapters) >= MAX_CHAPTERS:
            break
    if not chapters:
        raise _err("the model's guide named no file of the diff", 502)
    rest = [p for p in known_paths if p not in seen]
    if rest:
        chapters.append({"title": "Resto de cambios", "summary": "Ficheros del diff que ningún capítulo anterior cubre.",
                         "check": "", "files": rest})
    return str(raw.get("summary") or "").strip()[:1500], chapters


def review_guide(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Order a delivery's diff into chapters with the family's language model (Hoard Link). The guide is kept for the
    submission revision it read: asking again returns it, ``refresh`` writes it anew and ``only_cached`` never asks
    the model (``guide`` is null when there is none yet)."""
    task = self._task_row(a.get("task_id"))
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    cached = _guide_row(self, task)
    if cached is not None and not a.get("refresh"):
        return {"ok": True, "cached": True, "task_id": task["id"], "guide": _guide_view(cached)}
    if a.get("only_cached"):
        return {"ok": True, "cached": False, "task_id": task["id"], "guide": None}
    chat = getattr(self, "review_chat", None)
    if chat is None:
        raise _err("this Hub has no language model hooked to the Ágora: the guided review needs Hoard Link", 503)
    commits = [c for c in _task_diff(self, task) if not c.get("error")]
    if not commits:
        raise _err("none of the task's commits can be read from a registered checkout", 409)
    known = list(dict.fromkeys(f["path"] for c in commits for f in c.get("files") or []))
    if not known:
        raise _err("the task's commits change no files", 409)
    diff_text, shortened = _diff_for_prompt(commits)
    subjects = "\n".join(f"- {c['short']} {c.get('subject', '')}" for c in commits)
    system = ("Eres un revisor de código que prepara una lectura guiada de una entrega para otra persona. Ordena los "
              "cambios en capítulos en el orden en que conviene leerlos (primero lo que da contexto a lo demás: "
              "modelos de datos y contratos, después la lógica, la interfaz y al final las pruebas y documentos). "
              "Cada capítulo lleva un título corto, un resumen de lo que cambia y por qué, la lista exacta de ficheros "
              "(rutas tal como aparecen en el diff) y, en 'check', qué conviene comprobar al leerlo. Escribe en "
              "español. No inventes ficheros ni comportamientos que el diff no muestre.")
    user = (f"Tarea #{task['id']}: {task['title']}\n\nDescripción de la entrega:\n{(task.get('body') or '')[:1500]}\n\n"
            f"Commits:\n{subjects}\n\nFicheros del diff ({len(known)}):\n" + "\n".join(f"- {p}" for p in known)
            + f"\n\nDiff:\n{diff_text}")
    status, payload = chat({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            "json": _GUIDE_SCHEMA, "timeout_s": GUIDE_TIMEOUT_S, "max_tokens": 4000, "effort": "low",
                            "temperature": 0.2})
    if not isinstance(payload, dict) or not payload.get("ok"):
        detail = (payload or {}).get("detail") or (payload or {}).get("error") or "no answer"
        raise _err(f"the language model could not write the guide: {detail}", 503 if status in (0, 502, 503, 504) else 502)
    summary, chapters = build_chapters(payload.get("json"), known)
    if shortened:
        summary = (summary + "\n\n" if summary else "") + f"El modelo leyó una versión abreviada de: {', '.join(shortened[:8])}."
    now = self.clock()
    sig = _commits_sig(commits)
    with self.db.tx():
        self._touch(agent)
        self.db.execute("DELETE FROM agora_review_guides WHERE space_id=? AND task_id=? AND submission_revision=?",
                        (self.space_id, task["id"], int(task.get("submission_revision") or 0)))
        self.db.insert("agora_review_guides", {
            "space_id": self.space_id, "task_id": task["id"],
            "submission_revision": int(task.get("submission_revision") or 0), "commits_sig": sig,
            "model": str(payload.get("model") or ""), "summary": summary,
            "chapters": json.dumps(chapters, ensure_ascii=False), "requested_by": agent, "created": now})
    self._bump("agora.review.guide", {"task_id": task["id"], "by": agent, "chapters": len(chapters)})
    return {"ok": True, "cached": False, "task_id": task["id"], "guide": _guide_view(_guide_row(self, task))}


def _guide_view(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {"submission_revision": row["submission_revision"], "model": row["model"], "summary": row["summary"],
            "chapters": row["chapters"], "requested_by": row["requested_by"], "created": row["created"]}


#: Operation name → function; agora.py binds each as ``Agora.<name>`` under the space decorator.
OPERATIONS = {"review_diff": review_diff, "review_comments": review_comments,
              "review_comment_send": review_comment_send, "review_guide": review_guide}
WRITE_OPS = ("review_comment_send", "review_guide")


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    """MCP tool definitions (``schema`` is agora._schema)."""
    task = {"type": "integer"}
    return [
        {"name": "hub_agora_review_diff", "read": True,
         "description": "The diff of a task's submission by repository and commit: files, hunks "
                        "and lines with their old/new numbers, plus the line comments already left. Narrow it with "
                        "commit and path; summary_only returns just the per-file counts; max_lines caps the lines "
                        "of each file. A comment is anchored on (commit, path, side, line) of this diff.",
         "inputSchema": schema({"task_id": task, "commit": {"type": "string"}, "path": {"type": "string"},
                                "summary_only": {"type": "boolean"}, "max_lines": {"type": "integer", "minimum": 1}},
                               ["task_id"])},
        {"name": "hub_agora_review_comments", "read": True,
         "description": "The line comments left on a task's delivery (every submission revision unless revision is "
                        "given; those of an earlier revision say outdated). Filter by commit or path.",
         "inputSchema": schema({"task_id": task, "revision": {"type": ["integer", "string"]},
                                "commit": {"type": "string"}, "path": {"type": "string"}}, ["task_id"])},
        {"name": "hub_agora_review_comment_send",
         "description": "Comment on lines of a delivery's diff and send them to whoever delivered: comments is a list "
                        "of {commit, path, line, side new|old, end_line?, body}; each line must exist in the diff "
                        "(hub_agora_review_diff). The batch, with an optional general body, arrives as one message in "
                        "the task's thread mentioning the owner (its inbox wakes) and is stored for later listing. "
                        "Send expected_submission_revision to refuse a delivery that changed meanwhile. It does not "
                        "vote: use hub_agora_task_review for approve/changes.",
         "inputSchema": schema({"agent": agent_prop, "task_id": task, "comments": {
             "type": "array", "maxItems": MAX_COMMENTS, "items": {
                 "type": "object", "properties": {
                     "commit": {"type": "string"}, "path": {"type": "string"}, "line": {"type": "integer"},
                     "side": {"type": "string", "enum": list(SIDES)}, "end_line": {"type": "integer"},
                     "body": {"type": "string"}}, "required": ["path", "line", "body"]}},
                                "body": {"type": "string"},
                                "expected_submission_revision": {"type": "integer", "minimum": 1}},
                               ["agent", "task_id"])},
        {"name": "hub_agora_review_guide",
         "description": "Guided review: the family's language model (Hoard Link, the Sparks) orders a delivery's diff "
                        "into chapters, each with a summary, what to check and the files it covers, to read next to "
                        "the real diff. Kept per submission revision: asking again returns it; refresh writes it anew; "
                        "only_cached never calls the model (guide is null when none exists).",
         "inputSchema": schema({"agent": agent_prop, "task_id": task, "refresh": {"type": "boolean"},
                                "only_cached": {"type": "boolean"}}, ["agent", "task_id"])},
    ]
