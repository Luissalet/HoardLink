"""Read-only projection of the existing board, channel and transcript observer."""
from __future__ import annotations

import re
from typing import Any

from .agent_watch import snippet

COLORS = {"claude": "#d97757", "codex": "#10a37f", "cursor": "#7aa2f7", "faustus": "#d0545b"}
TOOL = {
    "name": "hub_agora_live", "read": True,
    "description": "Live server activity / actividad en directo de un servidor.\n"
                   "Read one server live: board, path locks, observed file/command snippets, channel messages and "
                   "one cursor target per active member. Targets carry source timestamps. No inferred movement, "
                   "file reads, model calls or read receipts. Idle after 60 seconds of silence.",
    "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "space_id": {"type": "string"}, "channel": {"type": "string", "default": "general"}}},
}


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


def project(board: dict[str, Any], watch: dict[str, Any], messages: list[dict[str, Any]],
            *, now: float, channel: str = "general", colors: dict[str, str] | None = None) -> dict[str, Any]:
    tasks = board.get("tasks", [])
    members = {m["id"]: m for m in board.get("agents", []) if m["id"] not in ("luis", "system")}
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
                     key=lambda t: t.get("updated", 0), reverse=True)
        target = {"kind": "rest", "id": agent, "at": 0}
        note = snippet(member.get("doing", ""))
        if own:
            task = own[0]
            target = {"kind": "task", "id": str(task["id"]), "at": task.get("updated", 0)}
            checkpoint = task.get("latest_checkpoint") or {}
            note = snippet(checkpoint.get("summary") or note)
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
        last_signal = max(target["at"], observed.get("last_activity", 0), member.get("last_seen", 0))
        provider = next((p for p in COLORS if agent == p or agent.startswith(p + "-")), "")
        actors.append({"id": agent, "name": member.get("name") or agent,
                       "color": (colors or {}).get(provider) or COLORS.get(provider, "#9aa3b2"),
                       "state": observed.get("state") or member.get("state"), "target": target,
                       "last_signal": last_signal, "resting": now - last_signal >= 60,
                       "note": snippet(session.get("last_text") if session.get("last_text")
                                       and session.get("last_text_at", 0) >= target["at"] else note),
                       "session": session or None})
    return {"ok": True, "now": now, "space": board.get("space"), "channel": channel,
            "tasks": [{**{k:t.get(k) for k in ("id", "title", "status", "owner", "repo", "thread_id", "updated")},
                       "progress": snippet((t.get("latest_checkpoint") or {}).get("summary") or t.get("checkpoint_summary"))}
                      for t in tasks],
            "files": list(files.values()), "messages": messages, "actors": actors,
            "watch_error": watch.get("error") if not watch.get("ok", True) else None}
