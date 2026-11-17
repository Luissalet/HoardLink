"""Ágora router: who answers a message of the person that names nobody.

A message the person writes in a channel or a thread without ``@``-naming anyone used to be heard by no agent.
With the router on (it is, per server, until someone turns it off) the Hub picks who should answer, up to
``max_agents`` of them, or nobody:

1. **Candidates, by code.** Every agent of the server that is around and has quota is scored from what the Hub
   already knows: the owner (and reviewer) of the thread's task, the owner of a ``#task`` the text names, an agent
   named by its id or provider, whoever works on related tasks or holds related locks, whose role note or
   current work matches the words of the message, the last to speak in the thread and whoever talked about the
   same subject lately. Each score carries its reasons.
2. **The decision.** With more than one candidate a small model (the family's, through Hoard Link, so the
   Sparks) chooses among them and says why; it can only pick from the list and may pick nobody. Without a model,
   when the model fails or when the server's setting is ``rules``, simple rules choose: whoever scores at least
   half of the best one.
3. **Delivery.** The chosen are added to the message's mentions, so the message reaches their inbox, their
   console and the watchers of the sessions started from the Ágora, like any mention. The message carries a
   ``route`` that says who was chosen, why and by what (the model or the rules), and a pass is possible:
   an agent that should not answer says so (``route_pass``) and the router tries the next candidate.

Settings per server (``route_settings_set``): ``enabled``, ``max_agents`` and ``model`` (``auto``: the family's
model; ``rules``: never ask a model). ``route`` simulates (and, with ``message_id`` and ``apply``, chooses again for a
message already written).

Only what the person writes is routed: an agent that names nobody is not answered by a chain of agents.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable, Optional

DEFAULT_MAX = 3
HARD_MAX = 5
MODELS = ("auto", "rules")
#: A candidate below this is not worth waking.
MIN_SCORE = 25
#: The rules choose whoever reaches this share of the best score.
SHARE_OF_BEST = 0.5
#: How many candidates the model sees.
MODEL_CANDIDATES = 8
#: Seconds the person's message may wait for the model before the rules decide.
MODEL_TIMEOUT_S = 15.0
RECENT_MESSAGES = 250
RECENT_WINDOW_S = 3 * 24 * 3600.0
_SKIP_PRESENCE = ("offline", "invited", "no_quota", "person")
_SKIP_IDS = ("hub", "ui", "system", "luis")

ROUTER_MIGRATION = """
    CREATE TABLE agora_router_settings (space_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
        max_agents INTEGER NOT NULL DEFAULT 3, model TEXT NOT NULL DEFAULT 'auto', updated REAL NOT NULL,
        updated_by TEXT NOT NULL DEFAULT '');
    CREATE TABLE agora_routes (message_id INTEGER PRIMARY KEY, space_id TEXT NOT NULL, thread_id INTEGER NOT NULL,
        picks TEXT NOT NULL DEFAULT '[]', ranking TEXT NOT NULL DEFAULT '[]', engine TEXT NOT NULL DEFAULT 'rules',
        note TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX agora_routes_space ON agora_routes(space_id, message_id);
"""

_STOP = frozenset("""
    para como pero esto esta este estos estas esos esas esa ese eso sobre entre desde hasta cuando donde porque
    tambien puede pueden podria hacer hace haces hacen tiene tienen tengo todo todos toda todas algo alguien nadie
    sino bien mejor mas menos muy cada otro otra otros otras unos unas cual cuales quien quienes ahora luego
    antes despues ya hay solo mismo misma favor gracias hola vale seria sera esta estan estoy estamos
    that this with from have will would could should about there their they them then than what when where which
    while into over also more some just like make need needs want please thanks thank hello
""".split())


def _ag() -> Any:
    from . import agora
    return agora


def _err(message: str, status: int = 400) -> Exception:
    return _ag().AgoraError(message, status)


def _plain(text: Any) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()


def tokens(text: Any) -> set[str]:
    """The words of a text worth matching: lowercase ASCII, four letters or more, no filler."""
    return {t for t in re.findall(r"[a-z0-9]+", _plain(text)) if len(t) >= 4 and t not in _STOP and not t.isdigit()}


# ---- settings -----------------------------------------------------------------------------------------------------

def _defaults(sid: str) -> dict[str, Any]:
    return {"space_id": sid, "enabled": True, "max_agents": DEFAULT_MAX, "model": "auto", "updated": None, "updated_by": ""}


def settings_of(self: Any, sid: Optional[str] = None) -> dict[str, Any]:
    sid = sid or self.space_id
    row = self.db.one("SELECT * FROM agora_router_settings WHERE space_id=?", (sid,))
    if row is None:
        return _defaults(sid)
    return {"space_id": sid, "enabled": bool(row["enabled"]), "max_agents": int(row["max_agents"]),
            "model": row["model"] if row["model"] in MODELS else "auto", "updated": row["updated"],
            "updated_by": row["updated_by"]}


def route_settings(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """How the router is set on this server."""
    return {"ok": True, "settings": settings_of(self), "models": list(MODELS)}


def route_settings_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Turn the router on or off for this server, set how many agents it may choose (1 to 5) and whether it may ask a
    model (``auto``) or decides by rules alone (``rules``). Whatever is not given stays."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    now = settings_of(self)
    if a.get("enabled") is not None:
        if not isinstance(a["enabled"], bool):
            raise _err("enabled must be true or false")
        now["enabled"] = a["enabled"]
    if a.get("max_agents") not in (None, ""):
        try:
            value = int(a["max_agents"])
        except (TypeError, ValueError):
            raise _err("max_agents must be a number from 1 to %d" % HARD_MAX) from None
        if not 1 <= value <= HARD_MAX:
            raise _err("max_agents must be a number from 1 to %d" % HARD_MAX)
        now["max_agents"] = value
    if a.get("model") not in (None, ""):
        model = str(a["model"]).strip().lower()
        if model not in MODELS:
            raise _err("model must be one of: " + ", ".join(MODELS))
        now["model"] = model
    with self.db.tx():
        self._touch(agent)
        self.db.execute(
            "INSERT INTO agora_router_settings(space_id, enabled, max_agents, model, updated, updated_by) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(space_id) DO UPDATE SET enabled=excluded.enabled, "
            "max_agents=excluded.max_agents, model=excluded.model, updated=excluded.updated, "
            "updated_by=excluded.updated_by", (self.space_id, int(now["enabled"]), now["max_agents"], now["model"],
                                                self.clock(), agent))
    self._bump("agora.route.settings", {"space_id": self.space_id, "by": agent})
    return {"ok": True, "settings": settings_of(self), "models": list(MODELS), "by": agent}


# ---- candidates ---------------------------------------------------------------------------------------------------

def _thread(self: Any, thread_id: Optional[int]) -> Optional[dict[str, Any]]:
    if not thread_id:
        return None
    row = self.db.one("SELECT * FROM threads WHERE id=? AND space_id=?", (int(thread_id), self.space_id))
    return dict(row) if row else None


def _task_of(self: Any, task_id: Any) -> Optional[dict[str, Any]]:
    if not task_id:
        return None
    row = self.db.one("SELECT * FROM tasks WHERE id=? AND space_id=?", (int(task_id), self.space_id))
    return dict(row) if row else None


def candidates(self: Any, text: str, thread_id: Optional[int] = None, author: str = "", exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The agents that could answer ``text`` said in ``thread_id``, best first, each with its score and reasons.
    Only those around with quota are listed (the owner of the thread's task also when it is away)."""
    ag = _ag()
    from . import agora_channels, agora_social
    now = self.clock()
    members = agora_channels._members(self, agora_social._in_hand(self))
    thread = _thread(self, thread_id)
    task = _task_of(self, thread.get("task_id")) if thread else None
    words = tokens(text)
    plain = _plain(text)
    named_tasks = {int(n) for n in re.findall(r"(?<![\w/])#(\d{1,7})\b", str(text or ""))}
    named_owners = {}
    for tid in named_tasks:
        t = _task_of(self, tid)
        if t and t.get("owner") and t["status"] not in ("done", "dropped"):
            named_owners[t["owner"]] = tid
    locks: dict[str, list[str]] = {}
    for r in self.db.query("SELECT owner, resource FROM locks WHERE expires>?", (now,)):
        locks.setdefault(r["owner"], []).append(str(r["resource"]))
    last_in_thread: Optional[str] = None
    if thread:
        for r in self.db.query("SELECT author FROM messages WHERE thread_id=? AND kind<>'system' ORDER BY id DESC LIMIT 12",
                               (int(thread["id"]),)):
            if r["author"] not in (None, "system", ag.PERSON) and last_in_thread is None:
                last_in_thread = r["author"]
    spoke: dict[str, set[str]] = {}
    if words:
        for r in self.db.query(
                "SELECT m.author, m.body FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.space_id=? AND "
                "m.created>? AND m.kind<>'system' ORDER BY m.id DESC LIMIT ?", (self.space_id, now - RECENT_WINDOW_S, RECENT_MESSAGES)):
            if r["author"] in (None, "system", ag.PERSON):
                continue
            hit = words & tokens(r["body"])
            if hit:
                spoke.setdefault(r["author"], set()).update(hit)
    pool = [m for m in members if m["id"] not in _SKIP_IDS and m["id"] != author and m["id"] not in exclude
            and m.get("kind") != "human"]
    out: list[dict[str, Any]] = []
    for m in pool:
        ident = m["id"]
        is_owner = bool(task and task.get("owner") == ident)
        if m["presence"] == "no_quota" or m["presence"] == "person":
            continue
        if m["presence"] in _SKIP_PRESENCE and not (is_owner and m["presence"] != "invited"):
            continue
        score = 0
        why: list[str] = []
        if is_owner:
            score += 100
            why.append(f"dueño de la tarea #{task['id']} de este hilo")
        elif task and task.get("reviewer") == ident:
            score += 45
            why.append(f"revisor de la tarea #{task['id']}")
        if ident in named_owners:
            score += 90
            why.append(f"dueño de la tarea #{named_owners[ident]} que nombras")
        if re.search(r"(?<![a-z0-9_.\-])" + re.escape(ident) + r"(?![a-z0-9_.\-])", plain):
            score += 80
            why.append("lo nombras")
        else:
            provider = ident.split("-", 1)[0]
            if provider in ("claude", "codex", "cursor", "faustus") and re.search(r"\b" + provider + r"\b", plain):
                score += 25
                why.append(f"nombras a {provider}")
        mine = [t for t in m.get("tasks") or []]
        shown: list[str] = []
        for t in mine:
            row = _task_of(self, t["id"]) or {}
            toks = tokens(" ".join(str(row.get(k) or "") for k in ("title", "repo", "paths", "labels", "body"))[:1500])
            hit = words & toks
            if hit:
                shown.append(f"#{t['id']}")
                score += min(30, 15 * len(hit))
        if shown:
            why.append("trabaja en " + ", ".join(shown[:3]) + " (tema relacionado)")
        held = locks.get(ident, [])
        lock_tokens = set().union(*(tokens(r) for r in held)) if held else set()
        lock_hit = words & lock_tokens
        if lock_hit:
            score += min(40, 20 * len(lock_hit))
            why.append("tiene bloqueado «" + sorted(lock_hit)[0] + "»")
        role = tokens(" ".join([ident.replace("-", " "), str(m.get("note") or ""), str(m.get("doing") or "")]))
        role_hit = words & role
        if role_hit:
            score += min(30, 10 * len(role_hit))
            why.append("su rol o su trabajo actual encaja: " + ", ".join(sorted(role_hit)[:3]))
        if ident == last_in_thread:
            score += 30
            why.append("fue el último en hablar en este hilo")
        talked = spoke.get(ident)
        if talked:
            score += min(30, 15 * len(talked))
            why.append("habló hace poco de " + ", ".join(sorted(talked)[:3]))
        if score > 0:
            score += {"working": 10, "idle": 5, "waiting": 5, "away": -10}.get(m["state"] if m["presence"] != "away" else "away", 0)
        out.append({"agent": ident, "score": score, "reasons": why, "presence": m["presence"],
                    "doing": str(m.get("doing") or "")[:120], "provider": m.get("role"), "rank": m.get("rank")})
    out = [c for c in out if c["score"] > 0]
    out.sort(key=lambda c: (-c["score"], c["agent"]))
    return out


# ---- decision -----------------------------------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {
    "picks": {"type": "array", "items": {"type": "object", "properties": {
        "agent": {"type": "string"}, "why": {"type": "string"}}, "required": ["agent"]}},
    "nobody": {"type": "boolean"}}, "required": ["picks"]}


def _by_rules(ranking: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    eligible = [c for c in ranking if c["score"] >= MIN_SCORE]
    if not eligible:
        return []
    floor = max(MIN_SCORE, eligible[0]["score"] * SHARE_OF_BEST)
    return [{"agent": c["agent"], "why": "; ".join(c["reasons"][:2]) or "el que mejor encaja"}
            for c in eligible if c["score"] >= floor][:limit]


def _ask_model(self: Any, text: str, thread: Optional[dict[str, Any]], ranking: list[dict[str, Any]],
               limit: int) -> Optional[list[dict[str, Any]]]:
    """The model's choice among ``ranking`` (``[]`` when it says nobody must answer), or ``None`` when there is no
    model or it did not give a usable answer."""
    chat: Optional[Callable[[dict[str, Any]], Any]] = getattr(self, "route_chat", None)
    if chat is None:
        return None
    lines = []
    for c in ranking[:MODEL_CANDIDATES]:
        lines.append(f"- {c['agent']} (puntos {c['score']}, {c['presence']}): " + ("; ".join(c["reasons"]) or "sin señales")
                     + (f". Ahora: {c['doing']}" if c["doing"] else ""))
    system = ("Eres el enrutador de un chat de trabajo entre agentes de programación. Una persona ha escrito un mensaje "
              "sin nombrar a nadie. Elige, solo de la lista, a quién le toca contestar: como mucho "
              f"{limit}, a quien de verdad pueda aportar, y a nadie si el mensaje no pide nada a ningún agente "
              "(un agradecimiento, una charla). Prefiere al dueño de la tarea del hilo y a quien trabaja en lo mismo. "
              "Contesta en JSON con 'picks' (agent y un 'why' de una frase en español) y 'nobody'.")
    user = (f"Hilo: {(thread or {}).get('title') or 'canal'}\n\nMensaje de la persona:\n{text[:1500]}\n\n"
            "Candidatos:\n" + "\n".join(lines))
    try:
        status, payload = chat({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                "json": _SCHEMA, "timeout_s": MODEL_TIMEOUT_S, "max_tokens": 400, "effort": "low",
                                "temperature": 0.1})
    except Exception:  # noqa: BLE001 - the rules decide when the model cannot
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    data = payload.get("json") if isinstance(payload.get("json"), dict) else None
    if data is None:
        try:
            data = json.loads(str(payload.get("content") or payload.get("text") or ""))
        except ValueError:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
        return None
    allowed = {c["agent"] for c in ranking[:MODEL_CANDIDATES]}
    chosen: list[dict[str, Any]] = []
    for p in data["picks"]:
        ident = str((p or {}).get("agent") or "").strip().lower().lstrip("@") if isinstance(p, dict) else ""
        if ident in allowed and all(ident != c["agent"] for c in chosen):
            chosen.append({"agent": ident, "why": str(p.get("why") or "").strip()[:240] or "elegido por el modelo"})
    return chosen[:limit]


def plan(self: Any, text: str, thread_id: Optional[int] = None, author: str = "", *, use_model: bool = True,
         limit: Optional[int] = None, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """What the router would do with ``text``: ``{"picks", "ranking", "engine", "note"}``. Writes nothing."""
    cfg = settings_of(self)
    limit = max(1, min(int(limit or cfg["max_agents"]), HARD_MAX))
    ranking = candidates(self, text, thread_id, author, exclude)
    note = ""
    engine = "rules"
    picks: Optional[list[dict[str, Any]]] = None
    close = [c for c in ranking if c["score"] >= MIN_SCORE]
    if use_model and cfg["model"] == "auto" and len(close) > 1:
        picks = _ask_model(self, text, _thread(self, thread_id), close, limit)
        if picks is None:
            note = "el modelo no contestó: decidieron las reglas"
        else:
            engine = "model"
    if picks is None:
        picks = _by_rules(ranking, limit)
    if not picks and not note:
        note = "nadie encaja lo bastante" if not ranking else "nadie tiene que contestar"
    return {"picks": picks, "ranking": ranking[:10], "engine": engine, "note": note}


def applies(self: Any, a: dict[str, Any], mentions: list[str], kind: str, author: str, body: str) -> bool:
    """Whether a message just written is the router's to route."""
    ag = _ag()
    return (author == ag.PERSON and not mentions and kind in ("comment", "proposal") and bool(str(body).strip())
            and settings_of(self)["enabled"])


def record(self: Any, message_id: int, thread_id: int, planned: dict[str, Any]) -> dict[str, Any]:
    """Keep the choice for a message: its ``route`` (and the mentions already carried by it, at post time)."""
    now = self.clock()
    picks = [{**p, "status": "assigned", "by": planned["engine"]} for p in planned["picks"]]
    with self.db.tx():
        self.db.execute(
            "INSERT OR REPLACE INTO agora_routes(message_id, space_id, thread_id, picks, ranking, engine, note, created, "
            "updated) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(message_id), self.space_id, int(thread_id), json.dumps(picks, ensure_ascii=False),
             json.dumps(planned.get("ranking") or [], ensure_ascii=False), planned["engine"], planned.get("note") or "",
             now, now))
    self._bump("agora.route", {"space_id": self.space_id, "message_id": int(message_id),
                               "agents": [p["agent"] for p in picks]})
    return view(self, int(message_id)) or {}


def _label(picks: list[dict[str, Any]], engine: str) -> str:
    live = [p for p in picks if p.get("status") == "assigned"]
    by = "elegido por el enrutador" + (" (modelo)" if engine == "model" else " (reglas)")
    if live:
        return "Lo atiende " + ", ".join("@" + p["agent"] for p in live) + " · " + by
    if picks:
        return "Nadie lo atiende: " + ", ".join("@" + p["agent"] for p in picks) + " pasaron · " + by
    return "Nadie tiene que contestar · decidido por el enrutador"


def _view(row: Any) -> dict[str, Any]:
    try:
        picks = json.loads(row["picks"] or "[]")
        ranking = json.loads(row["ranking"] or "[]")
    except ValueError:
        picks, ranking = [], []
    return {"message_id": int(row["message_id"]), "thread_id": int(row["thread_id"]), "picks": picks,
            "ranking": ranking, "engine": row["engine"], "note": row["note"], "chosen_by": "router",
            "label": _label(picks, row["engine"]), "updated": row["updated"]}


def view(self: Any, message_id: int) -> Optional[dict[str, Any]]:
    row = self.db.one("SELECT * FROM agora_routes WHERE message_id=? AND space_id=?", (int(message_id), self.space_id))
    return _view(row) if row else None


def decorate(self: Any, messages: list[dict[str, Any]]) -> None:
    """Add ``route`` to the messages the router chose for (without its ranking), in place."""
    ids = [int(m["id"]) for m in messages if m.get("id") is not None]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = {int(r["message_id"]): r for r in self.db.query(
        f"SELECT * FROM agora_routes WHERE message_id IN ({marks})", tuple(ids))}
    for m in messages:
        row = rows.get(int(m["id"])) if m.get("id") is not None else None
        if row is not None:
            v = _view(row)
            v.pop("ranking", None)
            m["route"] = v


# ---- operations ---------------------------------------------------------------------------------------------------

def route(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Simulate the router: who would answer ``text`` (said in ``thread_id`` or ``channel``). With ``message_id`` the
    text is that message's; with ``apply`` as well the choice is written for it (and the chosen are told). The
    model is asked unless ``use_model`` is false."""
    ag = _ag()
    from . import agora_channels
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    message_id = a.get("message_id")
    thread_id = a.get("thread_id")
    text = str(a.get("text") or "")
    author = str(a.get("author") or "").strip().lower()
    msg = None
    if message_id not in (None, ""):
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            raise _err("message_id must be a number") from None
        msg = self.db.one("SELECT m.* FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.space_id=?",
                          (mid, self.space_id))
        if msg is None:
            raise _err(f"no message {mid} in {self.space_id}", 404)
        text, thread_id, author = text or str(msg["body"] or ""), msg["thread_id"], author or msg["author"]
    elif a.get("channel") and not thread_id:
        thread_id = next((r.get("thread_id") for r in agora_channels._rows(self, archived=True)
                          if r["slug"] == agora_channels.slugify(a["channel"])), None)
    if not text.strip():
        raise _err("text is required (or a message_id)")
    if thread_id and _thread(self, thread_id) is None:
        raise _err(f"no thread {thread_id} in {self.space_id}", 404)
    planned = plan(self, text, int(thread_id) if thread_id else None, author or ag.PERSON,
                   use_model=a.get("use_model") is not False, limit=a.get("max_agents"))
    out = {"ok": True, "settings": settings_of(self), "picks": planned["picks"], "candidates": planned["ranking"],
           "engine": planned["engine"], "note": planned["note"], "applied": False,
           "label": _label([{**p, "status": "assigned"} for p in planned["picks"]], planned["engine"])}
    if a.get("apply"):
        if msg is None:
            raise _err("apply needs a message_id: the router writes its choice for a message that exists")
        with self.db.tx():
            self._touch(agent)
            have = [x for x in ag._l(msg["mentions"])]
            merged = [*have, *[p["agent"] for p in planned["picks"] if p["agent"] not in have]]
            self.db.execute("UPDATE messages SET mentions=? WHERE id=?", (ag._j(merged), int(msg["id"])))
        out["route"] = record(self, int(msg["id"]), int(msg["thread_id"]), planned)
        out["applied"] = True
        out["mentions"] = merged
    return out


def routes(self: Any, a: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The latest choices of the router on this server, newest first."""
    a = a or {}
    try:
        limit = max(1, min(int(a.get("limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    agent = str(a.get("for_agent") or "").strip().lower()
    rows = [_view(r) for r in self.db.query(
        "SELECT * FROM agora_routes WHERE space_id=? ORDER BY message_id DESC LIMIT ?", (self.space_id, 400 if agent else limit))]
    if agent:
        rows = [r for r in rows if any(p["agent"] == agent for p in r["picks"])][:limit]
    for r in rows:
        r.pop("ranking", None)
    return {"ok": True, "routes": rows, "settings": settings_of(self)}


def route_pass(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """An agent chosen for a message says it is not its to answer (``reason`` is optional): the router passes it to
    the next candidate, if there is one, and tells them in the thread."""
    ag = _ag()
    agent = self._agent(a.get("agent"))
    try:
        mid = int(a.get("message_id"))
    except (TypeError, ValueError):
        raise _err("message_id is required: the message you were chosen for") from None
    row = self.db.one("SELECT * FROM agora_routes WHERE message_id=? AND space_id=?", (mid, self.space_id))
    if row is None:
        raise _err(f"the router did not route message {mid}", 404)
    current = _view(row)
    mine = next((p for p in current["picks"] if p["agent"] == agent and p.get("status") == "assigned"), None)
    if mine is None:
        raise _err(f"{agent} is not among those the router chose for message {mid}", 409)
    msg = self.db.one("SELECT * FROM messages WHERE id=?", (mid,))
    passed = [p["agent"] for p in current["picks"] if p.get("status") == "passed"] + [agent]
    taken = [p["agent"] for p in current["picks"]]
    limit = max(1, settings_of(self)["max_agents"])
    nxt = next((c for c in candidates(self, str(msg["body"] or ""), int(row["thread_id"]), str(msg["author"] or ""),
                                      tuple(dict.fromkeys([*passed, *taken]))) if c["score"] >= MIN_SCORE), None)
    reason = str(a.get("reason") or "").strip()[:240]
    with self.db.tx():
        self._touch(agent)
        picks = current["picks"]
        mine["status"] = "passed"
        mine["reason"] = reason
        mentions = [m for m in ag._l(msg["mentions"]) if m != agent]
        told: list[str] = []
        if nxt is not None and sum(1 for p in picks if p.get("status") == "assigned") < limit:
            picks.append({"agent": nxt["agent"], "why": "; ".join(nxt["reasons"][:2]) or "el siguiente que mejor encaja",
                          "status": "assigned", "by": "rules", "after": agent})
            if nxt["agent"] not in mentions:
                mentions.append(nxt["agent"])
            told = [nxt["agent"]]
        self.db.execute("UPDATE messages SET mentions=? WHERE id=?", (ag._j(mentions), mid))
        self.db.execute("UPDATE agora_routes SET picks=?, updated=? WHERE message_id=?",
                        (json.dumps(picks, ensure_ascii=False), self.clock(), mid))
        if told:
            self._msg(int(row["thread_id"]), "system", "system",
                      f"@{agent} no atiende el mensaje #{mid}" + (f" ({reason})" if reason else "")
                      + f"; el enrutador lo pasa a @{told[0]}.", told)
    self._bump("agora.route.pass", {"space_id": self.space_id, "message_id": mid, "agent": agent,
                                    "next": nxt["agent"] if nxt else None})
    return {"ok": True, "passed": agent, "next": nxt["agent"] if nxt else None, "route": view(self, mid)}


OPERATIONS = {"route": route, "routes": routes, "route_pass": route_pass, "route_settings": route_settings,
              "route_settings_set": route_settings_set}
WRITE_OPS = ("route", "route_pass", "route_settings_set")
READ_ROUTES = ("routes", "route_settings")


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "hub_agora_route",
         "description": "The Ágora router: who should answer a message that names nobody. Give text (and thread_id or "
                        "channel for context) to simulate: it returns the agents chosen (up to the server's maximum, or "
                        "none) with the reason of each, the scored candidates and whether a model or the rules chose. "
                        "With message_id the text is that message's and with apply=true the choice is written for it: "
                        "the chosen are mentioned and the message carries the route. use_model=false decides by "
                        "rules only. The router acts by itself on what the person writes without naming anyone.",
         "inputSchema": schema({"agent": agent_prop, "text": {"type": "string", "maxLength": 20000},
                                "thread_id": {"type": "integer"}, "channel": {"type": "string"},
                                "message_id": {"type": "integer", "minimum": 1}, "apply": {"type": "boolean"},
                                "use_model": {"type": "boolean"}, "author": {"type": "string"},
                                "max_agents": {"type": "integer", "minimum": 1, "maximum": HARD_MAX}}, ["agent"])},
        {"name": "hub_agora_route_pass",
         "description": "You were chosen by the router for a message (it mentions you) and it is not yours to answer: say "
                        "so and the router tries the next candidate, who is told in the thread. reason is optional.",
         "inputSchema": schema({"agent": agent_prop, "message_id": {"type": "integer", "minimum": 1},
                                "reason": {"type": "string", "maxLength": 240}}, ["agent", "message_id"])},
        {"name": "hub_agora_routes", "read": True,
         "description": "The latest choices of the router on this server, newest first: the message, who was chosen and "
                        "why, who passed, and the engine (model or rules). for_agent keeps those that chose that agent.",
         "inputSchema": schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                "for_agent": {"type": "string"}}, [])},
        {"name": "hub_agora_route_settings", "read": True,
         "description": "How the router is set on this server: enabled, max_agents and model (auto: the family's model "
                        "through Hoard Link; rules: never ask a model).",
         "inputSchema": schema({}, [])},
        {"name": "hub_agora_route_settings_set",
         "description": "Change the router of this server: enabled (on or off), max_agents (1 to %d) and model (auto or "
                        "rules). What is not given stays." % HARD_MAX,
         "inputSchema": schema({"agent": agent_prop, "enabled": {"type": "boolean"},
                                "max_agents": {"type": "integer", "minimum": 1, "maximum": HARD_MAX},
                                "model": {"type": "string", "enum": list(MODELS)}}, ["agent"])},
    ]
