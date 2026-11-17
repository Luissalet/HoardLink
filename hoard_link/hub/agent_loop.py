"""Plan -> act -> observe of an observed agent session, read from its transcript lines.

``agent_watch.Session`` calls this tracker as it feeds lines: model text or reasoning is
*plan*, a tool call is *act* (with the tool's name) and a tool result is *observe*. The
phase changes only when a line says so; nothing moves between lines.

The tracker also keeps the steps of the current turn (plan, tools with their readable name
and a short summary of their arguments, results with ok/error and time, commit, delivery),
the commands it ran, the prompt and the answer. ``classify`` reads the kind of work the turn
did and ``judge_practices`` marks that kind's practices: code (tests run, the app checked in
a browser, docs updated, the commit identity, no evidence files committed, the delivery),
research (sources opened and cited, dated facts, nothing cited from an unseen source, the
answer in the user's language), files (output checked, requested format), operations
(commands ok and checked after running) or none for a plain conversation.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlparse

MAX_STEPS = 60
MAX_CALLS = 400
MAX_URLS = 400
EXPECTED_AUTHOR = "Luissalet"

KIND_LABELS = {"code": "Código", "research": "Investigación", "files": "Ficheros y documentos",
               "ops": "Operaciones", "chat": "Conversación"}
LABELS = {
    "tests": "Tests ejecutados",
    "browser": "App probada en navegador",
    "docs": "Documentación al día (FAUSTUS.md / README / AGENTS.md)",
    "commit": "Commit con la identidad correcta",
    "no_evidence": "Sin ficheros de evidencias en el commit",
    "agora": "Entrega en el Ágora (submit / done)",
    "sources_opened": "Fuentes abiertas, no solo buscadas",
    "sources_cited": "Fuentes citadas en la respuesta",
    "dated": "Datos con fecha comprobada",
    "verified": "Nada afirmado sin haberlo visto en una fuente",
    "language": "Respuesta en el idioma de quien pregunta",
    "output_checked": "Salida abierta y comprobada",
    "format": "En el formato pedido",
    "ran_ok": "Comandos terminados sin error",
    "checked": "Resultado comprobado tras ejecutarlo",
}
KIND_PRACTICES = {
    "code": ("tests", "browser", "docs", "commit", "no_evidence", "agora"),
    "research": ("sources_opened", "sources_cited", "dated", "verified", "language"),
    "files": ("output_checked", "format", "language"),
    "ops": ("ran_ok", "checked", "language"),
    "chat": (),
}
PRACTICES = tuple((pid, LABELS[pid]) for pid in KIND_PRACTICES["code"])

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

_GIT = re.compile(r"(?:^|[\s;&|(])git\s+\w", re.I)
_AGORA_TASK = re.compile(r"\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:claim|submit|done|review)\s+#?\d+|hub_agora_task_", re.I)
_CODE_FILE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|css|scss|vue|svelte|go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|"
                        r"php|sh|ps1|psm1|bat|cmd|sql|toml|ya?ml|json|ini|cfg|lock|gradle|swift)$", re.I)
_DOC_EXT = r"md|txt|docx?|odt|rtf|pdf|xlsx?|ods|csv|tsv|pptx?|odp|html?|png|jpe?g|webp|gif|svg|mp4|mp3|wav|epub"
_DOC_FILE = re.compile(rf"\.({_DOC_EXT})$", re.I)
_DOC_OUT = re.compile(rf"(?:\s-o\s+|--output[= ]|>\s*|--out[= ]|save(?:fig|_as)?\(\s*[\"'])([^\s\"'>]*\.(?:{_DOC_EXT}))\b", re.I)
_DOC_TOOL = re.compile(r"create_document|edit_document|update_document|generate_image|image_gen|make_slides|"
                       r"export_document|render_document|save_document|write_report", re.I)
_WEB_TOOL = re.compile(r"web_?search|web_?fetch|fetch_url|open_url|search_web|searx|browse|browser|navigate", re.I)
_FETCH_TOOL = re.compile(r"web_?fetch|fetch_url|open_url|browse|browser|navigate", re.I)
_FETCH_CMD = re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr|httpx?)\b[^\n]*https?://", re.I)
_INSPECT_TOOL = re.compile(r"^(read|read_file|view|view_file|open_file|inspect_deliverable|inspect_media|"
                           r"inspect_document|preview|look_at_image)$", re.I)
_READ_ONLY_TOOL = re.compile(r"^(read|read_file|view|view_file|ls|list|list_dir|glob|grep|search_files|find|"
                             r"lookup_tools|todowrite|todo_write|update_plan|ask_user|search_chats|"
                             r"search_project_chats|recall|manage_memory)$", re.I)
_READ_ONLY_CMD = re.compile(r"^\s*(ls|dir|cat|type|head|tail|less|more|grep|rg|find|tree|wc|stat|du|df|pwd|which|"
                            r"where|whoami|echo|env|printenv|ps|tasklist|top|uptime|free|nvidia-smi|ping|nslookup|"
                            r"Get-\w+|Test-\w+|Select-String|git\s+(?:status|log|diff|show|branch|remote)|"
                            r"docker\s+(?:ps|images|logs|inspect)|systemctl\s+status|journalctl|"
                            r"kubectl\s+(?:get|describe|logs)|curl\s+(?:-s\s+)?(?:-I\s+)?https?://)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"'`)\]}]+", re.I)
_CITE = re.compile(r"\[[^\]]+\]\(https?://|\[\d{1,2}\]|https?://", re.I)
_DATE = re.compile(
    r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b(19|20)\d{2}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}\s+(?:de\s+)?(?:ene(?:ro)?|feb(?:rero)?|mar(?:zo)?|abr(?:il)?|may(?:o)?|jun(?:io)?|jul(?:io)?|"
    r"ago(?:sto)?|sep(?:tiembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?|jan(?:uary)?|february|march|april|"
    r"june|july|aug(?:ust)?|sept?(?:ember)?|october|dec(?:ember)?)\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b|"
    r"\b(?:en|in|de|del|since|desde|as of|a)\s+(?:19|20)\d{2}\b|\b(?:19|20)\d{2}\b", re.I)
_FORMATS = {
    "pdf": ("pdf",), "docx": ("docx", "doc"), "word": ("docx", "doc"), "xlsx": ("xlsx", "xls"),
    "excel": ("xlsx", "xls"), "hoja de cálculo": ("xlsx", "xls", "ods", "csv"), "spreadsheet": ("xlsx", "xls", "ods", "csv"),
    "csv": ("csv",), "pptx": ("pptx", "ppt"), "powerpoint": ("pptx", "ppt"), "presentación": ("pptx", "ppt", "odp", "pdf"),
    "slides": ("pptx", "ppt", "odp", "pdf"), "markdown": ("md",), ".md": ("md",), "json": ("json",),
    "html": ("html", "htm"), "png": ("png",), "jpg": ("jpg", "jpeg"), "jpeg": ("jpg", "jpeg"), "svg": ("svg",),
    "txt": ("txt",), "epub": ("epub",),
}
_STOP_ES = frozenset(
    "de la que el en y los se del las un por con una su para es al lo como más pero sus le ya este sí porque esta "
    "entre cuando muy sin sobre también hasta hay donde quien desde todo nos durante todos uno les ni contra otros "
    "ese eso ante ellos esto antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos "
    "cual poco ella estar estas algunas algo nosotros mi mis tú te ti tu tus ellas cómo dónde cuál puedes hazme "
    "dime busca buscar quiero necesito hoy está son fue ha han".split())
_STOP_EN = frozenset(
    "the of and to in is that it for on with as was are be this by at or from have an not but what all were when we "
    "there can your which their if will about how out up them then so some would make like into has more could than "
    "been who its now did get may please find show tell me give today is are does do you".split())
_WORD = re.compile(r"[a-záéíóúñü']+", re.I)

TOOL_TITLES = {
    "bash": "Terminal", "shell": "Terminal", "powershell": "Terminal", "exec": "Terminal", "exec_command": "Terminal",
    "local_shell": "Terminal", "shell_command": "Terminal", "python": "Python", "read": "Leer", "read_file": "Leer",
    "write": "Escribir", "write_file": "Escribir", "edit": "Editar", "edit_file": "Editar", "multiedit": "Editar",
    "notebookedit": "Editar cuaderno", "apply_patch": "Parche", "ls": "Listar", "glob": "Buscar ficheros",
    "grep": "Buscar en ficheros", "web_search": "Buscar en la web", "websearch": "Buscar en la web",
    "web_fetch": "Abrir URL", "webfetch": "Abrir URL", "fetch_url": "Abrir URL", "browser": "Navegador",
    "task": "Delegar", "agent": "Delegar", "todowrite": "Tareas", "update_plan": "Plan",
    "askuserquestion": "Preguntar", "ask_user": "Preguntar", "skill": "Skill", "view_image": "Ver imagen",
}
_ARG_KEYS = ("query", "q", "url", "file_path", "path", "filename", "pattern", "command", "cmd", "title", "prompt",
             "description", "name", "task", "text", "input")


def _brief(value: Any, limit: int = 140) -> str:
    """Labels and evidence go through the observer's own masking: secret-looking lines dropped, the rest masked."""
    from .agent_watch import snippet     # agent_watch imports this module
    return snippet(str(value or ""), limit)


def _parse(args: Any) -> Any:
    if isinstance(args, str) and args.lstrip()[:1] in "{[":
        try:
            return json.loads(args)
        except ValueError:
            pass
    return args


def call_text(args: Any, limit: int = 1500) -> str:
    """The command, or else the edited path, of a tool call: what the practices are judged on."""
    args = _parse(args)
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


def tool_title(tool: str) -> str:
    """The words a person reads for a tool: «Buscar en la web», «Terminal»; ``mcp__x__name`` reads as its name."""
    low = (tool or "").lower()
    if low.startswith("mcp__"):
        low = low.rsplit("__", 1)[-1]
    if low in TOOL_TITLES:
        return TOOL_TITLES[low]
    if low.startswith("browser_"):
        return "Navegador · " + low[len("browser_"):].replace("_", " ")
    return (low or "herramienta").replace("_", " ")


def args_summary(args: Any, limit: int = 96) -> str:
    """The one argument that says what a call does (its query, URL, path or command), never raw JSON."""
    args = _parse(args)
    if isinstance(args, dict):
        for key in _ARG_KEYS:
            value = args.get(key)
            if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
                value = " ".join(value)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return _brief(str(value).strip().splitlines()[0], limit)
        first = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
        return _brief(first.strip().splitlines()[0] if first else "", limit)
    if isinstance(args, list):
        return _brief(" ".join(map(str, args)), limit)
    text = str(args or "").strip()
    return _brief(text.splitlines()[0] if text else "", limit)


def _target(command: str) -> str:
    m = re.search(r"\*\*\* (?:Update|Add) File:\s*(\S+)", command or "")
    if m:
        return m.group(1)
    first = (command or "").strip().splitlines()[0].strip() if (command or "").strip() else ""
    return first if first and " " not in first else ""


def _ended(call: dict[str, Any]) -> bool:
    return bool(call.get("ended", call.get("ok") is not None))


def _writes(call: dict[str, Any]) -> tuple[str, str]:
    tool = str(call.get("tool") or "")
    command = str(call.get("command") or "")
    if _DOC_TOOL.search(tool):
        return "doc", _target(command) or tool
    if _WRITE_TOOL.search(tool) and not _READ_ONLY_TOOL.match(tool):
        path = _target(command)
        return ("doc" if _DOC_FILE.search(path) else "code"), path
    out = _DOC_OUT.search(command)
    return ("doc", out.group(1)) if out else ("", "")


def classify(calls: list[dict[str, Any]]) -> str:
    """code > files > research > ops by what the turn did; chat when it used no tool or only read."""
    kinds = set()
    for call in calls:
        tool = str(call.get("tool") or "")
        command = str(call.get("command") or "")
        wrote, path = _writes(call)
        if (wrote == "code" or (wrote == "doc" and _DOCS.search(path)) or _GIT.search(command)
                or _TESTS.search(command) or _AGORA_TASK.search(f"{tool} {command}")):
            kinds.add("code")
        elif wrote == "doc":
            kinds.add("files")
        elif _WEB_TOOL.search(tool) or _FETCH_CMD.search(command):
            kinds.add("research")
        elif not _READ_ONLY_TOOL.match(tool):
            kinds.add("ops")
    return next((k for k in ("code", "files", "research", "ops") if k in kinds), "chat")


def language(text: str) -> Optional[str]:
    words = [w.lower() for w in _WORD.findall(text or "")[:400]]
    es = sum(w in _STOP_ES for w in words)
    en = sum(w in _STOP_EN for w in words)
    if es - en >= 2 or (es >= 1 and en == 0 and len(words) >= 3):
        return "es"
    if en - es >= 2 or (en >= 1 and es == 0 and len(words) >= 3):
        return "en"
    return None


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _code_marks(calls: list[dict[str, Any]], author: str) -> dict[str, dict[str, Any]]:
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
        ok = call.get("ok")
        if _TESTS.search(command) and _ended(call):
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
    return found


def _research_marks(calls: list[dict[str, Any]], answer: str, seen_urls: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    opened = next((c for c in calls if c.get("ok") is not False and _ended(c)
                   and (_FETCH_TOOL.search(str(c.get("tool") or "")) or _FETCH_CMD.search(str(c.get("command") or "")))),
                  None)
    if opened:
        found["sources_opened"] = {"state": "ok", "evidence": _brief(opened.get("summary") or opened.get("command"))}
    cited = [u.rstrip(".,;:") for u in _URL.findall(answer or "")]
    cite = _CITE.search(answer or "")
    if cite:
        found["sources_cited"] = {"state": "ok", "evidence": _brief(cited[0] if cited else cite.group(0))}
    date = _DATE.search(answer or "")
    if date:
        found["dated"] = {"state": "ok", "evidence": _brief(answer[max(0, date.start() - 40): date.end() + 20])}
    if cited:
        known = {_domain(u) for u in seen_urls} | {_domain(u) for c in calls for u in _URL.findall(str(c.get("command") or ""))}
        unseen = [u for u in cited if _domain(u) and _domain(u) not in known]
        found["verified"] = ({"state": "fail", "evidence": _brief("citado sin abrirlo ni aparecer en los resultados: "
                                                                   + ", ".join(unseen[:3]))}
                             if unseen else {"state": "ok", "evidence": f"{len(cited)} enlaces citados vistos en los resultados"})
    else:
        found["verified"] = {"state": "unknown", "evidence": "la respuesta no tiene enlaces que contrastar"}
    return found


def _files_marks(calls: list[dict[str, Any]], prompt: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    found: dict[str, dict[str, Any]] = {}
    skip: set[str] = set()
    outputs = [(i, path) for i, call in enumerate(calls) for wrote, path in [_writes(call)]
               if wrote and call.get("ok") is not False]
    if outputs:
        names = {re.split(r"[\\/]", p)[-1].lower() for _, p in outputs if p}
        check = next((c for c in calls[outputs[-1][0] + 1:] if c.get("ok") is not False and _ended(c) and (
            _INSPECT_TOOL.match(str(c.get("tool") or ""))
            or any(n and n in str(c.get("command") or "").lower() for n in names))), None)
        if check:
            found["output_checked"] = {"state": "ok", "evidence": _brief(
                f"{tool_title(str(check.get('tool') or ''))}: {check.get('summary') or check.get('command') or ''}")}
    low = (prompt or "").lower()
    wanted = sorted({ext for word, exts in _FORMATS.items() if re.search(rf"(?<![\w.]){re.escape(word)}\b", low) for ext in exts})
    if not wanted:
        skip.add("format")
    elif outputs:
        exts = {p.rsplit(".", 1)[-1].lower() for _, p in outputs if "." in p}
        hit = sorted(exts & set(wanted))
        found["format"] = ({"state": "ok", "evidence": "." + ", .".join(hit)} if hit else
                           {"state": "fail", "evidence": _brief(f"se pidió .{', .'.join(wanted)}; se escribió "
                                                                + ", ".join(p for _, p in outputs if p))})
    return found, skip


def _ops_marks(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    ended = [c for c in calls if _ended(c)]
    if ended:
        last = ended[-1]
        failed = sum(c.get("ok") is False for c in ended)
        what = _brief(last.get("summary") or last.get("command") or tool_title(str(last.get("tool") or "")))
        state = "fail" if last.get("ok") is False else "ok" if last.get("ok") else "unknown"
        found["ran_ok"] = {"state": state, "evidence": what + (f" (tras {failed} fallidos)" if failed and state == "ok" else "")}
    acting = [i for i, c in enumerate(calls) if not _READ_ONLY_TOOL.match(str(c.get("tool") or ""))
              and not _READ_ONLY_CMD.match(str(c.get("command") or ""))]
    if not acting and ended:
        found["checked"] = {"state": "ok", "evidence": "solo comandos de consulta"}
    elif acting:
        check = next((c for c in calls[acting[-1] + 1:] if c.get("ok") is not False and _ended(c)), None)
        if check:
            found["checked"] = {"state": "ok", "evidence": _brief(check.get("summary") or check.get("command")
                                                                  or tool_title(str(check.get("tool") or "")))}
    return found


def judge_practices(calls: list[dict[str, Any]], *, author: str = EXPECTED_AUTHOR, answer: str = "", prompt: str = "",
                    kind: Optional[str] = None, seen_urls: Optional[list[str]] = None,
                    final: bool = False) -> list[dict[str, Any]]:
    """The practices of the turn's kind of work. ``ok`` with what proves it, ``fail`` with what breaks it,
    ``unknown`` when the evidence does not settle it (a result the transcript does not grade, a commit with
    no explicit author) and ``pending`` when nothing seen touches it. What does not apply is left out."""
    kind = kind or classify(calls)
    skip: set[str] = set()
    if kind == "code":
        found = _code_marks(calls, author)
    elif kind == "research":
        found = _research_marks(calls, answer, seen_urls or [])
    elif kind == "files":
        found, skip = _files_marks(calls, prompt)
    elif kind == "ops":
        found = _ops_marks(calls)
    else:
        found = {}
    if "language" in KIND_PRACTICES[kind]:
        asked = language(prompt)
        if asked is None:
            skip.add("language")
        elif (answer or "").strip():
            said = language(answer)
            found["language"] = ({"state": "unknown", "evidence": "la respuesta no lo deja claro"} if said is None else
                                 {"state": "ok" if said == asked else "fail",
                                  "evidence": f"pregunta en {asked}, respuesta en {said}"})
        elif final:
            found["language"] = {"state": "unknown", "evidence": "sin respuesta escrita"}
    return [{"id": pid, "label": LABELS[pid], **found.get(pid, {"state": "pending", "evidence": ""})}
            for pid in KIND_PRACTICES[kind] if pid not in skip]


def summary(practices: list[dict[str, Any]]) -> dict[str, int]:
    return {"ok": sum(p["state"] == "ok" for p in practices),
            "fail": sum(p["state"] == "fail" for p in practices),
            "open": sum(p["state"] in ("pending", "unknown") for p in practices)}


def _secs(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 10 else f"{round(seconds)} s"


class Loop:
    """The loop of one observed session. Turn markers reset the steps, prompt and answer; the commands are kept
    for the session, and the kind of work is read from the current turn's calls."""

    def __init__(self) -> None:
        self.phase = "idle"
        self.since = 0.0
        self.lap = 0
        self.tool = ""
        self.prompt = ""
        self.answer = ""
        self.urls: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.turn_from = 0                      # index in ``calls`` where the current turn's calls start
        self._ids: dict[str, dict[str, Any]] = {}

    def _step(self, kind: str, label: str, ts: float, **extra: Any) -> None:
        self.steps.append({"kind": kind, "label": _brief(label), "at": ts, "lap": max(1, self.lap), **extra})
        if len(self.steps) > MAX_STEPS:
            del self.steps[0]

    def _enter(self, phase: str, ts: float) -> None:
        if phase != self.phase:
            self.phase = phase
            self.since = ts

    def turn_start(self, ts: float, prompt: str = "") -> None:
        self.steps = []
        self.lap = 1
        self.tool = ""
        self.phase = "plan"
        self.since = ts
        self.prompt = (prompt or "")[:4000]
        self.answer = ""
        self.urls = []
        self.turn_from = len(self.calls)

    def turn_end(self, ts: float) -> None:
        self.tool = ""
        if self.steps:
            self._step("end", "fin del turno", ts)
        self._enter("done", ts)

    def _next_lap(self) -> None:
        """A new lap starts when the agent plans or acts again after observing a result."""
        if self.phase == "observe" or not self.lap:
            self.lap += 1

    def text(self, ts: float, text: str = "") -> None:
        """Model text or reasoning. After the turn ended it is the closing answer, not a new plan."""
        if text and text not in self.answer and len(self.answer) < 20000:     # Codex logs each answer twice
            self.answer = (self.answer + "\n" + text).strip()
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
        title, detail = tool_title(name), args_summary(args)
        record = {"tool": name or "tool", "command": command, "summary": detail, "ok": None, "ended": False, "at": ts}
        self.calls.append(record)
        if len(self.calls) > MAX_CALLS:
            del self.calls[0]
            self.turn_from = max(0, self.turn_from - 1)
        if call_id:
            self._ids[call_id] = record
            while len(self._ids) > MAX_CALLS:
                self._ids.pop(next(iter(self._ids)))
        self.tool = name or "tool"
        self._step("tool", f"{title}: {detail}" if detail else title, ts, tool=self.tool, title=title, detail=detail)
        self._enter("act", ts)

    def tool_end(self, call_id: str, ts: float, ok: Optional[bool] = None, output: str = "") -> None:
        record = self._ids.pop(call_id, None)
        if record is None:
            return
        record["ended"], record["ok"] = True, ok
        if output and len(self.urls) < MAX_URLS:
            self.urls.extend(_URL.findall(output[:20000])[: MAX_URLS - len(self.urls)])
        took = max(0.0, ts - float(record.get("at") or ts))
        title = tool_title(record["tool"])
        verdict = "terminada" if ok is None else "ok" if ok else "con error"
        self._step("result", f"{title}: {verdict} · {_secs(took)}", ts, tool=record["tool"], title=title, ok=ok,
                   ms=round(took * 1000))
        if ok is not False and _COMMIT.search(record["command"]):
            self._step("commit", record["command"], ts)
        if ok is not False and _DELIVERY.search(f"{record['tool']} {record['command']}"):
            self._step("submit", record["command"], ts)
        self.tool = ""
        self._enter("observe", ts)

    def view(self) -> dict[str, Any]:
        turn = self.calls[self.turn_from:]
        kind = classify(turn)
        # Code is judged on the session's commands: a commit or a delivery often comes turns after the tests.
        practices = judge_practices(self.calls if kind == "code" else turn, answer=self.answer, prompt=self.prompt,
                                    kind=kind, seen_urls=self.urls, final=self.phase == "done")
        return {"phase": self.phase, "since": self.since, "lap": self.lap, "tool": self.tool or None,
                "tool_title": tool_title(self.tool) if self.tool else None, "kind": kind,
                "kind_label": KIND_LABELS[kind], "steps": list(self.steps), "practices": practices,
                "practices_summary": summary(practices)}
