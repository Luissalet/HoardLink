"""Ágora debates: a structured argument with findings inside a thread.

A debate freezes a snapshot of the question (the thread's or task's text plus the files and commits it links,
pinned to the repository's HEAD) and invites chosen agents. Each participant files **findings**:
``{claim, evidence[], status, disposition, author}``. Evidence items are ``file`` (``path#L10-L20`` or
``path:12``), ``commit``, ``url``, ``command`` (with its output) or ``note``. The others **verify**,
**challenge** or **reject** a finding with a short note; every act is an ordered event, so the rounds are simply
the event sequence. A finding's status follows the latest stance of each participant other than its author:

* ``rejected`` when the author withdraws it, when Luis rejects it, or when rejections outnumber verifications;
* ``challenged`` while any participant's latest stance is a challenge or an unresolved rejection;
* ``verified`` when at least one other participant verified it and nobody objects;
* ``proposed`` otherwise. Luis's stance, when he gives one, decides.

The consensus view groups the findings by status and says who agrees with what. Closing the debate writes the
outcome into the decision log (it resolves the host thread, or opens and resolves a ``decision`` thread when the
host is a task thread) and can open tasks from the verified findings whose disposition is ``adopt`` or ``adapt``.
Participants are mentioned in the host thread on every step, so the Ágora inbox (and its long wait) wakes them.

This module holds the operations; ``agora.py`` binds them to :class:`Agora` (``debate_*``), the REST routes
(``GET /api/agora/debates[/<id>]``, ``POST /api/agora/debate_<op>``) and the MCP tools ``hub_agora_debate_*``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

FINDING_STATUSES = ("proposed", "challenged", "verified", "rejected")
DISPOSITIONS = ("adopt", "adapt", "drop", "needs_luis")
STANCES = ("verify", "challenge", "reject")
EVIDENCE_KINDS = ("file", "commit", "url", "command", "note")
DEBATE_STATUSES = ("open", "closed")

#: Appended to ``agora.MIGRATIONS`` in order (see the migrations block there).
DEBATE_MIGRATION = """
    CREATE TABLE agora_debates (id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL DEFAULT 'ecosystem',
        thread_id INTEGER NOT NULL, title TEXT NOT NULL, question TEXT NOT NULL, snapshot TEXT NOT NULL,
        participants TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open', opened_by TEXT NOT NULL,
        outcome TEXT, closed_by TEXT, decision_thread_id INTEGER, spawned_tasks TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL, updated REAL NOT NULL, closed_at REAL);
    CREATE INDEX agora_debates_thread ON agora_debates(space_id, thread_id);
    CREATE TABLE agora_debate_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, debate_id INTEGER NOT NULL,
        author TEXT NOT NULL, claim TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'proposed', disposition TEXT NOT NULL DEFAULT 'needs_luis',
        message_id INTEGER, task_id INTEGER, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX agora_debate_findings_debate ON agora_debate_findings(debate_id, id);
    CREATE TABLE agora_debate_events (id INTEGER PRIMARY KEY AUTOINCREMENT, debate_id INTEGER NOT NULL,
        finding_id INTEGER, author TEXT NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
        message_id INTEGER, created REAL NOT NULL);
    CREATE INDEX agora_debate_events_debate ON agora_debate_events(debate_id, id);
"""

_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FILE_REF = re.compile(r"^(?P<path>[^\s#:]+?)(?:#L(?P<a>\d+)(?:-L?(?P<b>\d+))?|:(?P<c>\d+)(?:-(?P<d>\d+))?)?$")
_MAX_FREEZE_LINES = 200
_MAX_FREEZE_CHARS = 8000


def _ag():
    """The agora module, imported lazily (it imports this one)."""
    from . import agora
    return agora


def _err(message: str, status: int = 400):
    return _ag().AgoraError(message, status)


def guess_evidence_kind(ref: str) -> str:
    s = ref.strip()
    if re.match(r"^https?://", s, re.I):
        return "url"
    if _HEX.match(s):
        return "commit"
    if s.startswith(("$ ", "> ", "PS> ")):
        return "command"
    m = _FILE_REF.match(s)
    if m and ("/" in m["path"] or "\\" in m["path"] or "." in m["path"]) and not s.endswith("."):
        return "file"
    return "note"


def normalize_evidence(value: Any) -> list[dict[str, str]]:
    """Strings are classified (``path#L3-L9`` file, sha commit, URL, ``$ cmd`` + newline + output command, else
    note); objects take ``{kind, ref, output?, note?}``. At most 30 items."""
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    out: list[dict[str, str]] = []
    for item in items[:30]:
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            kind = guess_evidence_kind(text.splitlines()[0])
            ref, _, rest = text.partition("\n")
            if kind == "command":
                ref = re.sub(r"^(\$|>|PS>)\s+", "", ref)
                out.append({"kind": kind, "ref": ref[:2000], "output": rest.strip()[:8000]})
            else:
                out.append({"kind": kind, "ref": text[:2000]})
        elif isinstance(item, dict):
            ref = str(item.get("ref") or item.get("path") or item.get("url") or item.get("commit")
                      or item.get("command") or "").strip()
            kind = str(item.get("kind") or "").strip().lower() or (guess_evidence_kind(ref) if ref else "note")
            if kind not in EVIDENCE_KINDS:
                raise _err(f"evidence kind must be one of {', '.join(EVIDENCE_KINDS)}")
            entry = {"kind": kind, "ref": ref[:2000]}
            for key, limit in (("output", 8000), ("note", 2000)):
                if item.get(key):
                    entry[key] = str(item[key])[:limit]
            if not entry["ref"] and not entry.get("note") and not entry.get("output"):
                continue
            out.append(entry)
        else:
            raise _err("evidence items are strings or {kind, ref, output, note} objects")
    return out


def normalize_disposition(value: Any, default: str = "needs_luis") -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return default
    if text in ("needs", "luis", "needs_luis", "ask_luis", "person"):
        return "needs_luis"
    if text not in DISPOSITIONS:
        raise _err("disposition must be adopt, adapt, drop or needs_luis")
    return text


def finding_stances(author: str, events: list[dict[str, Any]]) -> dict[str, str]:
    """Latest stance of each participant on one finding (events oldest first). The author's own ``reject`` is a
    withdrawal and is kept under its name; its verify/challenge on its own finding count for nothing."""
    stances: dict[str, str] = {}
    for ev in events:
        if ev["kind"] not in STANCES:
            continue
        if ev["author"] == author and ev["kind"] != "reject":
            continue
        stances[ev["author"]] = ev["kind"]
    return stances


def finding_status(author: str, stances: dict[str, str], person: str = "luis") -> str:
    if stances.get(author) == "reject":
        return "rejected"
    if person in stances and person != author:
        return {"verify": "verified", "challenge": "challenged", "reject": "rejected"}[stances[person]]
    values = [v for k, v in stances.items() if k != author]
    verifies, challenges, rejects = values.count("verify"), values.count("challenge"), values.count("reject")
    if rejects and rejects > verifies:
        return "rejected"
    if challenges or rejects:
        return "challenged"
    if verifies:
        return "verified"
    return "proposed"


def _freeze_file(repo_path: Any, head: Optional[str], ref: str) -> dict[str, Any]:
    """A file reference pinned to the repository's HEAD; with a line range its text is frozen too."""
    A = _ag()
    entry: dict[str, Any] = {"ref": ref}
    m = _FILE_REF.match(ref.strip())
    if not m:
        return entry
    try:
        rel = A._normalize_repo_relpath(m["path"])
    except A.AgoraError:
        return entry
    entry["path"] = rel
    if head:
        entry["head"] = head
    start = int(m["a"] or m["c"] or 0)
    end = int(m["b"] or m["d"] or start or 0)
    if repo_path is None or not head or not start:
        return entry
    end = min(end, start + _MAX_FREEZE_LINES - 1)
    try:
        text = A._slice_lines(A._git_show(repo_path, head, rel), start, end)
    except A.AgoraError as exc:
        entry["freeze_error"] = str(exc)[:300]
        return entry
    entry.update(lines=[start, end], text=text[:_MAX_FREEZE_CHARS])
    return entry


def _repo_head(ag: Any, repo: str) -> tuple[Any, Optional[str]]:
    if not repo or not getattr(ag, "repo_lookup", None):
        return None, None
    try:
        path = ag.repo_lookup(repo)
        if path is None:
            return None, None
        res = _ag()._git_run(path, "rev-parse", "HEAD", timeout=5.0)
        head = (res.stdout or "").strip() if res.returncode == 0 else ""
        return path, (head if _HEX.match(head or "") else None)
    except Exception:  # noqa: BLE001 - a snapshot without a pinned HEAD is still a snapshot
        return None, None


def _participants(ag: Any, value: Any, opener: str) -> list[str]:
    A = _ag()
    out = [opener]
    for raw in A._list(value, "participants", 20):
        who = raw.strip().lower().lstrip("@")
        if not A._AGENT_RE.match(who) or who in ("hub", "ui", "system"):
            raise _err(f"participant {raw!r} is not an agent id")
        if who not in out:
            out.append(who)
    return out


def _join(ag: Any, debate: dict[str, Any], agent: str) -> list[str]:
    people = list(debate["participants"])
    if agent not in people:
        people.append(agent)
        ag.db.execute("UPDATE agora_debates SET participants=? WHERE id=?", (json.dumps(people), debate["id"]))
    return people


def _event(ag: Any, debate_id: int, finding_id: Optional[int], author: str, kind: str, note: str,
           message_id: Optional[int]) -> int:
    return ag.db.insert("agora_debate_events", {"debate_id": debate_id, "finding_id": finding_id, "author": author,
                                                "kind": kind, "note": note, "message_id": message_id,
                                                "created": ag.clock()})


def _debate_row(ag: Any, debate_id: Any) -> dict[str, Any]:
    try:
        did = int(debate_id)
    except (TypeError, ValueError):
        raise _err("debate_id must be a number") from None
    row = ag.db.one("SELECT * FROM agora_debates WHERE id=? AND space_id=?", (did, ag.space_id))
    if row is None:
        raise _err(f"no debate {did}", 404)
    d = dict(row)
    d["participants"] = _ag()._l(d.get("participants"))
    d["spawned_tasks"] = _ag()._l(d.get("spawned_tasks"))
    try:
        d["snapshot"] = json.loads(d["snapshot"]) if d.get("snapshot") else {}
    except ValueError:
        d["snapshot"] = {}
    return d


def _finding_row(ag: Any, finding_id: Any) -> dict[str, Any]:
    try:
        fid = int(str(finding_id).lstrip("Ff#"))
    except (TypeError, ValueError):
        raise _err("finding_id must be a number") from None
    row = ag.db.one("SELECT f.* FROM agora_debate_findings f JOIN agora_debates d ON d.id=f.debate_id "
                    "WHERE f.id=? AND d.space_id=?", (fid, ag.space_id))
    if row is None:
        raise _err(f"no finding {fid}", 404)
    d = dict(row)
    d["evidence"] = _ag()._l(d.get("evidence"))
    return d


def debate_view(ag: Any, debate_id: Any) -> dict[str, Any]:
    """The debate, its findings with who verified / challenged / rejected each, the ordered events and the
    consensus (findings grouped by status, each participant's tally, what is adopted)."""
    debate = _debate_row(ag, debate_id)
    events = [dict(r) for r in ag.db.query("SELECT * FROM agora_debate_events WHERE debate_id=? ORDER BY id",
                                           (debate["id"],))]
    for seq, ev in enumerate(events, 1):
        ev["seq"] = seq
    findings = []
    for r in ag.db.query("SELECT * FROM agora_debate_findings WHERE debate_id=? ORDER BY id", (debate["id"],)):
        f = dict(r)
        f["evidence"] = _ag()._l(f.get("evidence"))
        own = [ev for ev in events if ev["finding_id"] == f["id"]]
        stances = finding_stances(f["author"], own)
        f["stances"] = stances
        f["verified_by"] = sorted(k for k, v in stances.items() if v == "verify")
        f["challenged_by"] = sorted(k for k, v in stances.items() if v == "challenge")
        f["rejected_by"] = sorted(k for k, v in stances.items() if v == "reject")
        f["events"] = own
        f["round"] = sum(1 for ev in own if ev["kind"] in STANCES) + 1
        findings.append(f)
    by_status: dict[str, list[int]] = {s: [] for s in FINDING_STATUSES}
    for f in findings:
        by_status.setdefault(f["status"], []).append(f["id"])
    tally = {p: {"filed": 0, "verify": 0, "challenge": 0, "reject": 0} for p in debate["participants"]}
    for f in findings:
        tally.setdefault(f["author"], {"filed": 0, "verify": 0, "challenge": 0, "reject": 0})["filed"] += 1
        for who, stance in f["stances"].items():
            if who != f["author"]:
                tally.setdefault(who, {"filed": 0, "verify": 0, "challenge": 0, "reject": 0})[stance] += 1
    adopted = [f["id"] for f in findings if f["status"] == "verified" and f["disposition"] in ("adopt", "adapt")]
    counts = {s: len(ids) for s, ids in by_status.items()}
    others = [p for p in debate["participants"]]
    unanimous = [f["id"] for f in findings if f["status"] == "verified"
                 and set(p for p in others if p != f["author"]) <= set(f["verified_by"])]
    summary = ", ".join(f"{counts[s]} {s}" for s in FINDING_STATUSES if counts.get(s)) or "no findings yet"
    consensus = {"by_status": by_status, "counts": counts, "participants": tally, "adopted": adopted,
                 "unanimous": unanimous, "needs_luis": [f["id"] for f in findings if f["disposition"] == "needs_luis"
                                                        and f["status"] != "rejected"],
                 "summary": summary}
    return {"debate": debate, "findings": findings, "events": events, "consensus": consensus}


def _summary_lines(view: dict[str, Any]) -> list[str]:
    lines = []
    for status in FINDING_STATUSES:
        for f in view["findings"]:
            if f["status"] != status:
                continue
            who = f" · verifican {', '.join(f['verified_by'])}" if f["verified_by"] else ""
            lines.append(f"- F{f['id']} [{status} · {f['disposition']}] {f['author']}: {f['claim'][:300]}{who}")
    return lines


def _evidence_lines(evidence: list[dict[str, Any]]) -> list[str]:
    out = []
    for e in evidence:
        tail = ""
        if e.get("output"):
            tail = " → " + e["output"].strip().splitlines()[0][:200] if e["output"].strip() else ""
        out.append(f"  - [{e['kind']}] {e.get('ref') or e.get('note', '')}{tail}")
    return out


# ---- operations (bound to Agora as debate_* by agora.py) -------------------------------------------------------

def debate_open(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Open a debate on a thread (or a task's thread): freeze the question and its linked files/commits, invite
    the participants (mentioned in the thread, so their inbox wakes)."""
    A = _ag()
    person = bool(a.get("_person"))
    agent = self._agent(a.get("agent"), allow_person=person)
    if a.get("thread_id") in (None, "") and a.get("task_id") not in (None, ""):
        task = self._task_row(a.get("task_id"))
        thread = self._thread_row(task["thread_id"])
    else:
        thread = self._thread_row(a.get("thread_id"))
        task = self._task_row(thread["task_id"]) if thread.get("task_id") else None
    first = self.db.one("SELECT body FROM messages WHERE thread_id=? ORDER BY id LIMIT 1", (thread["id"],))
    opening = str(first["body"] or "") if first else ""
    title = A._txt(a.get("title"), 200, "title") or thread["title"]
    question = (A._txt(a.get("question"), 20000, "question") or opening or (task or {}).get("body")
                or thread["title"])
    participants = _participants(self, a.get("participants"), agent)
    files = A._list(a.get("files"), "files", 30)
    commits = A._list(a.get("commits"), "commits", 30)
    for c in (task or {}).get("commits") or []:
        if isinstance(c, str) and c not in commits:
            commits.append(c)
    repo = A._txt(a.get("repo"), 120, "repo") or str((task or {}).get("repo") or "")
    repo_path, head = _repo_head(self, repo)
    now = self.clock()
    snapshot: dict[str, Any] = {
        "taken_at": now, "question": question, "repo": repo, "head": head,
        "thread": {"id": thread["id"], "title": thread["title"], "kind": thread["kind"], "status": thread["status"],
                   "created_by": thread.get("created_by"), "opening": opening[:20000]},
        "task": ({k: task.get(k) for k in ("id", "title", "body", "acceptance", "repo", "paths", "commits", "branch",
                                           "status", "owner")} if task else None),
        "files": [_freeze_file(repo_path, head, f) for f in files[:30]],
        "commits": commits,
    }
    snapshot["digest"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
                                        .encode("utf-8")).hexdigest()
    others = [p for p in participants if p != agent]
    with self.db.tx():
        self._touch(agent)
        did = self.db.insert("agora_debates", {
            "space_id": self.space_id, "thread_id": thread["id"], "title": title, "question": question,
            "snapshot": json.dumps(snapshot, ensure_ascii=False, default=str), "participants": json.dumps(participants),
            "status": "open", "opened_by": agent, "created": now, "updated": now})
        pinned = f" @ {head[:10]}" if head else ""
        body = "\n".join([
            f"Debate #{did} abierto: {title}",
            f"Pregunta: {question[:1500]}",
            f"Participantes: {', '.join(participants)}",
            f"Instantánea: {len(files)} fichero(s), {len(commits)} commit(s){pinned} · sha256 {snapshot['digest'][:12]}",
            f"Hallazgos: agora debate finding {did} --claim \"...\" --evidence ruta#L1-L9 --disposition adopt"])
        mid = self._msg(thread["id"], agent, "comment", body, others)
        self._ack(agent, thread["id"], mid)
        _event(self, did, None, agent, "open", title, mid)
    self._bump("agora.debate.opened", {"debate_id": did, "thread_id": thread["id"], "by": agent,
                                       "participants": participants, "title": title})
    return {"ok": True, **debate_view(self, did)}


def _open_debate(ag: Any, debate_id: Any) -> dict[str, Any]:
    debate = _debate_row(ag, debate_id)
    if debate["status"] != "open":
        raise _err(f"debate {debate['id']} is closed", 409)
    return debate


def debate_finding(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """File a finding (claim + evidence + disposition), or amend one with ``finding_id`` (its author or Luis):
    a new claim, more evidence (appended) or another disposition. Anyone who files joins the debate."""
    A = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    amend = a.get("finding_id") not in (None, "")
    if amend:
        finding = _finding_row(self, a.get("finding_id"))
        debate = _open_debate(self, finding["debate_id"])
        if agent not in (finding["author"], A.PERSON):
            raise _err(f"only {finding['author']} (or {A.PERSON}) amends finding F{finding['id']}; "
                       "answer it with challenge / verify instead", 403)
    else:
        debate = _open_debate(self, a.get("debate_id"))
        finding = None
    claim = A._txt(a.get("claim"), 4000, "claim", required=not amend)
    evidence = normalize_evidence(a.get("evidence"))
    disposition = normalize_disposition(a.get("disposition"), default=finding["disposition"] if finding else "needs_luis")
    now = self.clock()
    with self.db.tx():
        self._touch(agent)
        people = _join(self, debate, agent)
        mentions = [p for p in people if p != agent]
        if disposition == "needs_luis" and A.PERSON not in mentions and agent != A.PERSON:
            mentions.append(A.PERSON)
        if finding is None:
            fid = self.db.insert("agora_debate_findings", {
                "debate_id": debate["id"], "author": agent, "claim": claim, "evidence": json.dumps(evidence, ensure_ascii=False),
                "status": "proposed", "disposition": disposition, "created": now, "updated": now})
            head = f"Hallazgo F{fid} (debate #{debate['id']}) · {disposition}"
            text, kind = claim, "finding"
        else:
            fid = finding["id"]
            merged = finding["evidence"] + [e for e in evidence if e not in finding["evidence"]]
            self.db.execute("UPDATE agora_debate_findings SET claim=?, evidence=?, disposition=?, updated=? WHERE id=?",
                            (claim or finding["claim"], json.dumps(merged, ensure_ascii=False), disposition, now, fid))
            head = f"Hallazgo F{fid} corregido (debate #{debate['id']}) · {disposition}"
            text, kind = claim or finding["claim"], "amend"
        lines = [head, text] + (["Evidencia:"] + _evidence_lines(evidence) if evidence else [])
        mid = self._msg(debate["thread_id"], agent, "comment", "\n".join(lines), mentions)
        self._ack(agent, debate["thread_id"], mid)
        if finding is None:
            self.db.execute("UPDATE agora_debate_findings SET message_id=? WHERE id=?", (mid, fid))
        _event(self, debate["id"], fid, agent, kind, (claim or "")[:2000], mid)
        self.db.execute("UPDATE agora_debates SET updated=? WHERE id=?", (now, debate["id"]))
    self._bump("agora.debate.finding", {"debate_id": debate["id"], "thread_id": debate["thread_id"], "finding_id": fid,
                                        "by": agent, "kind": kind})
    view = debate_view(self, debate["id"])
    return {"ok": True, "finding": next(f for f in view["findings"] if f["id"] == fid), **view}


_STANCE_TEXT = {"verify": "Verifica", "challenge": "Cuestiona", "reject": "Rechaza"}


def _stance(self: Any, a: dict[str, Any], kind: str) -> dict[str, Any]:
    A = _ag()
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    finding = _finding_row(self, a.get("finding_id"))
    debate = _open_debate(self, finding["debate_id"])
    if a.get("debate_id") not in (None, "") and int(a["debate_id"]) != debate["id"]:
        raise _err(f"finding F{finding['id']} belongs to debate {debate['id']}")
    note = A._txt(a.get("note"), 4000, "note", required=kind != "verify")
    if agent == finding["author"] and kind != "reject":
        raise _err("the author answers a challenge by amending the finding (debate_finding with finding_id); "
                   "it can withdraw it with reject", 409)
    now = self.clock()
    with self.db.tx():
        self._touch(agent)
        _join(self, debate, agent)
        verb = "Retira" if agent == finding["author"] else _STANCE_TEXT[kind]
        mentions = [finding["author"]] if finding["author"] != agent else []
        body = f"{verb} F{finding['id']} (debate #{debate['id']}): {note or 'comprobado'}"
        mid = self._msg(debate["thread_id"], agent, "comment", body, mentions)
        self._ack(agent, debate["thread_id"], mid)
        _event(self, debate["id"], finding["id"], agent, kind, note, mid)
        own = [dict(r) for r in self.db.query("SELECT * FROM agora_debate_events WHERE finding_id=? ORDER BY id",
                                              (finding["id"],))]
        status = finding_status(finding["author"], finding_stances(finding["author"], own), A.PERSON)
        self.db.execute("UPDATE agora_debate_findings SET status=?, updated=? WHERE id=?", (status, now, finding["id"]))
        self.db.execute("UPDATE agora_debates SET updated=? WHERE id=?", (now, debate["id"]))
    self._bump("agora.debate.stance", {"debate_id": debate["id"], "thread_id": debate["thread_id"],
                                       "finding_id": finding["id"], "by": agent,
                                       "stance": kind, "status": status})
    view = debate_view(self, debate["id"])
    return {"ok": True, "finding": next(f for f in view["findings"] if f["id"] == finding["id"]), **view}


def debate_verify(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Verify someone else's finding (optional note: what you checked)."""
    return _stance(self, a, "verify")


def debate_challenge(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Challenge a finding with a short note (what is wrong or missing). Its author answers by amending it."""
    return _stance(self, a, "challenge")


def debate_reject(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Reject a finding with the reason; its author rejecting it withdraws it."""
    return _stance(self, a, "reject")


def debate_show(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **debate_view(self, a.get("debate_id"))}


def debate_list(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Debates of the space, newest first; ``thread_id`` / ``task_id`` / ``status`` filter, ``full`` returns each
    debate's findings and consensus (what the thread view paints)."""
    a = a or {}
    where, params = ["space_id=?"], [self.space_id]
    thread_id = a.get("thread_id")
    if thread_id in (None, "") and a.get("task_id") not in (None, ""):
        thread_id = self._task_row(a.get("task_id"))["thread_id"]
    if thread_id not in (None, ""):
        where.append("thread_id=?")
        params.append(int(thread_id))
    if a.get("status"):
        if a["status"] not in DEBATE_STATUSES:
            raise _err("status must be open or closed")
        where.append("status=?")
        params.append(a["status"])
    try:
        limit = max(1, min(int(a.get("limit") or 50), 200))
    except (TypeError, ValueError):
        raise _err("limit must be a number") from None
    full = str(a.get("full", "")).lower() in ("1", "true", "yes")
    rows = self.db.query("SELECT id FROM agora_debates WHERE " + " AND ".join(where) +
                         " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated DESC LIMIT ?", (*params, limit))
    out = []
    for r in rows:
        view = debate_view(self, r["id"])
        if full:
            out.append(view)
        else:
            d = {k: v for k, v in view["debate"].items() if k != "snapshot"}
            d["consensus"] = view["consensus"]
            out.append(d)
    res: dict[str, Any] = {"ok": True, "debates": out}
    if thread_id not in (None, ""):
        res["thread_id"] = int(thread_id)   # lets a page match live agora.debate.* events to the open conversation
    return res


def debate_close(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Close the debate with its outcome: final dispositions (``dispositions`` {finding id: disposition}), tasks
    opened from the verified findings marked adopt/adapt (``spawn_tasks``; ``spawn_findings`` picks the ids), and
    the outcome recorded in the decision log (the host thread is resolved, or a decision thread is opened and
    resolved when the host is a task thread or already resolved)."""
    A = _ag()
    person = bool(a.get("_person"))
    agent = self._agent(a.get("agent"), allow_person=person)
    debate = _open_debate(self, a.get("debate_id"))
    outcome = A._txt(a.get("outcome"), 20000, "outcome", required=True)
    disp = a.get("dispositions") or {}
    if not isinstance(disp, dict):
        raise _err("dispositions must be an object {finding id: adopt|adapt|drop|needs_luis}")
    now = self.clock()
    with self.db.tx():
        self._touch(agent)
        _join(self, debate, agent)
        for key, value in disp.items():
            f = _finding_row(self, key)
            if f["debate_id"] != debate["id"]:
                raise _err(f"finding F{f['id']} belongs to debate {f['debate_id']}")
            d = normalize_disposition(value)
            if d != f["disposition"]:
                self.db.execute("UPDATE agora_debate_findings SET disposition=?, updated=? WHERE id=?", (d, now, f["id"]))
                _event(self, debate["id"], f["id"], agent, "disposition", d, None)
    view = debate_view(self, debate["id"])
    picked = {int(str(x).lstrip("Ff#")) for x in A._list(a.get("spawn_findings"), "spawn_findings", 50)}
    spawn = bool(a.get("spawn_tasks")) or bool(picked)
    kind = str(a.get("task_kind") or "feature").lower()
    repo = str(view["debate"]["snapshot"].get("repo") or "")
    host = self._thread_row(debate["thread_id"])
    spawned: list[int] = []
    if spawn:
        for f in view["findings"]:
            chosen = f["id"] in picked if picked else (f["status"] == "verified" and f["disposition"] in ("adopt", "adapt"))
            if not chosen or f.get("task_id"):
                continue
            body = "\n".join([f["claim"], "", f"Hallazgo F{f['id']} del debate #{debate['id']} ({debate['title']}), "
                              f"{f['status']} · {f['disposition']}; verifican: {', '.join(f['verified_by']) or '-'}.",
                              f"Hilo: {host['id']}."] + (["Evidencia:"] + _evidence_lines(f["evidence"]) if f["evidence"] else []))
            res = self.task_add({"agent": agent, "_person": person, "space_id": self.space_id, "title": f["claim"][:300],
                                 "body": body, "repo": repo, "kind": kind, "priority": 2,
                                 "labels": ["debate", f"debate-{debate['id']}"],
                                 "context": {"debate_id": debate["id"], "finding_id": f["id"]}})
            tid = res["task"]["id"]
            spawned.append(tid)
            with self.db.tx():
                self.db.execute("UPDATE agora_debate_findings SET task_id=? WHERE id=?", (tid, f["id"]))
    view = debate_view(self, debate["id"])
    lines = [f"Debate #{debate['id']} ({debate['title']}): {outcome}", "",
             f"Hallazgos: {view['consensus']['summary']}"] + _summary_lines(view)
    if spawned:
        lines.append("Tareas creadas: " + ", ".join(f"#{t}" for t in spawned))
    resolution = "\n".join(lines)[:20000]
    common = {"agent": agent, "_person": person, "space_id": self.space_id}
    if host["kind"] != "task" and host["status"] != "resolved":
        decision_tid = host["id"]
    else:
        others = [p for p in view["debate"]["participants"] if p != agent]
        opened = self.thread_open({**common, "title": f"Debate #{debate['id']}: {debate['title']}"[:200],
                                   "body": debate["question"][:20000], "kind": "decision",
                                   "task_id": host.get("task_id"), "mentions": others})
        decision_tid = opened["thread"]["id"]
    self.resolve({**common, "thread_id": decision_tid, "resolution": resolution})
    now = self.clock()
    with self.db.tx():
        self.db.execute("UPDATE agora_debates SET status='closed', outcome=?, closed_by=?, decision_thread_id=?, "
                        "spawned_tasks=?, closed_at=?, updated=? WHERE id=?",
                        (outcome, agent, decision_tid, json.dumps(spawned), now, now, debate["id"]))
        where = "en este hilo" if decision_tid == host["id"] else f"en el hilo {decision_tid}"
        note = f"Debate #{debate['id']} cerrado por {agent}: {outcome[:1500]}\nDecisión registrada {where}."
        if spawned:
            note += " Tareas: " + ", ".join(f"#{t}" for t in spawned) + "."
        others = [p for p in view["debate"]["participants"] if p != agent]
        mid = self._msg(host["id"], agent, "comment", note, others)
        self._ack(agent, host["id"], mid)
        _event(self, debate["id"], None, agent, "close", outcome[:2000], mid)
    self._bump("agora.debate.closed", {"debate_id": debate["id"], "thread_id": host["id"], "by": agent,
                                       "decision_thread_id": decision_tid, "tasks": spawned})
    return {"ok": True, "decision_thread_id": decision_tid, "tasks": spawned, **debate_view(self, debate["id"])}


#: Operation name → function; agora.py binds each as ``Agora.<name>`` under the space decorator.
OPERATIONS = {"debate_open": debate_open, "debate_finding": debate_finding, "debate_verify": debate_verify,
              "debate_challenge": debate_challenge, "debate_reject": debate_reject, "debate_close": debate_close,
              "debate_show": debate_show, "debate_list": debate_list}
WRITE_OPS = ("debate_open", "debate_finding", "debate_verify", "debate_challenge", "debate_reject", "debate_close")


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    """MCP tool definitions (``schema`` is agora._schema)."""
    ints = {"type": "array", "items": {"type": ["integer", "string"]}}
    strs = {"type": "array", "items": {"type": "string"}}
    ev = {"type": "array", "maxItems": 30, "description":
          "Strings are classified: path#L10-L20 or path:12 (file), commit sha, URL, '$ command' + newline + output, "
          "else a note. Or objects {kind: file|commit|url|command|note, ref, output, note}.",
          "items": {"type": ["string", "object"]}}
    disp = {"type": "string", "enum": list(DISPOSITIONS)}
    return [
        {"name": "hub_agora_debate_open",
         "description": "Open a structured debate inside a thread (thread_id) or a task's thread (task_id): freezes a "
                        "snapshot of the question (thread/task text plus files pinned to the repo HEAD, line ranges "
                        "frozen as text, and commits) and invites participants (mentioned, so their inbox wakes). "
                        "Use it when agents disagree on facts or design: findings with evidence beat long replies.",
         "inputSchema": schema({"agent": agent_prop, "thread_id": {"type": "integer"}, "task_id": {"type": "integer"},
                                "title": {"type": "string"}, "question": {"type": "string"},
                                "participants": strs, "files": strs, "commits": strs, "repo": {"type": "string"}},
                               ["agent"])},
        {"name": "hub_agora_debate_finding",
         "description": "File a finding in a debate: claim, evidence[] and disposition adopt|adapt|drop|needs_luis "
                        "(needs_luis also mentions Luis). With finding_id the author amends it (new claim, more "
                        "evidence, another disposition): that is how a challenge is answered.",
         "inputSchema": schema({"agent": agent_prop, "debate_id": {"type": "integer"},
                                "finding_id": {"type": ["integer", "string"]}, "claim": {"type": "string"},
                                "evidence": ev, "disposition": disp}, ["agent"])},
        {"name": "hub_agora_debate_verify",
         "description": "Verify another participant's finding after checking its evidence (note: what you checked). "
                        "A finding is verified when someone other than its author verified it and nobody objects.",
         "inputSchema": schema({"agent": agent_prop, "finding_id": {"type": ["integer", "string"]},
                                "debate_id": {"type": "integer"}, "note": {"type": "string"}}, ["agent", "finding_id"])},
        {"name": "hub_agora_debate_challenge",
         "description": "Challenge a finding with a short note (what is wrong, missing or unproven). The author is "
                        "mentioned and answers by amending; your later verify replaces the challenge.",
         "inputSchema": schema({"agent": agent_prop, "finding_id": {"type": ["integer", "string"]},
                                "debate_id": {"type": "integer"}, "note": {"type": "string"}},
                               ["agent", "finding_id", "note"])},
        {"name": "hub_agora_debate_reject",
         "description": "Reject a finding with the reason (the author rejecting its own finding withdraws it). "
                        "Rejected when rejections outnumber verifications; Luis's stance decides.",
         "inputSchema": schema({"agent": agent_prop, "finding_id": {"type": ["integer", "string"]},
                                "debate_id": {"type": "integer"}, "note": {"type": "string"}},
                               ["agent", "finding_id", "note"])},
        {"name": "hub_agora_debate_close",
         "description": "Close a debate with its outcome: optional final dispositions {finding id: disposition}; "
                        "spawn_tasks opens a task per verified finding marked adopt/adapt (or spawn_findings lists "
                        "ids). The outcome and the findings enter the decision log (host thread resolved, or a "
                        "decision thread when the host is a task thread).",
         "inputSchema": schema({"agent": agent_prop, "debate_id": {"type": "integer"}, "outcome": {"type": "string"},
                                "dispositions": {"type": "object"}, "spawn_tasks": {"type": "boolean"},
                                "spawn_findings": ints, "task_kind": {"type": "string"}},
                               ["agent", "debate_id", "outcome"])},
        {"name": "hub_agora_debate_show", "read": True,
         "description": "One debate: frozen snapshot, findings with evidence and who verified / challenged / rejected "
                        "each, the ordered events (rounds) and the consensus grouped by status.",
         "inputSchema": schema({"debate_id": {"type": "integer"}}, ["debate_id"])},
        {"name": "hub_agora_debate_list", "read": True,
         "description": "Debates of the space (open first): filter by thread_id, task_id or status open|closed; "
                        "full=true includes findings and events.",
         "inputSchema": schema({"thread_id": {"type": "integer"}, "task_id": {"type": "integer"},
                                "status": {"type": "string", "enum": list(DEBATE_STATUSES)},
                                "full": {"type": "boolean"}, "limit": {"type": "integer"}}, [])},
    ]
