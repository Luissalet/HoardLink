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
import re
from typing import Any, Optional

from . import agora_orders as orders

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

def draft_pass_open(self: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Open a draft pass of an order with its variants (key, title, idea and, optionally, the jobs of each variant)."""
    agent = self._agent(a.get("agent"), allow_person=bool(a.get("_person")))
    variants = a.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise _err(f"variants: a list of 1 to {MAX_VARIANTS} variants {{key, title, idea, jobs}}")
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
            if v.get("jobs"):
                if not isinstance(v["jobs"], list):
                    raise _err(f"variant {key}: jobs is a list of plan jobs (as in plan_set)")
                plan_jobs = []
                for n, j in enumerate(v["jobs"]):
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
                "idea": orders._txt(v.get("idea"), 4000, "idea"), "jobs": orders._dump(jobs), "status": "drafting",
                "measures": orders._dump(_measures(v.get("measures"))), "position": i, "created": now, "updated": now})
            journal.created("agora_draft_variants", vid)
            made.append(vid)
        token = journal.save()
    self._bump("agora.order.draft", {"order_id": oid, "pass_id": pid, "variants": made, "by": agent})
    return {"ok": True, "draft_pass": _pass_dict(self, _pass_row(self, pid)), "undo": token}


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
        situation = {"order": {"id": order_row["id"], "title": order_row["title"], "status": order_row["status"]},
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
                "note": "La pasada 0 espera la decisión de Luis en la puerta temprana." if waiting else
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


OPERATIONS = {"draft_pass_open": draft_pass_open, "draft_variant_set": draft_variant_set,
              "draft_gate_open": draft_gate_open, "draft_decide": draft_decide,
              "draft_passes": draft_passes, "draft_anchors": draft_anchors}
WRITE_OPS = ("draft_pass_open", "draft_variant_set", "draft_gate_open", "draft_decide")
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
                        "draft mode, each with its jobs (optional); baseline notes the time and GPU of a full pass." + undo,
         "inputSchema": schema({"agent": agent_prop, "order_id": order_id, "title": {"type": "string", "maxLength": 200},
                                "note": {"type": "string", "maxLength": 20000}, "baseline": measures,
                                "variants": {"type": "array", "items": variant, "minItems": 1, "maxItems": MAX_VARIANTS}},
                               ["agent", "order_id", "variants"])},
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
