"""Plan -> act -> observe of an observed agent session, read from its transcript lines.

``agent_watch.Session`` calls this tracker as it feeds lines: model text or reasoning is
*plan*, a tool call is *act* (with the tool's name) and a tool result is *observe*. The
phase changes only when a line says so; nothing moves between lines.

The tracker also keeps the steps of the current turn (plan, tools, results, commit,
delivery) and the commands it ran, from which ``judge_practices`` marks the work
practices of the common Agora criteria: tests run, the app checked in a browser,
docs updated, the commit identity, no evidence files committed, and the delivery.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

MAX_STEPS = 60
MAX_CALLS = 400
EXPECTED_AUTHOR = "Luissalet"

PRACTICES = (
    ("tests", "Tests ejecutados"),
    ("browser", "App probada en navegador"),
    ("docs", "Documentación al día (FAUSTUS.md / README / AGENTS.md)"),
    ("commit", "Commit con la identidad correcta"),
    ("no_evidence", "Sin ficheros de evidencias en el commit"),
    ("agora", "Entrega en el Ágora (submit / done)"),
)

_TESTS = re.compile(r"\b(pytest|py\.test|unittest|vitest|jest|tox|(?:npm|pnpm|yarn)(?: run)? (?:test|check)|"
                    r"go test|cargo test|node --test)\b", re.I)
_BROWSER = re.compile(r"playwright|puppeteer|chromium|headless|browser", re.I)
_DOCS = re.compile(r"\b(FAUSTUS|README(?:\.es)?|AGENTS)\.md\b", re.I)
_WRITE_TOOL = re.compile(r"write|edit|patch|replace|create_file|str_replace", re.I)
_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b", re.I)
_ADD = re.compile(r"\bgit\b[^\n|;&]*\badd\b", re.I)
_AUTHOR = re.compile(r"--author[= ]+[\"']?([^<\"']+)|user\.name=[\"']?([^\"'\s]+)|GIT_AUTHOR_NAME=[\"']?([^\"'\s]+)")
_TRAILER = re.compile(r"Co-Authored-By:", re.I)
_EVIDENCE = re.compile(r"\.(png|jpe?g|webp|gif|mp4|webm|har)\b|evidenc|screenshot|captura|qa[-_][\w-]*\.json", re.I)
_DELIVERY = re.compile(r"\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:submit|done)\s+#?\d+|hub_agora_(?:task_)?(?:submit|done)", re.I)


def _brief(value: Any, limit: int = 140) -> str:
    """Labels and evidence go through the observer's own masking: secret-looking lines dropped, the rest masked."""
    from .agent_watch import snippet     # agent_watch imports this module
    return snippet(str(value or ""), limit)


def call_text(args: Any, limit: int = 1500) -> str:
    """The command, or else the edited path, of a tool call: what the practices are judged on."""
    if isinstance(args, str) and args.lstrip()[:1] in "{[":
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        for key in ("command", "cmd", "file_path", "path", "url", "input"):
            value = args.get(key)
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                value = " ".join(value)
            if isinstance(value, str) and value.strip():
                return value[:limit]
        return ""
    if isinstance(args, list) and all(isinstance(x, str) for x in args):
        return " ".join(args)[:limit]
    return str(args or "")[:limit]


def judge_practices(calls: list[dict[str, Any]], *, author: str = EXPECTED_AUTHOR) -> list[dict[str, Any]]:
    """``ok`` with the call that proves it, ``fail`` with the call that breaks it, ``unknown`` when the
    evidence does not settle it (a result the transcript does not grade, a commit with no explicit
    author) and ``pending`` when nothing seen touches it."""
    found: dict[str, dict[str, Any]] = {}

    def mark(pid: str, state: str, evidence: str) -> None:
        if found.get(pid, {}).get("state") == "fail" and state != "fail":
            return
        if found.get(pid, {}).get("state") == "ok" and state == "unknown":
            return
        found[pid] = {"state": state, "evidence": _brief(evidence)}

    committed = False
    for call in calls:
        tool = str(call.get("tool") or "")
        command = str(call.get("command") or "")
        text = f"{tool} {command}"
        ended, ok = call.get("ended", True), call.get("ok")
        if _TESTS.search(command) and ended:
            mark("tests", "unknown" if ok is None else "ok" if ok else "fail", command)
        if _BROWSER.search(text) and ok is not False:
            mark("browser", "ok", text)
        if _DOCS.search(command) and (_WRITE_TOOL.search(tool) or _ADD.search(command) or _COMMIT.search(command)):
            mark("docs", "ok", text)
        if (_ADD.search(command) or _COMMIT.search(command)) and _EVIDENCE.search(command):
            mark("no_evidence", "fail", command)
        if _COMMIT.search(command) and ok is not False:
            committed = True
            who = next((g for m in _AUTHOR.finditer(command) for g in m.groups() if g), "").strip()
            if _TRAILER.search(command):
                mark("commit", "fail", "trailer en el mensaje: " + command)
            elif who:
                mark("commit", "ok" if who.lower().startswith(author.lower()) else "fail", "autor " + who)
            else:
                mark("commit", "unknown", "sin autor explícito (identidad del repo): " + command)
        if _DELIVERY.search(text) and ok is not False:
            mark("agora", "ok", text)
    if committed and "no_evidence" not in found:
        mark("no_evidence", "ok", "ninguna imagen, captura ni JSON de QA añadidos")
    return [{"id": pid, "label": label, **found.get(pid, {"state": "pending", "evidence": ""})}
            for pid, label in PRACTICES]


def summary(practices: list[dict[str, Any]]) -> dict[str, int]:
    return {"ok": sum(p["state"] == "ok" for p in practices),
            "fail": sum(p["state"] == "fail" for p in practices),
            "open": sum(p["state"] in ("pending", "unknown") for p in practices)}


class Loop:
    """The loop of one observed session. Turn markers reset the steps, the commands are kept for the session."""

    def __init__(self) -> None:
        self.phase = "idle"
        self.since = 0.0
        self.lap = 0
        self.tool = ""
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._ids: dict[str, dict[str, Any]] = {}

    def _step(self, kind: str, label: str, ts: float, **extra: Any) -> None:
        self.steps.append({"kind": kind, "label": _brief(label), "at": ts, "lap": max(1, self.lap), **extra})
        if len(self.steps) > MAX_STEPS:
            del self.steps[0]

    def _enter(self, phase: str, ts: float) -> None:
        if phase != self.phase:
            self.phase = phase
            self.since = ts

    def turn_start(self, ts: float) -> None:
        self.steps = []
        self.lap = 1
        self.tool = ""
        self.phase = "plan"
        self.since = ts

    def turn_end(self, ts: float) -> None:
        self.tool = ""
        if self.steps:
            self._step("end", "fin del turno", ts)
        self._enter("done", ts)

    def _next_lap(self) -> None:
        """A new lap starts when the agent plans or acts again after observing a result."""
        if self.phase == "observe" or not self.lap:
            self.lap += 1

    def text(self, ts: float) -> None:
        """Model text or reasoning. After the turn ended it is the closing answer, not a new plan."""
        if self.phase == "done":
            return
        self._next_lap()
        if self.phase != "plan" or not self.steps or self.steps[-1]["kind"] != "plan":
            self._step("plan", "razona", ts)
        self._enter("plan", ts)

    def tool_start(self, call_id: str, name: str, args: Any, ts: float) -> None:
        if self.phase == "done":
            self.turn_start(ts)        # a reply with no prompt line before it opens a new turn
        self._next_lap()
        command = call_text(args)
        record = {"tool": name or "tool", "command": command, "ok": None, "ended": False}
        self.calls.append(record)
        if len(self.calls) > MAX_CALLS:
            del self.calls[0]
        if call_id:
            self._ids[call_id] = record
            while len(self._ids) > MAX_CALLS:
                self._ids.pop(next(iter(self._ids)))
        self.tool = name or "tool"
        self._step("tool", f"{self.tool} {command}", ts, tool=self.tool)
        self._enter("act", ts)

    def tool_end(self, call_id: str, ts: float, ok: Optional[bool] = None) -> None:
        record = self._ids.pop(call_id, None)
        if record is None:
            return
        record["ended"], record["ok"] = True, ok
        self._step("result", record["tool"] + ("" if ok is None else " ok" if ok else " con error"), ts,
                   tool=record["tool"], ok=ok)
        if ok is not False and _COMMIT.search(record["command"]):
            self._step("commit", record["command"], ts)
        if ok is not False and _DELIVERY.search(f"{record['tool']} {record['command']}"):
            self._step("submit", record["command"], ts)
        self.tool = ""
        self._enter("observe", ts)

    def view(self) -> dict[str, Any]:
        practices = judge_practices(self.calls)
        return {"phase": self.phase, "since": self.since, "lap": self.lap, "tool": self.tool or None,
                "steps": list(self.steps), "practices": practices, "practices_summary": summary(practices)}
