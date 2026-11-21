"""Draft pass («pasada 0») of an order: the whole chain in draft, in variants, and the person's early gate on structure.

Before anything expensive runs, an order can ask for a draft pass: two to four structural **variants** of the whole
chain (for the cartographer: unrolling the map, turning it over with one hand, passing it from one hand to the other),
each made with every station's draft mode (``hub_station_drafts``: few-step low-resolution images, a primitive blocking
already split in parts, an animatic of key frames without in-betweens, an outline in text). Each variant gathers its
pieces (ordinary pieces of the order, registered with ``piece_add``) with the **anchor** each one gives the expensive
pass: ``blocking``, ``depth``, ``normals``, ``parts``, ``keyframe_start``, ``keyframe_end``, ``keyframe``,
``composition``, ``outline``, ``animatic`` or any short slug.

* ``draft_pass_open``: the variants, and optionally each variant's jobs (Ágora tasks of the order, branch
  ``pasada0-<key>``, made through ``plan_set``). A baseline (the time and GPU of a full pass) can be noted to compare.
* ``draft_variant_set``: attach pieces with their anchor, note the measures (seconds, GPU seconds, GPU, engine, steps)
  and mark the variant ready.
* ``draft_pass_note_set``: replace or clear the note for the whole pass.
* ``draft_gate_open``: the early gate. The person's page shows every variant end to end, side by side; the order waits.
* ``draft_decide``: only the person's own pages. Keep one variant (the others are discarded) or discard a variant.
  Nothing is deleted: a discarded variant stays with its note and its pieces as cancelled, without being sent back
  to any station (a draft is not a defect). Keeping a variant keeps its pieces, so ``job`` hands them to every later
  job of the order like any kept piece.
* ``draft_anchors``: what the expensive pass starts from: the kept variant's pieces grouped by anchor, which guide
  each later station uses (image ← depth, normals, composition; video ← first and last key frame; split ← blocking and
  parts) and the ``derives_from`` list to give ``piece_add`` so the new pieces record that they come from it.
* ``draft_passes``: read the passes of an order (or what waits for the person, every space).

Writes return ``undo`` for ``order_undo``: these tables join the orders' journal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from . import agora_orders as orders
from . import order_figure_lineage

PASS_STATES = ("drafting", "shown", "decided")
VARIANT_STATES = ("drafting", "ready", "kept", "discarded")
#: What each anchor guides in the expensive pass. Any other slug is accepted and listed under «other».
GUIDES = {
    "image": ("depth", "normals", "composition", "blocking_color"),
    "video": ("keyframe_start", "keyframe_end", "keyframe", "animatic"),
    "split": ("blocking", "parts"),
    "text": ("outline",),
}
MAX_VARIANTS = 4

DRAFTS_MIGRATION = """
    CREATE TABLE IF NOT EXISTS agora_draft_passes (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
        space_id TEXT NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'drafting',
        baseline TEXT NOT NULL DEFAULT '{}', opened_by TEXT NOT NULL, shown_by TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, shown REAL, decided REAL);
    CREATE INDEX IF NOT EXISTS agora_draft_passes_order ON agora_draft_passes(order_id, id);
    CREATE TABLE IF NOT EXISTS agora_draft_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL, space_id TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL,
        idea TEXT NOT NULL DEFAULT '', jobs TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'drafting',
        measures TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', decision_note TEXT NOT NULL DEFAULT '',
        position INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS agora_draft_variants_pass ON agora_draft_variants(pass_id, position);
    CREATE TABLE IF NOT EXISTS agora_draft_pieces (variant_id INTEGER NOT NULL, piece_id INTEGER NOT NULL,
        anchor TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(variant_id, piece_id));
"""

# The orders' journal restores these rows too, so order_undo undoes a draft write like any other.
orders._KEYS.update({"agora_draft_passes": "id", "agora_draft_variants": "id", "agora_draft_pieces": None})

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")
_ANCHOR = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_MEASURE_KEYS = ("seconds", "gpu_seconds", "gpu", "engine", "steps", "resolution", "device", "note")


def _err(message: str, status: int = 400, **extra: Any) -> Exception:
    return orders._err(message, status, **extra)


def _measures(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise _err("measures: an object {seconds, gpu_seconds, gpu, engine, steps, resolution, device, note}")
    out: dict[str, Any] = {}
    for key, v in value.items():
        if key not in _MEASURE_KEYS:
            raise _err(f"measures: unknown field {key}; use {', '.join(_MEASURE_KEYS)}")
        if key in ("seconds", "gpu_seconds"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise _err(f"measures.{key}: a number of seconds")
            out[key] = round(float(v), 2)
        else:
            out[key] = orders._txt(v, 300, f"measures.{key}") if not isinstance(v, (int, float)) else v
    return out


def _pass_row(self: Any, pass_id: Any) -> dict[str, Any]:
    pid = orders._int(pass_id, "pass_id")
    row = self.db.one("SELECT * FROM agora_draft_passes WHERE id=?", (pid,))
    if not row or row["space_id"] != self.space_id:
        raise _err(f"no draft pass #{pid} in space {self.space_id}", 404)
    return dict(row)


def _variant_row(self: Any, variant_id: Any) -> dict[str, Any]:
    vid = orders._int(variant_id, "variant_id")
    row = self.db.one("SELECT * FROM agora_draft_variants WHERE id=?", (vid,))
    if not row or row["space_id"] != self.space_id:
        raise _err(f"no draft variant #{vid} in space {self.space_id}", 404)
    return dict(row)


def _signature(row: dict[str, Any], pieces: list[dict[str, Any]]) -> str:
    """What the person judges in a variant: its title and idea and, for each piece, its id, anchor and the revision
    and bytes it shows. The page sends it back with the decision, so a variant changed while the person looked at it
    has to be shown again before it can be kept. Measures, notes and checks are not part of it."""
    shown = [row.get("title") or "", row.get("idea") or "",
             sorted([int(p["id"]), p.get("anchor") or "", int(p.get("revision") or 1), p.get("sha256") or "",
                     p.get("path") or "", p.get("url") or ""] for p in pieces)]
    return hashlib.sha256(json.dumps(shown, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _variant_dict(self: Any, row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["jobs"] = orders._load(row.get("jobs"), [])
    out["measures"] = orders._load(row.get("measures"), {})
    pieces = []
    for link in self.db.query("SELECT * FROM agora_draft_pieces WHERE variant_id=? ORDER BY position, piece_id", (row["id"],)):
        p = self.db.one("SELECT * FROM agora_pieces WHERE id=?", (link["piece_id"],))
        if p:
            pieces.append({**orders._piece_dict(self, dict(p)), "anchor": link["anchor"]})
    out["pieces"] = pieces
    out["signature"] = _signature(row, pieces)
    out["tasks"] = [dict(t) for t in self.db.query(
        "SELECT id, title, status, owner FROM tasks WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(out["jobs"])),
        tuple(out["jobs"]))] if out["jobs"] else []
    return out


def _pass_dict(self: Any, row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["baseline"] = orders._load(row.get("baseline"), {})
    out["variants"] = [_variant_dict(self, dict(v)) for v in self.db.query(
        "SELECT * FROM agora_draft_variants WHERE pass_id=? ORDER BY position, id", (row["id"],))]
    seconds = [v["measures"].get("seconds") for v in out["variants"] if v["measures"].get("seconds") is not None]
    gpu = [v["measures"].get("gpu_seconds") for v in out["variants"] if v["measures"].get("gpu_seconds") is not None]
    out["totals"] = {"seconds": round(sum(seconds), 2) if seconds else None,
                     "gpu_seconds": round(sum(gpu), 2) if gpu else None,
                     "variants": len(out["variants"])}
    base = out["baseline"]
    if out["totals"]["seconds"] and base.get("seconds"):
        out["totals"]["share_of_baseline"] = round(out["totals"]["seconds"] / float(base["seconds"]), 3)
    if out["totals"]["gpu_seconds"] and base.get("gpu_seconds"):
        out["totals"]["gpu_share_of_baseline"] = round(out["totals"]["gpu_seconds"] / float(base["gpu_seconds"]), 3)
    kept = [v for v in out["variants"] if v["status"] == "kept"]
    out["kept_variant"] = kept[0]["id"] if kept else None
    order = self.db.one("SELECT id, title, space_id, status FROM agora_orders WHERE id=?", (row["order_id"],))
    out["order"] = dict(order) if order else {}
    return out


def _merge_journal(self: Any, journal: Any, token: Optional[str]) -> None:
    """Fold the journal entry of a nested write (plan_set) into this call's, so one undo puts back both."""
    if not token:
        return
    entry = self.db.one("SELECT changes FROM agora_order_journal WHERE token=?", (token,))
    changes = orders._load(entry["changes"] if entry else "", {})
    journal.rows[:0] = changes.get("rows") or []
    journal.created_tasks.extend(changes.get("created_tasks") or [])
    journal.task_status.extend(changes.get("task_status") or [])
    journal.task_deps.extend(changes.get("task_deps") or [])
    self.db.execute("DELETE FROM agora_order_journal WHERE token=?", (token,))


# ---- writes by agents -----------------------------------------------------------------------------------------

def _visual_jobs(idea: str) -> list[dict[str, Any]]:
    return [
        {"key": "outline", "title": "Esquema del gesto", "station": "writer", "produces": ["outline"],
         "body": f"{idea}\n\nFija el gesto, la cámara y los detalles del expediente que hay que conservar.".strip()},
        {"key": "blocking", "title": "Bloqueo en volumen", "station": "vulcan", "after": ["outline"],
         "produces": ["blocking", "image"],
         "body": "Crea el bloqueo según el esquema con blocking_build. Entrega el GLB y el color, la profundidad "
                 "y las normales por clave, con la misma cámara."},
        {"key": "keyframes", "title": "Claves guiadas por el bloqueo", "station": "prospero", "after": ["blocking"],
         "produces": ["image"],
         "body": "Usa el color y la profundidad de cada pose del bloqueo para guiar su clave de imagen. "
                 "Contrasta el gesto, el encuadre y los accesorios con el esquema."},
        {"key": "shot", "title": "Plano de vídeo en borrador", "station": "prospero", "after": ["keyframes"],
         "produces": ["video"],
         "body": "Monta las claves en una animática corta. Comprueba que se lee el gesto en el orden previsto."},
        {"key": "figure", "title": "Cierre de figura en borrador", "station": "phidias",
         "after": ["blocking", "keyframes"], "produces": ["mesh"],
         "body": "Congela la pose elegida del bloqueo y comprueba que se sostiene en la peana. "
                 "Importa esa revisión, ejecuta el esculpido y el cierre, y comprueba apoyos, medidas y topología."},
    ]


def draft_pass_open(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Open a draft pass of an order with its variants (key, title, idea and, optionally, the jobs of each variant)."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    variants = a.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise _err(f"variants: a list of 1 to {MAX_VARIANTS} variants {{key, title, idea, jobs}}")
    template = a.get("template")
    if template not in (None, "visual"):
        raise _err("template must be visual / La plantilla debe ser visual")
    keys = []
    for v in variants:
        if not isinstance(v, dict):
            raise _err("each variant is an object {key, title, idea, jobs}")
        key = str(v.get("key") or "").strip().lower()
        if not _KEY.match(key) or key in keys:
            raise _err(f"variant key {key!r}: a short unique slug (a, b, c, desenrollar…)")
        keys.append(key)
    baseline = _measures(a.get("baseline"))
    with self.db.tx():
        order_row = orders._order_row(self, a.get("order_id"))
        oid = order_row["id"]
        journal = orders._Journal(self, agent, "draft_pass_open")
        now = orders._now(self)
        title = orders._txt(a.get("title"), 200, "title") or f"Pasada 0 de «{order_row['title']}»"
        pid = self.db.insert("agora_draft_passes", {
            "order_id": oid, "space_id": self.space_id, "title": title, "note": orders._txt(a.get("note"), orders.TEXT_MAX, "note"),
            "status": "drafting", "baseline": orders._dump(baseline), "opened_by": agent, "created": now})
        journal.created("agora_draft_passes", pid)
        made = []
        for i, (v, key) in enumerate(zip(variants, keys)):
            jobs: list[int] = []
            idea = orders._txt(v.get("idea"), 4000, "idea")
            variant_jobs = v.get("jobs")
            if template == "visual" and "jobs" not in v:
                variant_jobs = _visual_jobs(idea)
            if variant_jobs:
                if not isinstance(variant_jobs, list):
                    raise _err(f"variant {key}: jobs is a list of plan jobs (as in plan_set)")
                plan_jobs = []
                for n, j in enumerate(variant_jobs):
                    if not isinstance(j, dict) or j.get("task_id"):
                        raise _err(f"variant {key}: each job is a new plan job {{title, station, produces, check, after}}")
                    plan_jobs.append({**j, "key": f"{key}:{j.get('key') or n}", "branch": f"pasada0-{key}",
                                      "title": f"[Pasada 0 · {key}] {j.get('title') or ''}".strip(),
                                      "after": [f"{key}:{x}" if isinstance(x, str) else x for x in (j.get("after") or [])],
                                      "body": ((j.get("body") or "") + "\n\nModo borrador: entrega el borrador de tu puesto "
                                               "(hub_station_drafts dice cuál es y cuánto tarda) y regístralo en la variante "
                                               f"«{key}» con hub_agora_draft_variant_set.").strip()})
                res = orders.plan_set(self, {"agent": agent, "_person": a.get("_person"), "order_id": oid, "jobs": plan_jobs})
                _merge_journal(self, journal, res.get("undo"))
                jobs = [int(res["keys"][j["key"]]) for j in plan_jobs]
            vid = self.db.insert("agora_draft_variants", {
                "pass_id": pid, "order_id": oid, "space_id": self.space_id, "key": key,
                "title": orders._txt(v.get("title"), 200, "title") or key,
                "idea": idea, "jobs": orders._dump(jobs), "status": "drafting",
                "measures": orders._dump(_measures(v.get("measures"))), "position": i, "created": now, "updated": now})
            journal.created("agora_draft_variants", vid)
            made.append(vid)
        token = journal.save()
    self._bump("agora.order.draft", {"order_id": oid, "pass_id": pid, "variants": made, "by": agent})
    return {"ok": True, "draft_pass": _pass_dict(self, _pass_row(self, pid)), "undo": token}


def draft_pass_note_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Replace or clear the note for a whole draft pass."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    if "note" not in a or not isinstance(a["note"], str):
        raise _err("note must be text; send an empty string to clear it")
    note = orders._txt(a["note"], orders.TEXT_MAX, "note")
    with self.db.tx():
        draft = _pass_row(self, a.get("pass_id"))
        journal = orders._Journal(self, agent, "draft_pass_note_set")
        journal.before("agora_draft_passes", draft["id"])
        self.db.execute("UPDATE agora_draft_passes SET note=? WHERE id=?", (note, draft["id"]))
        journal.rows[0]["after"] = {**draft, "note": note}
        token = journal.save()
    self._bump("agora.order.draft", {"order_id": draft["order_id"], "pass_id": draft["id"], "by": agent})
    return {"ok": True, "draft_pass": _pass_dict(self, _pass_row(self, draft["id"])), "undo": token}


def draft_variant_set(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Attach pieces to a variant with their anchor, note its measures and say whether it is ready to be shown."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    with self.db.tx():
        variant = _variant_row(self, a.get("variant_id"))
        if variant["status"] in ("kept", "discarded"):
            raise _err(f"variant #{variant['id']} is already decided ({variant['status']})", 409)
        draft = _pass_row(self, variant["pass_id"])
        before = _variant_dict(self, variant)["signature"]
        journal = orders._Journal(self, agent, "draft_variant_set")
        pieces = a.get("pieces") or []
        if not isinstance(pieces, list):
            raise _err("pieces: a list of {piece, anchor}")
        position = int(self.db.scalar("SELECT COALESCE(MAX(position), 0) FROM agora_draft_pieces WHERE variant_id=?",
                                      (variant["id"],), 0) or 0)
        for item in pieces:
            item = {"piece": item} if isinstance(item, int) else item
            if not isinstance(item, dict):
                raise _err("each piece is {piece, anchor}")
            pid = orders._int(item.get("piece"), "piece")
            anchor = str(item.get("anchor") or "").strip().lower()
            if anchor and not _ANCHOR.match(anchor):
                raise _err(f"anchor {anchor!r}: a short slug (depth, normals, blocking, keyframe_start…)")
            p = self.db.one("SELECT order_id FROM agora_pieces WHERE id=?", (pid,))
            if not p or p["order_id"] != variant["order_id"]:
                raise _err(f"piece #{pid} is not a piece of order #{variant['order_id']}", 404)
            where = {"variant_id": variant["id"], "piece_id": pid}
            journal.before("agora_draft_pieces", None, where=where)
            if self.db.one("SELECT 1 AS x FROM agora_draft_pieces WHERE variant_id=? AND piece_id=?", (variant["id"], pid)):
                self.db.execute("UPDATE agora_draft_pieces SET anchor=? WHERE variant_id=? AND piece_id=?",
                                (anchor, variant["id"], pid))
            else:
                position += 1
                self.db.insert("agora_draft_pieces", {**where, "anchor": anchor, "position": position})
        for pid in orders._ids(a.get("remove"), "remove"):
            where = {"variant_id": variant["id"], "piece_id": pid}
            journal.before("agora_draft_pieces", None, where=where)
            self.db.execute("DELETE FROM agora_draft_pieces WHERE variant_id=? AND piece_id=?", (variant["id"], pid))
        changes: dict[str, Any] = {}
        if "measures" in a:
            changes["measures"] = orders._dump({**orders._load(variant["measures"], {}), **_measures(a.get("measures"))})
        if "note" in a:
            changes["note"] = orders._txt(a.get("note"), orders.TEXT_MAX, "note")
        if "status" in a:
            status = str(a.get("status") or "").lower()
            if status not in ("drafting", "ready"):
                raise _err("status: drafting or ready (keeping or discarding is the person's decision)")
            if status == "ready" and not self.db.scalar("SELECT COUNT(*) FROM agora_draft_pieces WHERE variant_id=?",
                                                        (variant["id"],), 0):
                raise _err(f"variant «{variant['key']}» has no pieces yet: attach them before marking it ready")
            changes["status"] = status
        if changes:
            journal.before("agora_draft_variants", variant["id"])
            changes["updated"] = orders._now(self)
            self.db.execute(f"UPDATE agora_draft_variants SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                            (*changes.values(), variant["id"]))
        token = journal.save()
    self._bump("agora.order.draft", {"order_id": variant["order_id"], "pass_id": draft["id"], "variant": variant["id"], "by": agent})
    out = _variant_dict(self, _variant_row(self, variant["id"]))
    result = {"ok": True, "variant": out, "undo": token}
    if draft["status"] == "shown":
        result["shown_again"] = out["signature"] != before
        if result["shown_again"]:
            result["note"] = ("The early gate is open: the person's page shows this variant again, and a decision taken "
                              "on what it showed before is refused until they have seen it.")
    return result


def draft_gate_open(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Show every variant of a draft pass to the person, end to end, for one early decision. The order waits."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    with self.db.tx():
        draft = _pass_row(self, a.get("pass_id"))
        if draft["status"] != "drafting":
            raise _err(f"draft pass #{draft['id']} is already {draft['status']}", 409)
        rows = [dict(v) for v in self.db.query("SELECT * FROM agora_draft_variants WHERE pass_id=? ORDER BY position", (draft["id"],))]
        not_ready = [v["key"] for v in rows if v["status"] != "ready"]
        if not_ready:
            raise _err("every variant must be ready (with its pieces) before the early gate: " + ", ".join(not_ready), 409,
                       error_code="variants_not_ready", variants=not_ready)
        journal = orders._Journal(self, agent, "draft_gate_open")
        journal.before("agora_draft_passes", draft["id"])
        note = a.get("note")
        self.db.execute("UPDATE agora_draft_passes SET status='shown', shown=?, shown_by=?" + (", note=?" if note else "") +
                        " WHERE id=?", (orders._now(self), agent, *([orders._txt(note, orders.TEXT_MAX, "note")] if note else []), draft["id"]))
        orders._set_status(self, draft["order_id"], "waiting", journal, only_from=("open", "producing"))
        token = journal.save()
    self._bump("agora.order.gate", {"order_id": draft["order_id"], "draft_pass": draft["id"], "by": agent})
    return {"ok": True, "draft_pass": _pass_dict(self, _pass_row(self, draft["id"])), "undo": token}


# ---- the person's decision ------------------------------------------------------------------------------------

def _settle(self: Any, journal: Any, variant: dict[str, Any], status: str, note: str, situation: dict[str, Any],
            decision: str, shared: list[int]) -> list[int]:
    """Keep or discard one variant: its state, its note, its pieces and the decision record. Nothing is deleted and
    nothing is sent back to a station. A piece that another variant still uses (kept, or not decided yet) is not
    discarded with this one: it follows the variant that keeps it, whatever the order of the decisions (listed in
    ``shared``). It is cancelled when the last variant using it is discarded."""
    now = orders._now(self)
    journal.before("agora_draft_variants", variant["id"])
    self.db.execute("UPDATE agora_draft_variants SET status=?, decision_note=?, updated=? WHERE id=?",
                    (status, note, now, variant["id"]))
    stale: list[int] = []
    for link in self.db.query("SELECT piece_id FROM agora_draft_pieces WHERE variant_id=?", (variant["id"],)):
        pid = int(link["piece_id"])
        piece = self.db.one("SELECT status FROM agora_pieces WHERE id=?", (pid,))
        if not piece:
            continue
        if status == "discarded" and self.db.scalar(
                "SELECT COUNT(*) FROM agora_draft_pieces l JOIN agora_draft_variants v ON v.id=l.variant_id "
                "WHERE l.piece_id=? AND v.id<>? AND v.status<>'discarded'", (pid, variant["id"]), 0):
            if pid not in shared:
                shared.append(pid)
            continue
        journal.before("agora_pieces", pid)
        if status == "kept" and piece["status"] in ("provisional", "checked"):
            self.db.execute("UPDATE agora_pieces SET status='kept', updated=? WHERE id=?", (now, pid))
        elif status == "discarded" and piece["status"] != "cancelled":
            self.db.execute("UPDATE agora_pieces SET status='cancelled', stale_because=?, updated=? WHERE id=?",
                            (f"variante «{variant['key']}» descartada en la pasada 0", now, pid))
            stale += orders._stale_below(self, journal, pid, variant["order_id"])
        self.db.insert("agora_order_decisions", {
            "order_id": variant["order_id"], "gate_id": 0, "piece_id": pid, "space_id": self.space_id,
            "decision": decision, "note": note, "situation": orders._dump(situation), "created": now})
    return stale


def draft_decide(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The person's early decision: keep one variant (the others still undecided are discarded) or discard one.
    Only the person's own pages may call it."""
    if not a.get("_person"):
        raise _err("only the person decides the early gate, from their own page", 403)
    agent = self._agent(a.get("agent"), allow_person=True)
    decision = str(a.get("decision") or "").lower()
    if decision not in orders.DECISIONS:
        raise _err("decision is keep or cancel")
    note = orders._txt(a.get("note"), orders.TEXT_MAX, "note")
    seen = str(a.get("signature") or "").strip()
    with self.db.tx():
        variant = _variant_row(self, a.get("variant_id"))
        draft = _pass_row(self, variant["pass_id"])
        if a.get("pass_id") is not None and draft["id"] != orders._int(a["pass_id"], "pass_id"):
            raise _err("the variant does not belong to this draft pass", 404)
        if draft["status"] != "shown":
            raise _err(f"draft pass #{draft['id']} is not waiting for a decision ({draft['status']})", 409)
        if variant["status"] in ("kept", "discarded"):
            raise _err(f"variant «{variant['key']}» is already {variant['status']}", 409)
        current = _variant_dict(self, variant)
        if not seen:
            raise _err("signature: the variant's signature as the page showed it")
        if seen != current["signature"]:
            raise _err(f"variant «{variant['key']}» changed after it was shown: show it again before deciding", 409,
                       error_code="variant_changed", variant_id=variant["id"], signature=current["signature"])
        for p in current["pieces"]:
            if not orders._bytes_ok(self, orders._piece_row(self, p["id"])):
                raise _err(f"piece #{p['id']}: its file changed since it was registered and no copy of that revision is "
                           "kept; the station has to register it again", 409, error_code="piece_changed", piece_id=p["id"])
        rows = [dict(v) for v in self.db.query("SELECT * FROM agora_draft_variants WHERE pass_id=? ORDER BY position", (draft["id"],))]
        order_row = orders._order_row(self, draft["order_id"])
        situation = {"decided_by": agent, "order": {"id": order_row["id"], "title": order_row["title"], "status": order_row["status"]},
                     "deadline": order_row["deadline"], "draft_pass": {"id": draft["id"], "title": draft["title"]},
                     "variant": {"id": variant["id"], "key": variant["key"], "title": variant["title"]},
                     "alternatives": [{"id": v["id"], "key": v["key"], "title": v["title"], "status": v["status"]} for v in rows]}
        journal = orders._Journal(self, agent, "draft_decide")
        result: dict[str, Any] = {"kept": None, "discarded": [], "stale": [], "shared": []}
        if decision == "keep":
            result["stale"] += _settle(self, journal, variant, "kept", note, situation, "keep", result["shared"])
            result["kept"] = variant["id"]
            for other in rows:
                if other["id"] != variant["id"] and other["status"] not in ("kept", "discarded"):
                    why = f"Descartada al conservar «{variant['title']}»." + (f" {note}" if note else "")
                    result["stale"] += _settle(self, journal, other, "discarded", why, situation, "cancel", result["shared"])
                    result["discarded"].append(other["id"])
        else:
            result["stale"] += _settle(self, journal, variant, "discarded", note, situation, "cancel", result["shared"])
            result["discarded"].append(variant["id"])
        result["shared"] = [p for p in result["shared"]
                            if self.db.scalar("SELECT status FROM agora_pieces WHERE id=?", (p,), "") != "cancelled"]
        left = self.db.scalar("SELECT COUNT(*) FROM agora_draft_variants WHERE pass_id=? AND status NOT IN ('kept','discarded')",
                              (draft["id"],), 0)
        if not left:
            journal.before("agora_draft_passes", draft["id"])
            self.db.execute("UPDATE agora_draft_passes SET status='decided', decided=? WHERE id=?", (orders._now(self), draft["id"]))
            busy = self.db.scalar("SELECT COUNT(*) FROM agora_gates WHERE order_id=? AND status='open'", (draft["order_id"],), 0)
            busy += self.db.scalar("SELECT COUNT(*) FROM agora_draft_passes WHERE order_id=? AND status='shown'", (draft["order_id"],), 0)
            if not busy:
                orders._set_status(self, draft["order_id"], "producing", journal, only_from=("waiting",))
    self._bump("agora.order.decided", {"order_id": draft["order_id"], "draft_pass": draft["id"], "decision": decision,
                                       "variant": variant["id"]})
    return {"ok": True, **result, "draft_pass": _pass_dict(self, _pass_row(self, draft["id"]))}


# ---- reading --------------------------------------------------------------------------------------------------

def draft_passes(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Draft passes of one order, or (without order_id) those waiting for the person in this space or every space."""
    all_spaces = str(a.get("all_spaces", "")).lower() in ("1", "true", "yes") or a.get("all_spaces") is True
    oid = orders._int(a.get("order_id"), "order_id", required=False)
    if oid:
        orders._order_row(self, oid)
        rows = self.db.query("SELECT * FROM agora_draft_passes WHERE order_id=? ORDER BY id DESC", (oid,))
    else:
        status = str(a.get("status") or "shown").lower()
        if status not in PASS_STATES:
            raise _err(f"status must be one of {', '.join(PASS_STATES)}")
        rows = self.db.query("SELECT * FROM agora_draft_passes WHERE status=?" + ("" if all_spaces else " AND space_id=?") +
                             " ORDER BY created", (status,) if all_spaces else (status, self.space_id))
    return {"ok": True, "draft_passes": [_pass_dict(self.for_space(r["space_id"]), dict(r)) for r in rows]}


def draft_anchors(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """What the expensive pass of an order starts from: the kept variant's pieces by anchor, the guide each later
    station takes from them, and the derives_from to give piece_add."""
    oid = orders._int(a.get("order_id"), "order_id", required=False)
    tid = orders._int(a.get("task_id"), "task_id", required=False)
    if not oid and tid:
        job = self.db.one("SELECT order_id FROM agora_order_jobs WHERE task_id=?", (tid,))
        if not job:
            raise _err(f"task #{tid} is not a job of any order", 404)
        oid = int(job["order_id"])
    if not oid:
        raise _err("give order_id or the task_id of one of its jobs")
    order_row = orders._order_row(self, oid)
    kept = self.db.one("SELECT v.* FROM agora_draft_variants v JOIN agora_draft_passes p ON p.id=v.pass_id "
                       "WHERE v.order_id=? AND v.status='kept' ORDER BY p.id DESC, v.id DESC LIMIT 1", (oid,))
    if not kept:
        waiting = self.db.scalar("SELECT COUNT(*) FROM agora_draft_passes WHERE order_id=? AND status='shown'", (oid,), 0)
        return {"ok": True, "order_id": oid, "variant": None,
                "waiting": bool(waiting),
                "note": "La pasada 0 espera la decisión de la persona en la puerta temprana." if waiting else
                        "Esta orden no tiene ninguna variante conservada."}
    variant = _variant_dict(self, dict(kept))
    anchors: dict[str, list[dict[str, Any]]] = {}
    for p in variant["pieces"]:
        anchors.setdefault(p["anchor"] or "other", []).append(
            {"piece": p["id"], "title": p["title"], "kind": p["kind"], "path": p["path"], "stored": p.get("stored") or "",
             "url": p["url"], "previews": p["previews"], "status": p["status"]})
    guides = {name: {slug: anchors[slug] for slug in slugs if slug in anchors} for name, slugs in GUIDES.items()}
    known = {s for slugs in GUIDES.values() for s in slugs}
    guides["other"] = {slug: items for slug, items in anchors.items() if slug not in known}
    derives = [{"piece": p["id"], "role": "material" if p["anchor"] in GUIDES["split"] else "reference",
                "note": f"pasada 0 · variante «{variant['key']}» · {p['anchor'] or 'pieza'}"} for p in variant["pieces"]]
    return {"ok": True, "order_id": oid, "order_title": order_row["title"],
            "variant": {k: variant[k] for k in ("id", "pass_id", "key", "title", "idea", "measures", "decision_note")},
            "anchors": anchors, "guides": {k: v for k, v in guides.items() if v}, "derives_from": derives}


def _figure_source(self: Any, reference: dict[str, Any], model_piece: Any) -> dict[str, Any]:
    """Resolve the GLB, offer the modelling step, or give explicit choices."""
    eligible = order_figure_lineage.eligible_model
    order_id = reference["order_id"]
    if model_piece is not None:
        piece = orders._piece_row(self, model_piece)
        if piece["order_id"] != order_id:
            raise _err("Choose a model from this order. / Elige un modelo de esta orden.", 404)
        if eligible(piece):
            return piece
        candidates = []
    else:
        lineage = list(orders._figure_materials(self, reference, same_order=True))
        linked = [p for p in lineage if eligible(p)]
        if len(lineage) == 1 and linked:
            return linked[0]
        placeholders = ", ".join("?" for _ in orders.ARTICULATED_KINDS)
        candidates = linked or [dict(p) for p in self.db.query(
            f"SELECT * FROM agora_pieces WHERE order_id=? AND kind IN ({placeholders}) ORDER BY id",
            (order_id, *orders.ARTICULATED_KINDS),
        ) if eligible(p)]
        if not linked:
            replaced = {p["supersedes"] for p in candidates}
            candidates = [p for p in candidates if p["id"] not in replaced]
    choices = [{"id": p["id"], "title": p["title"], "revision": p["revision"]}
               for p in sorted(candidates, key=lambda p: p["id"])]
    if model_piece is None and (step := order_figure_lineage.missing_model(self, reference)):
        return {"ok": True, "status": "needs_model", "next_step": step, "model_choices": choices, "undo": None}
    if not candidates:
        raise _err("Create or register an animated GLB for this order first. / "
                   "Crea o registra primero un GLB animado para esta orden.",
                   409, error_code="articulated_model_required")

    if len(choices) == 1:
        choice = choices[0]
        name = f"#{choice['id']} «{choice['title']}»"
        raise _err(f"Confirm {name} with model_piece. / Confirma {name} con model_piece.",
                   409, error_code="articulated_model_confirmation_required", model_choices=choices)
    raise _err("Choose model_piece for the matching take. / Indica el model_piece de la toma.",
               409, error_code="articulated_model_ambiguous", model_choices=choices)


def draft_figure_request(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Queue a print pose from a paused variant or a registered reference frame."""
    seconds = a.get("seconds")
    size_mm = a.get("size_mm", 216)
    from_frame = a.get("frame_piece") is not None
    if (isinstance(seconds, bool) or not isinstance(seconds, (float, int))
            or not math.isfinite(seconds) or not 0 <= seconds <= (100_000 / 24 if from_frame else 4)):
        raise _err("Choose a time within the take. / Elige un instante dentro de la toma.")
    if isinstance(size_mm, bool) or not isinstance(size_mm, (float, int)) or not 10 <= size_mm <= 2000:
        raise _err("Choose 10 to 2000 mm. / Elige un tamaño de 10 a 2000 mm.")
    frame = math.floor(seconds * 24 + 0.5)
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    with self.db.tx():
        if from_frame:
            if any(a.get(key) is not None for key in ("variant_id", "take_piece", "signature")):
                raise _err("Use frame_piece or a paused variant. / Usa frame_piece o una variante pausada.")
            order = orders._order_row(self, a.get("order_id"))
            reference_row = orders._piece_row(self, a["frame_piece"])
            reference = orders._piece_dict(self, reference_row)
            if (reference["order_id"] != order["id"] or reference["view"] != "image" or not reference["sha256"]
                    or not reference["exists"] or reference["status"] in ("cancelled", "stale")):
                raise _err("Choose a registered frame image from this order. / Elige un fotograma registrado de esta orden.")
            source = _figure_source(self, reference_row, a.get("model_piece"))
            if source.get("status") == "needs_model":
                return source
            variant_id, title = None, order["title"]
        else:
            row = _variant_row(self, a.get("variant_id"))
            draft = _pass_row(self, row["pass_id"])
            if draft["status"] != "shown" or row["status"] != "ready":
                raise _err("This variant is no longer waiting. / Esta variante ya no está pendiente.", 409)
            variant = _variant_dict(self, row)
            if variant["signature"] != a.get("signature"):
                raise _err("The variant has changed. / La variante ha cambiado.", 409, error_code="variant_changed")
            video_id = orders._int(a.get("take_piece"), "take_piece")
            reference = next((p for p in variant["pieces"] if p["id"] == video_id and p["anchor"] == "animatic"), None)
            if not reference or Path(reference.get("path", "")).suffix.lower() not in {".mp4", ".webm", ".mov", ".m4v"}:
                raise _err("Choose a take attached to this variant. / Elige una toma de esta variante.")
            blocking = [p for p in variant["pieces"] if p["anchor"] == "blocking" and
                        Path(p.get("path", "")).suffix.lower() == ".glb"]
            if len(blocking) != 1 or not blocking[0].get("sha256") or not reference.get("sha256"):
                raise _err("Create and register one animated blocking GLB for this variant. / "
                           "Crea y registra un GLB de bloqueo animado para esta variante.",
                           409, error_code="articulated_model_required")
            source = blocking[0]
            order = {"id": row["order_id"]}
            variant_id, title = row["id"], variant["title"]
        signature = hashlib.sha256(json.dumps([variant_id, reference["id"], reference["sha256"], source["id"],
                                               source["sha256"], frame, float(size_mm)]).encode()).hexdigest()
        branch = "figure-" + signature[:24]
        existing = self.db.one("SELECT task_id FROM agora_order_jobs WHERE order_id=? AND branch=? AND removed=0",
                               (order["id"], branch))
        derivation = [{"piece": source["id"], "role": "material"}, {"piece": reference["id"], "role": "reference"}]
        stored = source.get("stored")
        frozen = {"source": stored if stored and Path(stored).is_file() else source["path"],
                  "source_sha256": source["sha256"], "frame": frame, "fps": 24}
        if from_frame:
            frozen.update(print_size_mm=size_mm, sheet_wall_mm=2.0)
        test_pose = orders._is_blocking(self, source)
        result = {"ok": True, "frame": frame, "freeze_arguments": frozen, "derives_from": derivation,
                  "test_pose": test_pose}
        if existing:
            return {**result, "task_id": existing["task_id"], "existing": True, "undo": None}
        plan = orders.plan_set(self, {
            "order_id": order["id"], "agent": agent, "_person": bool(a.get("_person")),
            "jobs": [{"key": "figure", "title": f"Figura de {title} · f{frame} · {size_mm:g} mm",
                      "station": "phidias", "branch": branch, "skill": "studio-figure-print-and-slice",
                      "inputs": [source["id"], reference["id"]],
                      "produces": ["posed-figure-mesh", "closed-print-mesh", "coloured-3mf"],
                      "body": (
                          f"Referencia visual #{reference['id']} r{reference['revision']} "
                          f"({reference['sha256']}), {seconds:g} s. "
                          f"Evalúa el GLB articulado #{source['id']} r{source['revision']} con "
                          f"blocking_freeze (frame={frame}, fps=24, source_sha256={source['sha256']}). "
                          "Registra la pose estática con el GLB articulado como material y la imagen o toma como referencia. "
                          + ("Conserva el linaje de material de la pose de prueba hasta el 3MF. " if test_pose else "")
                          + f"Importa el piece_id y SHA-256 de la pose con print_import (size_mm={size_mm:g}). "
                          "Esculpe, cierra y mide la pared. Colorea en Nikias desde el GLB posado y coteja la paleta "
                          "con la referencia visual. Registra el 3MF y las vistas del laminado. "
                          "Si quedan cuerpos separados, mide los huecos antes de usar bridge_gap_mm."),
                      "check": ("Coteja los agarres frontal y a tres cuartos, la pared de al menos 0,8 mm, "
                                "los colores de la referencia y el laminado.")}],
        })
    return {**result, "task_id": plan["keys"]["figure"], "undo": plan["undo"]}


OPERATIONS = {"draft_pass_open": draft_pass_open, "draft_pass_note_set": draft_pass_note_set,
              "draft_variant_set": draft_variant_set,
              "draft_gate_open": draft_gate_open, "draft_decide": draft_decide,
              "draft_passes": draft_passes, "draft_anchors": draft_anchors, "draft_figure_request": draft_figure_request,
              "draft_model_request": order_figure_lineage.draft_model_request}
WRITE_OPS = ("draft_pass_open", "draft_pass_note_set", "draft_variant_set", "draft_gate_open", "draft_decide",
             "draft_figure_request", "draft_model_request")
READ_ROUTES = ("draft_passes", "draft_anchors")
#: The person's early decision has no tool: only their pages call it.
NO_TOOL = ("draft_decide",)


def tools(schema: Any, agent_prop: dict[str, Any]) -> list[dict[str, Any]]:
    order_id = {"type": "integer", "minimum": 1, "description": "The order's id (id in hub_agora_orders)."}
    undo = " Returns undo for hub_agora_order_undo."
    measures = {"type": "object", "additionalProperties": False, "properties": {
        "seconds": {"type": "number", "minimum": 0}, "gpu_seconds": {"type": "number", "minimum": 0},
        "gpu": {"type": "string", "maxLength": 300}, "engine": {"type": "string", "maxLength": 300},
        "steps": {"type": ["integer", "string"]}, "resolution": {"type": "string", "maxLength": 300},
        "device": {"type": "string", "maxLength": 300}, "note": {"type": "string", "maxLength": 300}}}
    variant = {"type": "object", "additionalProperties": False, "required": ["key", "title"], "properties": {
        "key": {"type": "string", "maxLength": 20, "description": "Short slug: a, b, c, desenrollar…"},
        "title": {"type": "string", "maxLength": 200}, "idea": {"type": "string", "maxLength": 4000},
        "jobs": {"type": "array", "maxItems": 30, "items": {"type": "object"},
                 "description": "Plan jobs of this variant, as in hub_agora_plan_set (key, title, station, produces, check, after…)."},
        "measures": measures}}
    return [
        {"name": "hub_agora_draft_pass_open",
         "description": "Open a draft pass (pasada 0) of an order: 1-4 structural variants of the whole chain made in "
                        "draft mode. Use template=visual for outline → blocking → guided keys → draft shot and figure. "
                        "Usa template=visual para esquema → bloqueo → claves guiadas → plano y figura. "
                        "Variants can supply their own jobs. Baseline notes the time and GPU of a full pass." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id, "title": {"type": "string", "maxLength": 200},
                                "note": {"type": "string", "maxLength": 20000}, "baseline": measures,
                                "template": {"type": "string", "enum": ["visual"]},
                                "variants": {"type": "array", "items": variant, "minItems": 1, "maxItems": MAX_VARIANTS}},
                               ["agent", "order_id", "variants"])},
        {"name": "hub_agora_draft_pass_note_set",
         "description": "Correct a draft pass note / Corrige la nota de una pasada.\n"
                        "Works while the pass is shown. Send an empty string to clear the note. "
                        "Funciona con la pasada mostrada. Envía texto vacío para borrar la nota." + undo,
         "inputSchema": schema({"agent": agent_prop, "pass_id": {"type": "integer", "minimum": 1},
                                "note": {"type": "string", "maxLength": orders.TEXT_MAX}}, ["agent", "pass_id", "note"])},
        {"name": "hub_agora_draft_variant_set",
         "description": "Attach pieces to a draft variant with their anchor (blocking, depth, normals, parts, "
                        "keyframe_start, keyframe_end, composition, outline…), note measures and mark it ready." + undo,
         "inputSchema": schema({"agent": agent_prop, "variant_id": {"type": "integer", "minimum": 1},
                                "pieces": {"type": "array", "maxItems": 60, "items": {"anyOf": [
                                    {"type": "integer", "minimum": 1},
                                    {"type": "object", "additionalProperties": False, "required": ["piece"], "properties": {
                                        "piece": {"type": "integer", "minimum": 1}, "anchor": {"type": "string", "maxLength": 40}}}]}},
                                "remove": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 60},
                                "measures": measures, "note": {"type": "string", "maxLength": 20000},
                                "status": {"type": "string", "enum": ["drafting", "ready"]}}, ["agent", "variant_id"])},
        {"name": "hub_agora_draft_gate_open",
         "description": "Show every ready variant of a draft pass to the person, end to end, for the early decision "
                        "made on their page. The order waits for it." + undo,
         "inputSchema": schema({"agent": agent_prop, "pass_id": {"type": "integer", "minimum": 1},
                                "note": {"type": "string", "maxLength": 20000}}, ["agent", "pass_id"])},
        {"name": "hub_agora_draft_figure_request",
         "description": "Queue a figure from a paused take. / Encarga una figura de una toma pausada.\n"
                        "For a registered frame use order_id and frame_piece. If it needs an articulated model, "
                        "use hub_agora_draft_model_request or specify a matching model_piece from model_choices." + undo,
         "inputSchema": schema({"agent": agent_prop, "variant_id": {"type": "integer", "minimum": 1},
                                "take_piece": {"type": "integer", "minimum": 1}, "signature": {"type": "string"},
                                "order_id": order_id,
                                "frame_piece": {"type": "integer", "minimum": 1},
                                "model_piece": {"type": "integer", "minimum": 1},
                                "seconds": {"type": "number", "minimum": 0, "maximum": 100_000 / 24},
                                "size_mm": {"type": "number", "minimum": 10, "maximum": 2000}},
                               ["agent", "seconds"])},
        {"name": "hub_agora_draft_model_request",
         "description": "Queue an articulated GLB for a take or frame without one. / "
                        "Encarga el GLB articulado que falta para una toma o un fotograma." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id,
                                "piece_id": {"type": "integer", "minimum": 1}}, ["agent", "order_id", "piece_id"])},
        {"name": "hub_agora_draft_passes", "read": True,
         "description": "Draft passes of an order with variants, pieces by anchor, measures against the baseline and "
                        "decisions; without order_id, those waiting for the person (all_spaces for every space).",
         "inputSchema": schema({"order_id": order_id, "status": {"type": "string", "enum": list(PASS_STATES)},
                                "all_spaces": {"type": "boolean"}}, [])},
        {"name": "hub_agora_draft_anchors", "read": True,
         "description": "What the expensive pass starts from: the kept variant's pieces by anchor, the guide for "
                        "image, video and split, and the derives_from to pass to hub_agora_piece_add.",
         "inputSchema": schema({"order_id": order_id, "task_id": {"type": "integer", "minimum": 1}}, [])},
    ]
