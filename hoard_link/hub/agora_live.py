"""Read-only projection of the existing board, channel and transcript observer."""
from __future__ import annotations

import re
from typing import Any

from . import agent_loop
from .agent_watch import snippet

COLORS = {"claude": "#d97757", "codex": "#10a37f", "cursor": "#7aa2f7", "faustus": "#d0545b"}
TOOL = {
    "name": "hub_agora_live", "read": True,
    "description": "Live server activity / actividad en directo de un servidor.\n"
                   "Read one server live: board, path locks, observed file/command snippets, channel messages and "
                   "one cursor target per active member. Targets carry source timestamps. No inferred movement, "
                   "file reads, model calls or read receipts. Idle after 60 seconds of silence. Each actor carries its "
                   "agent loop when its transcript is observed: phase plan|act|observe|done (model text = plan, tool "
                   "call = act, tool result = observe), lap, tool in flight (and its readable title), the turn's steps "
                   "(tools by readable name with a short argument summary, results with ok/error and time), the kind "
                   "of work the turn did (code|research|files|ops|chat) and that kind's practices each "
                   "ok|fail|unknown|pending with its evidence: code = tests, browser, docs, commit identity, no "
                   "evidence files, Agora delivery and task claimed with locks; research = sources opened and cited, "
                   "dated facts, nothing cited from an unseen source, answer language; files = output checked, "
                   "requested format, language; ops = commands ok, checked after running, language; chat = none. "
                   "Each task carries its owner's loop summary.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "space_id": {"type": "string"}, "channel": {"type": "string", "default": "general"}}},
}
PRACTICES_TOOL = {
    "name": "hub_agora_practices", "read": False,
    "description": "Agent loop and practice checklist / bucle y buenas prácticas de un miembro.\n"
                   "Read the observed loop of a member (phase plan|act|observe|done, lap, tool, steps of the turn) and "
                   "the practices of the kind of work its turn did (code, research, files, operations; none for a plain "
                   "conversation) marked from the commands it ran and what it answered; for code: tests, app checked in "
                   "a browser, docs updated, commit identity, no evidence files committed, Agora delivery. With "
                   "post=true and "
                   "task_id, writes the checklist as a comment in that task's thread so the review sees what was met "
                   "and what is missing. hub_agora_task_submit posts it there by itself when the submitter is observed.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "space_id": {"type": "string"}, "agent": {"type": "string"}, "member": {"type": "string"},
        "task_id": {"type": "integer"}, "post": {"type": "boolean", "default": False}}, "required": ["agent"]},
}
MARK = {"ok": "✓", "fail": "✗", "unknown": "?", "pending": "·"}
STATE = {"ok": "cumplida", "fail": "incumplida", "unknown": "sin confirmar", "pending": "falta"}


def practices_text(member: str, loop: dict[str, Any]) -> str:
    """The checklist as a thread comment: what was met, what failed and what is missing, with the evidence."""
    count = loop.get("practices_summary") or agent_loop.summary(loop["practices"])
    kind = agent_loop.KIND_LABELS.get(loop.get("kind") or "code", "Código").lower()
    lines = [f"Buenas prácticas observadas de {member} · {kind} (vuelta {loop.get('lap') or 0}): "
             f"{count['ok']} cumplidas · {count['fail']} incumplidas · {count['open']} faltan o sin confirmar"]
    for p in loop["practices"]:
        lines.append(f"- {MARK[p['state']]} {p['label']}: {STATE[p['state']]}"
                     + (f" — `{p['evidence']}`" if p.get("evidence") else ""))
    return "\n".join(lines)


def board_practices(loop: dict[str, Any], agent: str, own: list[dict[str, Any]],
                    locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The transcript checklist plus what the board itself proves: the task claimed with path locks, and a
    delivery the transcript may not show (a submit from another session or the page)."""
    practices = [dict(p) for p in loop.get("practices", [])]
    task = own[0] if own else None
    if (loop.get("kind") or "code") != "code":
        return practices                 # the board's proofs are code-work practices
    if task and task.get("status") in ("review", "approved", "done"):
        for p in practices:
            if p["id"] == "agora" and p["state"] != "ok":
                p.update(state="ok", evidence=f"tarea #{task['id']} en {task['status']}")
    held = [lock["resource"] for lock in locks if lock.get("owner") == agent and lock["resource"].startswith("path:")]
    practices.append({"id": "claim", "label": "Tarea reclamada con bloqueos de ruta",
                      "state": "ok" if task and held else "pending",
                      "evidence": (f"#{task['id']} · " + ", ".join(held[:3])) if task and held else ""})
    return practices


def file_path(text: str) -> str:
    """Only explicit patch paths, quoted filenames or a bare path are file targets."""
    patch = re.search(r"\*\*\* (?:Update|Add|Delete) File:\s*([^\r\n]+)", text)
    if patch:
        return patch[1].strip()
    quoted = re.search(r'''["']([^"'\r\n]+[\\/][^"'\r\n]+\.[a-zA-Z0-9]{1,12})["']''', text)
    if quoted:
        return quoted[1]
    if re.fullmatch(r"[\w.:-]+(?:[\\/][\w. -]+)+\.[a-zA-Z0-9]{1,12}", text.strip()):
        return text.strip()
    return ""


def task_progress(task: dict[str, Any]) -> str:
    compact = task.get("checkpoint_summary") or {}
    full = task.get("latest_checkpoint") or {}
    return snippet(compact.get("summary") or (full.get("payload") or {}).get("summary") or full.get("summary"))


def task_signal_at(task: dict[str, Any]) -> float:
    checkpoint = task.get("checkpoint_summary") or task.get("latest_checkpoint") or {}
    return max(task.get("updated", 0), checkpoint.get("created", 0))


def project(board: dict[str, Any], watch: dict[str, Any], messages: list[dict[str, Any]],
            *, now: float, channel: str = "general", colors: dict[str, str] | None = None) -> dict[str, Any]:
    tasks = board.get("tasks", [])
    members = {m["id"]: m for m in board.get("agents", []) if m["id"] not in ((board.get("person") or {}).get("id"), "system")}
    # Locks are global in the board. Only this server's task locks and unscoped member locks belong here.
    task_ids = {t["id"] for t in tasks}
    locks = [lock for lock in board.get("locks", []) if lock.get("owner") in members
             and (lock.get("task_id") is None or lock["task_id"] in task_ids)]
    sessions = {s["key"]: s for s in watch.get("sessions", [])}
    files: dict[str, dict[str, Any]] = {}
    actors = []
    for lock in locks:
        resource = lock["resource"]
        if not resource.startswith("path:"):
            continue
        path = resource[5:]
        repo, _, relative = path.partition("/")
        if path in files:
            files[path]["owners"].append(lock["owner"])
        else:
            files[path] = {"id": path, "repo": repo, "path": relative or path, "source": "lock",
                           "owners": [lock["owner"]], "at": lock.get("acquired", 0), "preview": ""}
    for agent, member in members.items():
        observed = watch.get("agents", {}).get(agent, {})
        session = sessions.get(observed.get("session_key"), {})
        # An ended observed turn leaves the stage even if its heartbeat is still recent.
        if observed.get("state") == "idle":
            continue
        active = observed.get("state") in ("working", "tool", "waiting", "stale")
        if not active and (member.get("state") not in ("working", "waiting") or member.get("stale")):
            continue
        own = sorted((t for t in tasks if t.get("owner") == agent),
                     key=task_signal_at, reverse=True)
        target = {"kind": "rest", "id": agent, "at": 0}
        note = snippet(member.get("doing", ""))
        if own:
            task = own[0]
            target = {"kind": "task", "id": str(task["id"]), "at": task_signal_at(task)}
            note = task_progress(task) or note
        action = {**(session.get("last_action") or {}), **(session.get("tool") or {})}
        action_at = action.get("since", 0)
        if action_at and action_at >= target["at"]:
            text = snippet(action.get("input", ""))
            path = action.get("path") or file_path(action.get("input", ""))
            if path:
                key = path.replace("\\", "/")
                previous = files.get(key, {})
                owners = list(dict.fromkeys([*previous.get("owners", []), agent]))
                if previous.get("at", 0) <= action_at:
                    files[key] = {"id": key, "repo": session.get("workspace") or session.get("cwd") or "",
                                  "path": path, "source": "observer", "owners": owners, "at": action_at,
                                  "preview": action.get("preview") or text, "tool": action.get("name", "")}
                else:
                    previous["owners"] = owners
                target = {"kind": "file", "id": key, "at": action_at}
            elif text:
                # An observed command has its own row, without inventing a file it might touch.
                key = "command:" + agent
                files[key] = {"id": key, "repo": session.get("workspace") or session.get("cwd") or "",
                              "path": action.get("name", "Herramienta"), "source": "command", "owners": [agent],
                              "at": action_at, "preview": text, "tool": action.get("name", "")}
                target = {"kind": "file", "id": key, "at": action_at}
        for lock in locks:
            path = lock["resource"][5:]
            if lock["owner"] == agent and lock["resource"].startswith("path:") and lock.get("acquired", 0) > target["at"]:
                target = {"kind": "file", "id": path, "at": lock["acquired"]}
        for message in messages:
            if message.get("author") == agent and message.get("created", 0) >= target["at"]:
                target = {"kind": "message", "id": str(message["id"]), "at": message["created"]}
                note = snippet(message.get("body", ""))
        loop = None
        if session.get("loop"):
            loop = {**session["loop"], "practices": board_practices(session["loop"], agent, own, locks)}
            loop["practices_summary"] = agent_loop.summary(loop["practices"])
            loop["tokens"] = session.get("tokens")
            loop["turn_started_at"] = session.get("turn_started_at")
        last_signal = max(target["at"], observed.get("last_activity", 0), member.get("last_seen", 0))
        provider = next((p for p in COLORS if agent == p or agent.startswith(p + "-")), "")
        actors.append({"id": agent, "name": member.get("name") or agent,
                       "color": (colors or {}).get(provider) or COLORS.get(provider, "#9aa3b2"),
                       "state": observed.get("state") or member.get("state"), "target": target,
                       "last_signal": last_signal, "resting": now - last_signal >= 60,
                       "note": snippet(session.get("last_text") if session.get("last_text")
                                       and session.get("last_text_at", 0) >= target["at"] else note),
                       "session": session or None, "loop": loop})
    loops = {a["id"]: a["loop"] for a in actors if a["loop"]}
    return {"ok": True, "now": now, "space": board.get("space"), "channel": channel,
            "tasks": [{**{k:t.get(k) for k in ("id", "title", "status", "owner", "repo", "thread_id", "updated")},
                       "progress": task_progress(t),
                       "loop": ({k: loops[t["owner"]].get(k) for k in ("phase", "lap", "tool", "tool_title", "kind",
                                                                         "kind_label", "practices_summary", "calls",
                                                                         "calls_done", "turn_started_at")}
                                if t.get("owner") in loops and t.get("status") not in ("done", "dropped") else None)}
                      for t in tasks],
            "files": list(files.values()), "messages": messages, "actors": actors,
            "watch_error": watch.get("error") if not watch.get("ok", True) else None}
