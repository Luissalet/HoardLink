"""The bus writes to the Brain — the ``brain`` facet.

A rule may carry a ``brain`` action (``actions.py``): *when* an event matches, create or update an entity or a
note in Faustus's Brain and link it to others. The action is a template over the event::

    {"kind": "brain",
     "key": "phileas:shipment:${event.data.shipment_id}",
     "entity": {"name": "${event.data.person}", "type": "person", "aliases": [], "summary": ""},
     "note": {"folder": "Familia/Envíos", "title": "Envío ${event.data.merchant}", "mode": "append" | "replace",
              "date": "${event.data.due}", "text": ["Transportista: ${event.data.carrier}", ...], "tags": []},
     "links": [{"to": "${event.data.merchant}", "type": "organization", "rel": "related_to"},
               {"each": "${event.data.attendees}", "to": "${item}", "type": "person"}]}

``${a.b|c.d}`` takes the first path that has a value; a text line whose placeholders are all empty is left out.
``key`` names the thing written: the same key always lands on the same entity and the same note, so an event seen
twice writes nothing new and a later event about the same thing updates it. ``entity`` is created or found by name;
``note`` lives at ``<folder>/<title>.md`` (``replace`` keeps the latest text, ``append`` adds one dated line per
event); each ``links`` target is an entity: the main entity gets a relation to it and the note cites it.

Where Faustus is comes from the placement (PC or a Spark), or from a fixed address set here. Every write goes
through a queue on disk (``<data>/brain_bus.db``): when Faustus does not answer, or is being moved, the write
waits and is retried; nothing is dropped. A Faustus without ``POST /api/brain/entities`` still gets the notes
(the write is ``partial``) and the entities follow when that route appears.

HTTP: ``GET /api/brain`` (target, queue, rules), ``GET /api/brain/history``, ``POST /api/brain/test``
(what a rule would write for an event, or write it), ``POST /api/brain/retry``, ``POST /api/brain/target``,
``POST /api/brain/rules/<id>`` (``{"enabled": bool}``; installs a recommended rule that is missing).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional

from .facets import Facet, Request

TYPES = ("person", "project", "organization", "place", "tool", "concept", "event", "other")
TYPE_NAMES = {"person": "persona", "project": "proyecto", "organization": "organización", "place": "lugar",
              "tool": "herramienta", "concept": "concepto", "event": "evento", "other": "entidad"}
TYPE_FOLDERS = {"person": "Personas", "project": "Proyectos", "organization": "Organizaciones", "place": "Lugares",
                "tool": "Herramientas", "concept": "Conceptos", "event": "Eventos", "other": "Otros"}
RETRY_STEPS = (20.0, 60.0, 180.0, 600.0, 900.0)
PROBE_EVERY_S = 600.0
KEEP_WRITES = 3000
MAX_LOG = 200
_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")
_BAD_TITLE = re.compile(r'[\\/:*?"<>|#^\[\]\r\n\t]+')


class Unreachable(Exception):
    """Faustus did not answer (or answered 5xx): the write stays queued."""


class Rejected(Exception):
    """Faustus refused the write (4xx): retrying the same body will not help."""


# ------------------------------------------------------------------------------------------ templates
def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.strip().split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _first(expr: str, ctx: dict[str, Any]) -> Any:
    for option in expr.split("|"):
        option = option.strip()
        if len(option) >= 2 and option[0] == option[-1] and option[0] in "'\"":
            return option[1:-1]
        value = _lookup(ctx, option)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or json.dumps(value, ensure_ascii=False, default=str))
    return str(value)


def render(template: Any, ctx: dict[str, Any]) -> str:
    """``template`` with its ``${...}`` filled; spaces collapsed, dangling separators trimmed."""
    out = _PLACEHOLDER.sub(lambda m: _text(_first(m.group(1), ctx)), str(template or ""))
    return re.sub(r"\s+", " ", out).strip().strip("·-–:#,").strip()


def render_value(template: Any, ctx: dict[str, Any]) -> Any:
    """A template that is exactly one placeholder keeps the value's type (a list stays a list)."""
    whole = _PLACEHOLDER.fullmatch(str(template or "").strip())
    return _first(whole.group(1), ctx) if whole else render(template, ctx)


def render_lines(text: Any, ctx: dict[str, Any]) -> list[str]:
    """The text as lines; a line that has placeholders and all of them are empty is dropped."""
    lines = text if isinstance(text, list) else str(text or "").split("\n")
    out: list[str] = []
    for raw in lines:
        raw = str(raw)
        found = _PLACEHOLDER.findall(raw)
        if found and all(_first(expr, ctx) in (None, "", [], {}) for expr in found):
            continue
        line = render(raw, ctx)
        if line:
            out.append(line)
    return out


def safe_title(title: str) -> str:
    return re.sub(r"\s+", " ", _BAD_TITLE.sub(" ", str(title or ""))).strip().strip(".")[:120].strip()


def safe_folder(folder: str) -> str:
    parts = [safe_title(p) for p in str(folder or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p not in (".", "..")) or "Familia"


def brain_actions(rule: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in (rule.get("then") or []) if isinstance(a, dict) and a.get("kind") == "brain"]


def validate_action(action: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    entity, note = action.get("entity"), action.get("note")
    if not isinstance(entity, dict) and not isinstance(note, dict):
        return ["'brain' needs an entity ({name, type}) or a note ({title, text})"]
    if isinstance(entity, dict) and not entity.get("name"):
        problems.append("'brain' entity needs a name")
    if isinstance(entity, dict) and str(entity.get("type") or "other") not in TYPES and "${" not in str(entity.get("type")):
        problems.append(f"'brain' entity type must be one of {', '.join(TYPES)}")
    if isinstance(note, dict) and not note.get("title"):
        problems.append("'brain' note needs a title")
    if isinstance(note, dict) and str(note.get("mode") or "replace") not in ("replace", "append"):
        problems.append("'brain' note mode is 'replace' or 'append'")
    if "links" in action and not isinstance(action["links"], list):
        problems.append("'brain' links must be a list")
    return problems


def build_plan(action: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """What ``action`` writes for the event in ``ctx``: ``{key, entity?, note?, links, origin}`` or ``{skip}``."""
    event = ctx.get("event") if isinstance(ctx.get("event"), dict) else {}
    plan: dict[str, Any] = {"links": []}
    entity = action.get("entity")
    if isinstance(entity, dict):
        name = render(entity.get("name"), ctx)[:160]
        if not name:
            return {"skip": "el evento no trae el nombre de la entidad"}
        etype = render(entity.get("type") or "other", ctx).lower()
        aliases = render_value(entity.get("aliases"), ctx) if isinstance(entity.get("aliases"), str) else [
            render(a, ctx) for a in (entity.get("aliases") or [])]
        aliases = [str(a) for a in (aliases if isinstance(aliases, list) else [aliases]) if a and str(a) != name]
        plan["entity"] = {"name": name, "type": etype if etype in TYPES else "other", "aliases": aliases[:10],
                          "summary": " ".join(render_lines(entity.get("summary"), ctx))[:600]}
    seen = {plan["entity"]["name"].casefold()} if "entity" in plan else set()
    for link in action.get("links") or []:
        if not isinstance(link, dict):
            continue
        items = [None]
        if "each" in link:
            many = render_value(link.get("each"), ctx)
            items = many if isinstance(many, list) else ([many] if many else [])
        for item in items[:30]:
            sub = ctx if item is None else {**ctx, "item": item}
            to = render(link.get("to"), sub)[:160]
            if not to or to.casefold() in seen:
                continue
            seen.add(to.casefold())
            ltype = render(link.get("type") or "other", sub).lower()
            plan["links"].append({"to": to, "type": ltype if ltype in TYPES else "other",
                                  "rel": render(link.get("rel") or "related_to", sub) or "related_to"})
    note = action.get("note")
    if isinstance(note, dict):
        title = safe_title(render(note.get("title"), ctx))
        if not title:
            return {"skip": "el evento no trae el título de la nota"}
        mode = "append" if str(note.get("mode") or "replace") == "append" else "replace"
        lines = render_lines(note.get("text"), ctx)
        tags = [t for t in (render(x, ctx).lower().replace(" ", "-") for x in (note.get("tags") or [])) if t]
        plan["note"] = {"folder": safe_folder(render(note.get("folder") or "Familia", ctx)), "title": title, "mode": mode,
                        "date": render(note.get("date"), ctx)[:32], "tags": tags[:8],
                        "text": " · ".join(lines) if mode == "append" else "\n".join(f"- {line}" for line in lines)}
    if "entity" not in plan and "note" not in plan:
        return {"skip": "la acción no define ni entidad ni nota"}
    # the key names the thing: an event that lacks what the key is made of is about nothing we can name
    if any(_first(expr, ctx) in (None, "", [], {}) for expr in _PLACEHOLDER.findall(str(action.get("key") or ""))):
        return {"skip": "el evento no trae el dato que identifica lo que se escribe (la clave)"}
    key = render(action.get("key"), ctx) if action.get("key") else ""
    if not key:
        thing = plan["entity"]["name"] if "entity" in plan else f"{plan['note']['folder']}/{plan['note']['title']}"
        key = f"{event.get('source') or 'hub'}:{thing}"
    plan["key"] = key[:240]
    ts = float(event.get("ts") or ctx.get("now") or time.time())
    plan["origin"] = {"type": str(event.get("type") or ""), "source": str(event.get("source") or ""),
                      "when": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")}
    return plan


def plan_hash(plan: dict[str, Any]) -> str:
    core = {k: plan.get(k) for k in ("key", "entity", "note", "links")}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def note_path(plan: dict[str, Any]) -> str:
    note = plan.get("note")
    if note:
        return f"{note['folder']}/{note['title']}.md"
    ent = plan["entity"]
    return f"Familia/{TYPE_FOLDERS.get(ent['type'], 'Otros')}/{safe_title(ent['name'])}.md"


def zone_text(plan: dict[str, Any], log: list[dict[str, Any]]) -> str:
    """The part of the note the hub owns: the text (or the dated lines), who it links to and where it came from."""
    note = plan.get("note")
    parts: list[str] = []
    if note and note["mode"] == "append":
        parts.append("\n".join(f"- {e['when']} · {e['line']}" for e in log))
    elif note:
        parts.append(note["text"])
    else:
        parts.append(plan["entity"].get("summary") or "")
    names = ([plan["entity"]["name"]] if plan.get("entity") and note else []) + [l["to"] for l in plan.get("links") or []]
    if names:
        parts.append("Enlaces: " + " · ".join(f"[[{n}]]" for n in names))
    origin = plan.get("origin") or {}
    if origin.get("type"):
        parts.append(f"Origen: {origin.get('source') or 'hub'} · {origin['type']} · {origin.get('when') or ''}".rstrip(" ·"))
    return "\n\n".join(p for p in parts if p.strip())


def compose_note(existing: Optional[str], plan: dict[str, Any], zone: str, *, now_iso: str, previous: str = "") -> str:
    """The whole file: a new note with its header, or the existing one with only the hub's part replaced. The hub's
    part is the text it wrote last time (``previous``): no markers in the note. When that text is no longer there
    (someone rewrote it) the new text goes at the end and what the person wrote stays."""
    block = zone
    if existing is None:
        note = plan.get("note") or {}
        tags = ["familia"] + [t for t in (note.get("tags") or []) if t != "familia"]
        head = ["---", "kind: note", f"created: {now_iso}", f"updated: {now_iso}", "tags: [" + ", ".join(tags) + "]"]
        if note.get("date"):
            head.append(f"date: {note['date']}")
        head += [f"hub_key: {json.dumps(plan['key'], ensure_ascii=False)}", "---", ""]
        return "\n".join(head) + block + "\n"
    if block in existing:
        return existing
    if previous and previous in existing:
        out = existing.replace(previous, block, 1)
    else:
        out = existing.rstrip("\n") + "\n\n" + block + "\n"
    return _refresh_header(out, plan, now_iso)


def _refresh_header(text: str, plan: dict[str, Any], now_iso: str) -> str:
    """``updated`` and ``date`` of a note the hub created (its header carries ``hub_key``) follow the latest write."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    head = text[4:end] if end > 0 else ""
    if "hub_key:" not in head:
        return text
    lines = head.split("\n")
    fields = {"updated": now_iso}
    if (plan.get("note") or {}).get("date"):
        fields["date"] = plan["note"]["date"]
    for key, value in fields.items():
        at = next((i for i, line in enumerate(lines) if line.startswith(key + ":")), None)
        if at is None:
            lines.insert(next((i for i, line in enumerate(lines) if line.startswith("hub_key:")), len(lines)), f"{key}: {value}")
        else:
            lines[at] = f"{key}: {value}"
    return "---\n" + "\n".join(lines) + text[end:]


# ------------------------------------------------------------------------------------------ recommended rules
def factory_rules() -> list[dict[str, Any]]:
    """Rules over events the apps already emit. ``sample`` is the event data the page offers to try the rule with
    (it is not stored with the rule). ``cooldown_s`` is 0: every event counts, the key keeps them from piling up."""
    def rule(rid: str, name: str, when: str, brain: dict[str, Any], note: str, sample: dict[str, Any], *,
             enabled: bool = True, sample_type: str = "") -> dict[str, Any]:
        return {"id": rid, "name": name, "when": {"type": when}, "then": [{"kind": "brain", **brain}], "cooldown_s": 0,
                "enabled": enabled, "note": note, "sample": {"type": sample_type or when.split("|")[0], "data": sample}}

    return [
        rule("rule-brain-people-commitment", "Compromiso con una persona → persona en el Cerebro",
             "people.commitment.added|people.commitment.noted",
             {"key": "people:person:${event.data.person_id|event.data.person}",
              "entity": {"name": "${event.data.person}", "type": "person"},
              "note": {"folder": "Familia/Compromisos", "title": "Compromisos con ${event.data.person}", "mode": "append",
                       "tags": ["compromiso"], "text": ["${event.data.text|event.data.title}", "vence ${event.data.due}"]}},
             "People apunta un compromiso con alguien: esa persona queda como entidad y el compromiso se añade a su nota.",
             {"id": 41, "direction": "mine", "person_id": 7, "person": "Marta Ibarra", "text": "Enviarle el presupuesto",
              "due": "2026-11-20", "source": "manual"}),
        rule("rule-brain-shipment", "Envío → nota enlazada a la tienda", "phileas.shipment.new|phileas.shipment.delivered",
             {"key": "phileas:shipment:${event.data.shipment_id}",
              "note": {"folder": "Familia/Envíos", "mode": "append", "tags": ["envío"],
                       "title": "Envío ${event.data.merchant} ${event.data.order_ref|event.data.tracking_number|event.data.shipment_id}",
                       "text": ["Entregado el ${event.data.delivered_at}", "Artículos: ${event.data.items}",
                                "Transportista: ${event.data.carrier}", "Seguimiento: ${event.data.tracking_number}",
                                "Pedido: ${event.data.order_ref}"]},
              "links": [{"to": "${event.data.merchant}", "type": "organization"}]},
             "Phileas ve un envío nuevo o entregado: una nota por envío, con una línea por novedad, enlazada a la tienda.",
             {"shipment_id": "s_demo1", "merchant": "Tienda Norte", "order_ref": "4471", "items": ["Zapatillas"],
              "carrier": "Correos", "tracking_number": "PQ123"}),
        rule("rule-brain-kafka-deadline", "Plazo → nota con fecha", "kafka.deadline.created",
             {"key": "kafka:deadline:${event.data.deadline_id}",
              "note": {"folder": "Familia/Plazos", "title": "${event.data.title}", "date": "${event.data.due}", "tags": ["plazo"],
                       "text": ["Vence: ${event.data.due}", "Tipo: ${event.data.kind}"]}},
             "Kafka crea un plazo (fin de garantía, devolución, pago): una nota con su fecha.",
             {"deadline_id": "t_demo1", "title": "Fin de la garantía de Tienda Norte", "due": "2029-11-09", "kind": "warranty_end"}),
        rule("rule-brain-kafka-document", "Documento archivado → nota enlazada al emisor", "kafka.document.archived",
             {"key": "kafka:document:${event.data.doc_id}",
              "note": {"folder": "Familia/Documentos", "date": "${event.data.date}", "tags": ["documento"],
                       "title": "${event.data.kind} ${event.data.merchant} ${event.data.date|event.data.doc_id}",
                       "text": ["Emisor: ${event.data.merchant}", "Importe: ${event.data.amount} ${event.data.currency}",
                                "Fecha: ${event.data.date}", "Pedido: ${event.data.order_ref}",
                                "Documento: ${event.data.doc_id}"]},
              "links": [{"to": "${event.data.merchant}", "type": "organization"}]},
             "Kafka archiva una factura, un recibo o una garantía: una nota con su fecha, enlazada a quien lo emite.",
             {"doc_id": "d_demo1", "kind": "invoice", "merchant": "Tienda Norte", "amount": 59.9, "currency": "EUR",
              "date": "2026-11-09", "order_ref": "4471"}),
        rule("rule-brain-kafka-warranty", "Garantía → nota con fecha de fin", "kafka.warranty.created",
             {"key": "kafka:warranty:${event.data.doc_id}",
              "note": {"folder": "Familia/Plazos", "title": "Garantía ${event.data.merchant} ${event.data.doc_id}",
                       "date": "${event.data.until}", "tags": ["garantía"],
                       "text": ["Hasta: ${event.data.until}", "Tienda: ${event.data.merchant}", "Envío: ${event.data.shipment_id}"]},
              "links": [{"to": "${event.data.merchant}", "type": "organization"}]},
             "Kafka abre una garantía: una nota con la fecha en que termina, enlazada a la tienda.",
             {"doc_id": "d_demo1", "shipment_id": "s_demo1", "merchant": "Tienda Norte", "until": "2029-11-09"}),
        rule("rule-brain-minutes", "Acta de reunión → nota enlazada a los asistentes", "funes.minutes.ready",
             {"key": "funes:minutes:${event.data.minutes_id}",
              "note": {"folder": "Familia/Reuniones", "title": "${event.data.title} ${event.data.date}",
                       "date": "${event.data.date}", "tags": ["reunión"],
                       "text": ["Fecha: ${event.data.date}", "Asistentes: ${event.data.attendees}",
                                "Acciones pendientes: ${event.data.action_items}"]},
              "links": [{"each": "${event.data.attendees}", "to": "${item.name|item}", "type": "person"}]},
             "Funes termina un acta: una nota de la reunión enlazada a cada asistente como persona.",
             {"minutes_id": "m_demo1", "title": "Revisión del taller", "date": "2026-11-09",
              "attendees": ["Marta Ibarra", "Pablo Sanz"], "action_items": 2}),
        rule("rule-brain-purchase", "Compra → nota enlazada a la tienda", "purchases.stage",
             {"key": "purchases:${event.data.id}",
              "note": {"folder": "Familia/Compras", "title": "Compra ${event.data.title|event.data.merchant} ${event.data.id}",
                       "mode": "append", "tags": ["compra"], "text": ["Estado: ${event.data.stage}"]},
              "links": [{"to": "${event.data.merchant}", "type": "organization"}]},
             "Una compra cambia de etapa en el Hub: una nota por compra con sus etapas. Desactivada de fábrica porque "
             "repite lo que ya apuntan el envío y el documento.",
             {"id": 67, "stage": "filed", "previous": None, "merchant": "Tienda Norte", "title": "Tienda Norte"}, enabled=False),
        rule("rule-brain-agora-delivery", "Tarea del Ágora terminada → nota enlazada a su espacio", "agora.task.done",
             {"key": "agora:task:${event.data.task_id}",
              "note": {"folder": "Familia/Entregas", "title": "Entrega ${event.data.task_id} ${event.data.title}",
                       "tags": ["entrega"],
                       "text": ["Espacio: ${event.data.space_id}", "Agente: ${event.data.agent}",
                                "Commits: ${event.data.commits}", "Revisión: ${event.data.review_state}"]},
              "links": [{"to": "${event.data.space_id}", "type": "project"}]},
             "Un agente cierra una tarea del Ágora: una nota de la entrega enlazada al espacio. Desactivada de fábrica: "
             "son muchas al día.",
             {"space_id": "hoardlink", "task_id": 2409, "agent": "claude",
              "commits": ["abc1234"], "review_state": "exempt"}, enabled=False),
    ]


def samples() -> dict[str, dict[str, Any]]:
    return {r["id"]: r["sample"] for r in factory_rules()}


# ------------------------------------------------------------------------------------------ the Brain over HTTP
class BrainClient:
    """The few calls the hub makes to ``<faustus>/api/brain``. ``Unreachable`` = keep the write queued."""

    def __init__(self, base: str, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, query: Optional[dict[str, str]] = None) -> tuple[int, Any]:
        url = f"{self.base}/api/brain{path}" + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                             "User-Agent": "hoard-hub"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode("utf-8", "replace"), exc.code
        except Exception as exc:  # noqa: BLE001 - refused, timed out, DNS, TLS: all "not there now"
            raise Unreachable(f"{type(exc).__name__}: {exc}") from exc
        if status >= 500 or status in (408, 429):
            raise Unreachable(f"HTTP {status}: {raw[:200]}")
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            if status < 400:
                raise Unreachable(f"HTTP {status} sin JSON: no parece el Cerebro de Faustus")
            return status, {"detail": raw[:200]}

    @staticmethod
    def _detail(payload: Any) -> str:
        return str((payload or {}).get("detail") or (payload or {}).get("error") or payload)[:300] if isinstance(payload, dict) else str(payload)[:300]

    def status(self) -> dict[str, Any]:
        status, payload = self._call("GET", "/status")
        if status != 200 or not isinstance(payload, dict):
            raise Unreachable(f"HTTP {status}: {self._detail(payload)}")
        return payload

    def read_note(self, path: str) -> Optional[str]:
        status, payload = self._call("GET", "/note", query={"path": path})
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise Rejected(f"leer {path}: HTTP {status}: {self._detail(payload)}")
        return str(payload.get("content") or "")

    def write_note(self, path: str, content: str) -> str:
        status, payload = self._call("PUT", "/note", {"path": path, "content": content})
        if status != 200 or not isinstance(payload, dict):
            raise Rejected(f"escribir {path}: HTTP {status}: {self._detail(payload)}")
        return str((payload.get("note") or {}).get("path") or path)

    def entities_supported(self) -> bool:
        status, _ = self._call("POST", "/entities", {})
        return status not in (404, 405)

    def upsert_entity(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The entity, or None when this Faustus has no route to create one."""
        status, payload = self._call("POST", "/entities", body)
        if status in (404, 405):
            return None
        if status != 200 or not isinstance(payload, dict):
            raise Rejected(f"entidad {body.get('name')}: HTTP {status}: {self._detail(payload)}")
        return payload


# ------------------------------------------------------------------------------------------ the facet
class BrainFacet(Facet):
    id = "brain"
    ui_scripts = ("brain.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._kick = False          # something was queued while the worker was busy: do not sleep on it
        self._down_until = 0.0
        self._down_steps = 0
        self._last_error = ""
        self._last_ok = 0.0
        self._probe_at = 0.0
        self.retry_steps = RETRY_STEPS
        self.settings_path = os.path.join(hub.config.data_dir, "brain_bus.json")
        path = os.path.join(hub.config.data_dir, "brain_bus.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS writes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, rule_id TEXT NOT NULL DEFAULT '', "
            "rule_name TEXT NOT NULL DEFAULT '', event_id INTEGER, event_type TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, "
            "plan TEXT NOT NULL, plan_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
            "next_try REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}', done_ts REAL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS writes_status ON writes(status, id)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS things (key TEXT PRIMARY KEY, entity_id TEXT NOT NULL DEFAULT '', entity_name TEXT NOT NULL DEFAULT '', "
            "note_path TEXT NOT NULL DEFAULT '', plan_hash TEXT NOT NULL DEFAULT '', hashes TEXT NOT NULL DEFAULT '[]', "
            "log TEXT NOT NULL DEFAULT '[]', base TEXT NOT NULL DEFAULT '', writes INTEGER NOT NULL DEFAULT 0, "
            "repeats INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, zone TEXT NOT NULL DEFAULT '')")
        if "zone" not in [c[1] for c in self._db.execute("PRAGMA table_info(things)").fetchall()]:
            self._db.execute("ALTER TABLE things ADD COLUMN zone TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def start(self) -> None:
        self._worker = threading.Thread(target=self._loop, name="hoard-hub-brain", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=3)
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    # -- where the Brain is -----------------------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(open(self.settings_path, "r", encoding="utf-8-sig").read())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def set_target(self, url: str) -> dict[str, Any]:
        """Pin the Faustus the bus writes to (``""`` = follow the placement again)."""
        url = str(url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            return {"ok": False, "status": 400, "error": "la dirección empieza por http:// o https://"}
        data = self._settings()
        data["faustus_url"] = url
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.settings_path)
        with self._wake:
            self._down_until, self._down_steps, self._probe_at = 0.0, 0, 0.0
            self._kick = True
            self._wake.notify_all()
        return {"ok": True, "target": self.target()}

    def target(self) -> dict[str, Any]:
        """``{base, source, node?, moving}``: the fixed address, else the Spark the placement names, else this PC."""
        fixed = str(os.environ.get("HOARD_HUB_BRAIN_URL") or self._settings().get("faustus_url") or "").strip().rstrip("/")
        if fixed:
            return {"base": fixed, "source": "fixed", "moving": False}
        try:
            state = self.hub.placement.state()
        except Exception:  # noqa: BLE001
            state = {}
        if (state.get("job") or {}).get("running"):
            return {"base": "", "source": "placement", "moving": True}
        if state.get("location") == "sparks":
            url = str((state.get("urls") or {}).get("faustus") or "").rstrip("/")
            if url.startswith(("http://", "https://")):
                return {"base": url, "source": "placement", "node": (state.get("node") or {}).get("name"), "moving": False}
        urls = list(getattr(self.hub.config, "faustus_urls", None) or ["http://127.0.0.1:7000"])
        return {"base": str(urls[0]).rstrip("/"), "source": "pc", "moving": False}

    def client(self, base: str) -> BrainClient:
        return BrainClient(base)

    # -- rows -----------------------------------------------------------------------------------
    _WCOLS = "id, ts, rule_id, rule_name, event_id, event_type, key, plan, plan_hash, status, attempts, next_try, error, result, done_ts"

    @staticmethod
    def _write_row(r: tuple[Any, ...]) -> dict[str, Any]:
        plan, result = json.loads(r[7] or "{}"), json.loads(r[13] or "{}")
        return {"id": int(r[0]), "ts": float(r[1]), "rule": r[2], "rule_name": r[3], "event_id": r[4], "event_type": r[5],
                "key": r[6], "plan": plan, "plan_hash": r[8], "status": r[9], "attempts": int(r[10]), "next_try": float(r[11] or 0),
                "error": r[12], "result": result, "done_ts": r[14], "url": result.get("url") or "",
                "summary": describe_plan(plan)}

    def _thing(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT key, entity_id, entity_name, note_path, plan_hash, hashes, log, base, writes, repeats, updated, zone "
                                 "FROM things WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        return {"key": r[0], "entity_id": r[1], "entity_name": r[2], "note_path": r[3], "plan_hash": r[4],
                "hashes": json.loads(r[5] or "[]"), "log": json.loads(r[6] or "[]"), "base": r[7], "writes": int(r[8]),
                "repeats": int(r[9]), "updated": float(r[10]), "zone": r[11]}

    def get_write(self, write_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._db.execute(f"SELECT {self._WCOLS} FROM writes WHERE id=?", (int(write_id),)).fetchone()
        return self._write_row(r) if r else None

    def history(self, *, rule: str = "", status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if rule:
            where.append("rule_id=?"); params.append(rule)
        if status:
            where.append("status=?"); params.append(status)
        limit = max(1, min(int(limit or 50), 500))
        with self._lock:
            rows = self._db.execute(f"SELECT {self._WCOLS} FROM writes WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                                    (*params, limit)).fetchall()
        return [self._write_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM writes GROUP BY status").fetchall()
        out = {"pending": 0, "done": 0, "partial": 0, "failed": 0}
        out.update({s: int(n) for s, n in rows})
        return out

    # -- from a rule ------------------------------------------------------------------------------
    def effect(self, plan: dict[str, Any]) -> str:
        """``create`` | ``update`` | ``unchanged`` for a plan, from what the hub already wrote under its key."""
        thing = self._thing(plan["key"])
        if thing is None or not (thing["note_path"] or thing["entity_id"]):
            return "create"
        h = plan_hash(plan)
        append = (plan.get("note") or {}).get("mode") == "append"
        return "unchanged" if (h in thing["hashes"] if append else h == thing["plan_hash"]) else "update"

    def submit(self, action: dict[str, Any], ctx: dict[str, Any], *, force: bool = False, wait: float = 0.0) -> dict[str, Any]:
        """Queue what ``action`` writes for the event in ``ctx``. The same thing with the same content is not queued
        again; ``wait`` blocks up to that many seconds for the write to land."""
        plan = build_plan(action, ctx)
        if plan.get("skip"):
            return {"ok": True, "skipped": True, "reason": plan["skip"]}
        h = plan_hash(plan)
        event = ctx.get("event") if isinstance(ctx.get("event"), dict) else {}
        rule = ctx.get("rule") if isinstance(ctx.get("rule"), dict) else {}
        with self._wake:
            if not force and self.effect(plan) == "unchanged":
                self._db.execute("UPDATE things SET repeats = repeats + 1 WHERE key=?", (plan["key"],))
                self._db.commit()
                thing = self._thing(plan["key"]) or {}
                return {"ok": True, "unchanged": True, "key": plan["key"], "note": thing.get("note_path") or "",
                        "entity_id": thing.get("entity_id") or ""}
            dup = None if force else self._db.execute(
                "SELECT id FROM writes WHERE key=? AND plan_hash=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (plan["key"], h)).fetchone()
            if dup:
                return {"ok": True, "queued": True, "duplicate": True, "write_id": int(dup[0]), "key": plan["key"]}
            event_id = event.get("id") if isinstance(event.get("id"), int) else None
            cur = self._db.execute(
                "INSERT INTO writes (ts, rule_id, rule_name, event_id, event_type, key, plan, plan_hash) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), str(rule.get("id") or ""), str(rule.get("name") or ""), event_id, str(event.get("type") or ""),
                 plan["key"], json.dumps(plan, ensure_ascii=False), h))
            self._db.commit()
            write_id = int(cur.lastrowid)
            self._kick = True
            self._wake.notify_all()
        out = {"ok": True, "queued": True, "write_id": write_id, "key": plan["key"]}
        if wait > 0:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                row = self.get_write(write_id)
                if row and row["status"] != "pending":
                    return {"ok": row["status"] in ("done", "partial"), "write_id": write_id, "key": plan["key"],
                            "status": row["status"], "error": row["error"], "result": row["result"], "url": row["url"]}
                time.sleep(0.05)
            row = self.get_write(write_id) or {}
            out.update(status="pending", error=row.get("error") or self._last_error)
        return out

    # -- the queue ----------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.drain()
            except Exception:  # noqa: BLE001 - the worker must outlive a bad row
                worked = False
            with self._wake:
                if self._stop.is_set():
                    return
                if not self._kick:
                    self._wake.wait(0.2 if worked else 5.0)
                self._kick = False

    def drain(self) -> bool:
        """Deliver what is due, oldest first. Returns True when something was written."""
        now = time.time()
        target = self.target()
        if target.get("moving") or not target.get("base"):
            self._last_error = "Faustus se está trasladando: se escribirá cuando termine"
            return False
        if now < self._down_until:
            return False
        if now - self._probe_at > PROBE_EVERY_S:
            self._requeue_partial(target["base"])
        worked = False
        while not self._stop.is_set():
            with self._lock:
                r = self._db.execute(f"SELECT {self._WCOLS} FROM writes WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 1",
                                     (time.time(),)).fetchone()
            if not r:
                break
            row = self._write_row(r)
            try:
                result = self.deliver(row["plan"], target["base"])
            except Unreachable as exc:
                self._down_steps += 1
                step = self.retry_steps[min(self._down_steps, len(self.retry_steps)) - 1]
                self._down_until = time.time() + step
                self._last_error = f"Faustus no responde en {target['base']}: {exc}"[:300]
                with self._lock:
                    self._db.execute("UPDATE writes SET attempts=attempts+1, error=? WHERE id=?", (self._last_error, row["id"]))
                    self._db.commit()
                break
            except Exception as exc:  # noqa: BLE001 - Rejected, or a row this code cannot handle: neither may block the queue
                reason = str(exc) if isinstance(exc, Rejected) else f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self._db.execute("UPDATE writes SET status='failed', attempts=attempts+1, error=?, done_ts=? WHERE id=?",
                                     (reason[:500], time.time(), row["id"]))
                    self._db.commit()
                continue
            self._down_steps, self._down_until, self._last_error, self._last_ok = 0, 0.0, "", time.time()
            status = "partial" if result.get("entities_pending") else "done"
            error = "este Faustus aún no admite crear entidades: la nota está escrita y la entidad se añadirá sola" if status == "partial" else ""
            with self._lock:
                self._db.execute("UPDATE writes SET status=?, attempts=attempts+1, error=?, result=?, done_ts=? WHERE id=?",
                                 (status, error, json.dumps(result, ensure_ascii=False), time.time(), row["id"]))
                self._db.execute("DELETE FROM writes WHERE id <= (SELECT MAX(id) FROM writes) - ? AND status IN ('done','failed')",
                                 (KEEP_WRITES,))
                self._db.commit()
            worked = True
            try:
                self.hub.events.emit("hub.brain.written", {"rule": row["rule"], "key": row["key"], "status": status,
                                                           "note": (result.get("note") or {}).get("path") or "",
                                                           "entity": (result.get("entity") or {}).get("name") or "",
                                                           "event_id": row["event_id"]}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        return worked

    def _requeue_partial(self, base: str) -> None:
        """Writes that left their entities for later go back to the queue once this Faustus can create them."""
        self._probe_at = time.time()
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM writes WHERE status='partial'").fetchone()[0]
        if not n:
            return
        try:
            if not self.client(base).entities_supported():
                return
        except Unreachable:
            return
        if self._requeue(("partial",)):
            with self._wake:
                self._kick = True
                self._wake.notify_all()

    def _requeue(self, statuses: tuple[str, ...], write_id: Optional[int] = None) -> int:
        """Back to the queue. Only the newest write of each thing goes back: replaying an older one after a newer one
        landed would put old text over new, so an older ``partial`` is closed and an older ``failed`` stays as it is."""
        marks = ",".join("?" for _ in statuses)
        n = 0
        with self._lock:
            if write_id is not None:
                rows = self._db.execute(f"SELECT id, key, status FROM writes WHERE id=? AND status IN ({marks})",
                                        (int(write_id), *statuses)).fetchall()
            else:
                rows = self._db.execute(f"SELECT id, key, status FROM writes WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
            for wid, key, status in rows:
                newest = self._db.execute("SELECT MAX(id) FROM writes WHERE key=?", (key,)).fetchone()[0]
                if status == "pending" or int(newest or 0) == int(wid):
                    self._db.execute("UPDATE writes SET status='pending', next_try=0, error='' WHERE id=?", (wid,))
                    n += 1
                elif status == "partial":
                    self._db.execute("UPDATE writes SET status='done', error='' WHERE id=?", (wid,))
            self._db.commit()
        return n

    def deliver(self, plan: dict[str, Any], base: str) -> dict[str, Any]:
        """Write one plan to the Brain at ``base`` and remember where it landed. Raises Unreachable / Rejected."""
        api = self.client(base)
        key = plan["key"]
        thing = self._thing(key) or {"entity_id": "", "entity_name": "", "note_path": "", "hashes": [], "log": [], "writes": 0}
        if thing.get("base") and thing["base"] != base:
            thing = {**thing, "entity_id": ""}          # another Faustus: its entity ids mean nothing here
        note, entity, links = plan.get("note"), plan.get("entity"), plan.get("links") or []
        result: dict[str, Any] = {"base": base}
        log = list(thing.get("log") or [])
        if note and note["mode"] == "append" and note["text"]:
            entry_key = hashlib.sha256(note["text"].encode("utf-8")).hexdigest()[:16]
            if not any(e.get("k") == entry_key for e in log):
                log.append({"k": entry_key, "when": (plan.get("origin") or {}).get("when") or "", "line": note["text"]})
            log = log[-MAX_LOG:]
        supported: Optional[bool] = None
        path = ""
        zone = str(thing.get("zone") or "")
        probe = None
        if entity or links:
            # ask for the route with the first thing it would create; None = this Faustus has no such route
            first = entity or {"name": links[0]["to"], "type": links[0]["type"]}
            probe = api.upsert_entity({"name": first["name"], "type": first["type"]})
            supported = probe is not None
        if note or (entity and not supported):
            path = thing.get("note_path") or note_path(plan)
            existing = api.read_note(path)
            previous, zone = zone, zone_text(plan, log)
            content = compose_note(existing, plan, zone, previous=previous,
                                   now_iso=datetime.now().astimezone().isoformat(timespec="seconds"))
            if existing != content:
                path = api.write_note(path, content)
            result["note"] = {"path": path, "created": existing is None, "written": existing != content,
                              "card": not note}
            result["url"] = f"{base}/brain?note={urllib.parse.quote(path)}"
        mentions = [f"note:{path}"] if path and note else []
        if supported:
            if entity:
                body = {"name": entity["name"], "type": entity["type"], "aliases": entity.get("aliases") or [],
                        "summary": entity.get("summary") or "", "mentions": mentions,
                        "relations": [{"rel": l["rel"], "to": l["to"], "to_type": l["type"]} for l in links]}
                if thing.get("entity_id") and thing.get("entity_name") and thing["entity_name"] != entity["name"]:
                    body["aliases"] = list(dict.fromkeys([*body["aliases"], thing["entity_name"]]))
                got = api.upsert_entity(body) or {}
                ent = got.get("entity") or {}
                result["entity"] = {"id": ent.get("id") or "", "name": ent.get("name") or entity["name"], "type": ent.get("type") or entity["type"],
                                    "created": bool((probe or {}).get("created")), "path": got.get("path") or ""}
                if not result.get("url") and got.get("path"):
                    result["url"] = f"{base}/brain?note={urllib.parse.quote(got['path'])}"
            else:
                for l in links:
                    api.upsert_entity({"name": l["to"], "type": l["type"], "mentions": mentions})
            result["links"] = [{"to": l["to"], "type": l["type"], "rel": l["rel"]} for l in links]
        elif entity or links:
            result["entities_pending"] = True
        h = plan_hash(plan)
        hashes = [x for x in (thing.get("hashes") or []) if x != h][-(MAX_LOG - 1):] + [h]
        with self._lock:
            self._db.execute(
                "INSERT INTO things (key, entity_id, entity_name, note_path, plan_hash, hashes, log, base, writes, updated, zone) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(key) DO UPDATE SET entity_id=excluded.entity_id, entity_name=excluded.entity_name, "
                "note_path=excluded.note_path, plan_hash=excluded.plan_hash, hashes=excluded.hashes, log=excluded.log, base=excluded.base, "
                "writes=things.writes+1, updated=excluded.updated, zone=excluded.zone",
                (key, (result.get("entity") or {}).get("id") or thing.get("entity_id") or "",
                 (entity or {}).get("name") or thing.get("entity_name") or "", path or thing.get("note_path") or "", h,
                 json.dumps(hashes), json.dumps(log, ensure_ascii=False), base, time.time(), zone))
            self._db.commit()
        return result

    def retry(self, write_id: Optional[int] = None) -> dict[str, Any]:
        """Put failed, partial or waiting writes back at the front of the queue (one, or all of them) and try now."""
        with self._wake:
            n = self._requeue(("failed", "partial", "pending"), write_id)
            self._down_until, self._down_steps = 0.0, 0
            self._kick = True
            self._wake.notify_all()
        return {"ok": True, "requeued": n, "counts": self.counts()}

    # -- rules, preview, status ---------------------------------------------------------------------
    def rules(self) -> list[dict[str, Any]]:
        """Every rule with a ``brain`` action, installed or recommended, with what it has written."""
        installed = {r["id"]: r for r in self.hub.rules.list()}
        factory = {r["id"]: r for r in factory_rules()}
        with self._lock:
            stats = {row[0]: row[1:] for row in self._db.execute(
                "SELECT rule_id, COUNT(*), SUM(status='pending'), SUM(status='failed'), MAX(ts) FROM writes GROUP BY rule_id").fetchall()}
        out: list[dict[str, Any]] = []
        for rid in list(installed) + [k for k in factory if k not in installed]:
            rule = installed.get(rid) or factory[rid]
            acts = brain_actions(rule)
            if not acts:
                continue
            n, pending, failed, last = stats.get(rid) or (0, 0, 0, None)
            out.append({"id": rid, "name": rule.get("name") or rid, "note": rule.get("note") or "", "when": rule.get("when") or {},
                        "then": acts, "installed": rid in installed, "enabled": bool(rule.get("enabled", True)) and rid in installed,
                        "recommended": rid in factory, "sample": (factory.get(rid) or {}).get("sample"),
                        "writes": int(n or 0), "pending": int(pending or 0), "failed": int(failed or 0), "last_ts": last,
                        "runs": int(rule.get("runs") or 0), "skipped": int(rule.get("skipped") or 0)})
        return out

    def set_rule(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        """Turn a brain rule on or off; a recommended one that is not installed is installed first."""
        if self.hub.rules.get(rule_id) is None:
            template = next((r for r in factory_rules() if r["id"] == rule_id), None)
            if template is None:
                return {"ok": False, "status": 404, "error": f"unknown rule: {rule_id}"}
            res = self.hub.rules.add({**template, "enabled": bool(enabled)})
            return res if not res.get("ok") else {"ok": True, "installed": True, "rule": res["rule"]}
        return self.hub.rules.update(rule_id, {"enabled": bool(enabled)})

    def _event_for(self, body: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        ev = body.get("event") if isinstance(body.get("event"), dict) else None
        if ev is None and body.get("event_id"):
            ev = self.hub.events.get(int(body["event_id"]))
        if ev is None:
            sample = body.get("sample") if isinstance(body.get("sample"), dict) else (samples().get(str(rule.get("id") or "")) or {})
            when = str(((rule.get("when") or {}).get("type")) or "manual.test")
            ev = {"type": sample.get("type") or when.split("|")[0], "data": sample.get("data") or {}}
        return {"id": ev.get("id"), "ts": float(ev.get("ts") or time.time()), "type": str(ev.get("type") or ""),
                "source": str(ev.get("source") or str(ev.get("type") or "").split(".")[0] or "ui"),
                "data": ev.get("data") if isinstance(ev.get("data"), dict) else {}}

    def test(self, body: dict[str, Any]) -> dict[str, Any]:
        """What a rule would write for an event (``write: true`` writes it and waits for the answer)."""
        from . import actions
        from .rules import rule_matches
        rule = body.get("rule") if isinstance(body.get("rule"), dict) else None
        rid = str(body.get("rule_id") or body.get("id") or "")
        if rule is None and rid:
            rule = self.hub.rules.get(rid) or next((r for r in factory_rules() if r["id"] == rid), None)
        if rule is None:
            return {"ok": False, "status": 404, "error": "falta la regla: rule_id (instalada o recomendada) o rule"}
        acts = brain_actions(rule)
        if not acts:
            return {"ok": False, "status": 400, "error": "esa regla no escribe en el Cerebro"}
        problems = [p for a in acts for p in validate_action(a)]
        if problems:
            return {"ok": False, "status": 400, "error": "; ".join(problems)}
        event = self._event_for(body, rule)
        ctx = actions.base_context(event=event, rule={"id": rule.get("id"), "name": rule.get("name")})
        target = self.target()
        out: dict[str, Any] = {"ok": True, "rule": rule.get("id"), "event": event, "matches": rule_matches(rule, event),
                               "target": target, "writes": []}
        for action in acts:
            plan = build_plan(action, ctx)
            if plan.get("skip"):
                out["writes"].append({"skipped": True, "reason": plan["skip"]})
                continue
            thing = self._thing(plan["key"]) or {}
            log = list(thing.get("log") or [])
            note = plan.get("note")
            if note and note["mode"] == "append" and note["text"] and not any(e.get("line") == note["text"] for e in log):
                log.append({"when": plan["origin"]["when"], "line": note["text"]})
            item = {"key": plan["key"], "effect": self.effect(plan), "entity": plan.get("entity"), "links": plan["links"],
                    "note": ({"path": thing.get("note_path") or note_path(plan), "mode": note["mode"], "date": note.get("date") or "",
                              "text": zone_text(plan, log)} if note else None),
                    "summary": describe_plan(plan)}
            if body.get("write"):
                item["written"] = self.submit(action, ctx, force=bool(body.get("force")), wait=float(body.get("wait_s") or 25.0))
            out["writes"].append(item)
        return out

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        target = self.target()
        if probe and target.get("base"):
            try:
                api = self.client(target["base"])
                info = api.status()
                target.update(reachable=True, notes=info.get("notes"), entities=info.get("entities"),
                              entities_api=api.entities_supported())
            except Unreachable as exc:
                target.update(reachable=False, error=str(exc)[:200])
        return {"ok": True, "target": target, "fixed_url": str(self._settings().get("faustus_url") or ""), "counts": self.counts(),
                "waiting_s": max(0, int(self._down_until - time.time())), "last_error": self._last_error,
                "last_ok": self._last_ok or None, "rules": self.rules(), "types": list(TYPES)}

    # -- HTTP -----------------------------------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/brain":
            return self.status(probe=req.q_bool("probe"))
        if req.path == "/api/brain/history":
            return {"ok": True, "writes": self.history(rule=str(req.q("rule") or ""), status=str(req.q("status") or ""),
                                                      limit=req.q_int("limit", 50)), "counts": self.counts()}
        return None

    def post(self, req: Request) -> Optional[Any]:
        if not req.path.startswith("/api/brain/"):
            return None
        if not req.caller():
            return {"ok": False, "status": 401, "error": "a family bearer token is required"}
        b = req.body or {}
        if req.path == "/api/brain/test":
            return self.test(b)
        if req.path == "/api/brain/retry":
            return self.retry(int(b["id"]) if str(b.get("id") or "").isdigit() else None)
        if req.path == "/api/brain/target":
            return self.set_target(str(b.get("url") or ""))
        if req.path.startswith("/api/brain/rules/"):
            return self.set_rule(urllib.parse.unquote(req.path[len("/api/brain/rules/"):]), bool(b.get("enabled", True)))
        return None

    # -- agent tools -------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {"name": "hub_brain_status",
             "description": "Rules that write the family's events into Faustus's Brain: which exist, which are on, what each has "
                            "written, where Faustus is (PC or Spark) and what waits in the queue. Keywords: cerebro, brain, "
                            "entidades, notas, reglas, bus. probe=true also asks Faustus whether it answers.",
             "inputSchema": {"type": "object", "properties": {"probe": {"type": "boolean", "default": False}},
                             "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_brain_rule_set",
             "description": "Turn a Brain rule on or off (a recommended one that is not installed is installed). To write a new "
                            "one use hub_rule_add with a then of [{kind: 'brain', key, entity: {name, type}, note: {folder, title, "
                            "text, mode: replace|append, date}, links: [{to, type, rel}]}]; ${event.data.x|event.data.y} in strings.",
             "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "enabled": {"type": "boolean", "default": True}},
                             "required": ["id"], "additionalProperties": False}},
            {"name": "hub_brain_test",
             "description": "What a Brain rule would write for an event: the entity, the note text and path, the links, and whether "
                            "it would create, update or change nothing. Give rule_id (or a whole rule) and an event {type, data} "
                            "(or event_id of a past one; without either, the rule's sample). write=true writes it for real.",
             "inputSchema": {"type": "object", "properties": {
                 "rule_id": {"type": "string"}, "rule": {"type": "object"},
                 "event": {"type": "object", "description": "{type, source?, data}"}, "event_id": {"type": "integer"},
                 "write": {"type": "boolean", "default": False}, "force": {"type": "boolean", "default": False}},
                 "additionalProperties": False}},
            {"name": "hub_brain_history",
             "description": "What the Brain rules have written: per write the rule, the event, the entity and note, the state "
                            "(done, pending, partial, failed) and the link to the note in Faustus.",
             "inputSchema": {"type": "object", "properties": {
                 "rule": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "done", "partial", "failed"]},
                 "limit": {"type": "integer", "default": 30}}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_brain_retry",
             "description": "Try again now the Brain writes that failed, wait for Faustus or lack their entities (one id, or all).",
             "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "additionalProperties": False}},
            {"name": "hub_brain_target",
             "description": "Pin the Faustus the Brain rules write to (url), or follow the placement again (empty url).",
             "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "default": ""}}, "additionalProperties": False}},
        ]

    def handlers(self) -> dict[str, Any]:
        def clean(res: dict[str, Any]) -> dict[str, Any]:
            res.pop("status", None) if isinstance(res.get("status"), int) else None
            return res

        return {
            "hub_brain_status": lambda a: self.status(probe=bool(a.get("probe"))),
            "hub_brain_rule_set": lambda a: clean(self.set_rule(str(a.get("id") or ""), bool(a.get("enabled", True)))),
            "hub_brain_test": lambda a: clean(self.test(a)),
            "hub_brain_history": lambda a: {"ok": True, "counts": self.counts(), "writes": [
                {k: w[k] for k in ("id", "ts", "rule", "rule_name", "event_id", "event_type", "key", "status", "error", "summary", "url")}
                for w in self.history(rule=str(a.get("rule") or ""), status=str(a.get("status") or ""), limit=int(a.get("limit") or 30))]},
            "hub_brain_retry": lambda a: self.retry(int(a["id"]) if a.get("id") is not None else None),
            "hub_brain_target": lambda a: clean(self.set_target(str(a.get("url") or ""))),
        }


def describe_plan(plan: dict[str, Any]) -> str:
    """One line for the history: what a plan writes."""
    parts: list[str] = []
    ent, note = plan.get("entity"), plan.get("note")
    if ent:
        parts.append(f"{TYPE_NAMES.get(ent.get('type') or 'other', 'entidad')} «{ent.get('name')}»")
    if note:
        parts.append(f"nota {note.get('folder')}/{note.get('title')}")
    if plan.get("links"):
        parts.append("enlaces: " + ", ".join(l.get("to") or "" for l in plan["links"]))
    return " · ".join(parts)


__all__ = ["BrainFacet", "BrainClient", "build_plan", "factory_rules", "validate_action", "brain_actions", "render"]
