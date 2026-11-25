"""Per-space weekly quota floors over the Hub's shared account observations."""

from __future__ import annotations

import json
from typing import Any

from . import agora_channels, agora_ranks


PROVIDERS = {"claude": "Claude", "codex": "Codex"}
STUDIO_FLOORS = {"claude": 45, "codex": 55}
SET_TOOL = "hub_agora_quota_floors_set"
MIGRATION = f"""
CREATE TABLE agora_quota_floors (
    space_id TEXT PRIMARY KEY REFERENCES coordination_spaces(id),
    revision INTEGER NOT NULL, floors TEXT NOT NULL);
CREATE TABLE agora_quota_floor_alerts (
    space_id TEXT NOT NULL REFERENCES coordination_spaces(id),
    provider_id TEXT NOT NULL, PRIMARY KEY(space_id, provider_id));
INSERT INTO agora_quota_floors(space_id, revision, floors)
    SELECT id, 1, '{json.dumps(STUDIO_FLOORS)}' FROM coordination_spaces WHERE id='estudio';
"""


def seed(agora: Any, space_id: str) -> None:
    if space_id == "estudio":
        agora.db.insert("agora_quota_floors", {"space_id": space_id, "revision": 1,
                                               "floors": json.dumps(STUDIO_FLOORS)})


def _policy(agora: Any) -> tuple[int, dict[str, float]]:
    row = agora.db.one("SELECT revision, floors FROM agora_quota_floors WHERE space_id=?", (agora.space_id,))
    return (int(row["revision"]), json.loads(row["floors"])) if row else (0, {})


def _weekly(provider: str, pool: dict) -> dict[str, Any]:
    # Explicit duration keeps Codex's five-hour primary out of a weekly rule. Only the account-wide window applies.
    ids = {"claude.week"} if provider == "claude" else {"codex.primary", "codex.secondary"}
    window = next((w for w in pool.get("windows", []) if w["window_id"] in ids
                   and (provider == "claude" or w.get("window_duration_mins") == 7 * 24 * 60)), {})
    remaining = window.get("remaining_percent") if window.get("measurement") == "measured" and not window.get("stale") else None
    return {"remaining_percent": remaining, "reported_at": window.get("reported_at"),
            "window_id": window.get("window_id")}


def _latest(provider: str, pools: list[dict]) -> dict:
    local = [pool for pool in pools if f"{provider}-capacity" in pool.get("agent_ids", [])]
    return max(local or pools, key=lambda pool: (pool["reported_at"], pool["pool_id"]), default={})


def view(agora: Any, usage: dict | None = None) -> dict[str, Any]:
    revision, floors = _policy(agora)
    usage = agora.usage() if usage is None else usage
    providers = {}
    for report in usage.get("providers", []):
        provider = report["provider_id"]
        pool = _latest(provider, report["pools"])
        providers[provider] = {"windows": pool.get("windows", []), "reported_at": pool.get("reported_at")}
        if provider in PROVIDERS:
            providers[provider]["weekly"] = _weekly(provider, pool)
    return {"space_id": agora.space_id, "revision": revision, "floors": floors, "providers": providers}


def can_manage(agora: Any, actor: str) -> bool:
    return (actor == agora.quota_foreman(agora.space_id)
            or agora_ranks.rank_of(agora, actor) in ("person", "director", "lead"))


def save(agora: Any, args: dict, actor: str) -> dict[str, Any]:
    if not can_manage(agora, actor):
        return {"ok": False, "status": 403, "error": "Only a space lead can change quota floors. / Solo un responsable del espacio puede cambiar los suelos de cuota."}
    expected, values = args.get("expected_revision"), args.get("floors")
    if (type(expected) is not int or expected < 0 or not isinstance(values, dict) or values.keys() - PROVIDERS.keys()
            or any(type(value) not in (int, float) or not 0 <= value <= 100
                   for value in values.values())):
        return {"ok": False, "status": 400,
                "error": "Send a revision and weekly floors from 0 to 100. / Indica una revisión y suelos semanales entre 0 y 100."}
    with agora.db.tx():
        revision, previous = _policy(agora)
        if revision != expected:
            return {"ok": False, "status": 409, "code": "stale_quota_floors",
                    "error": "Quota floors have changed. Refresh and try again. / Los suelos han cambiado. Actualiza y vuelve a intentarlo.",
                    "revision": revision}
        if values == previous:
            return {"ok": True, "revision": revision, "floors": previous}
        revision += 1
        agora.db.execute(
            "INSERT INTO agora_quota_floors(space_id, revision, floors) VALUES(?,?,?) "
            "ON CONFLICT(space_id) DO UPDATE SET revision=excluded.revision, floors=excluded.floors",
            (agora.space_id, revision, json.dumps(values)))
        for provider in PROVIDERS:
            if previous.get(provider) != values.get(provider):
                agora.db.execute("DELETE FROM agora_quota_floor_alerts WHERE space_id=? AND provider_id=?",
                                 (agora.space_id, provider))
    agora._bump("agora.quota_floors.updated", {"revision": revision, "by": actor})
    return {"ok": True, "revision": revision, "floors": values,
            "undo": {"tool": SET_TOOL, "arguments": {"space_id": agora.space_id,
                                                    "expected_revision": revision, "floors": previous}}}


def _text(provider: str, remaining: float, floor: float) -> dict[str, str]:
    name = PROVIDERS[provider]
    es_remaining, es_floor = (f"{value:g}".replace(".", ",") for value in (remaining, floor))
    return {
        "es": f"{name} tiene {es_remaining} % de cuota semanal libre. Suelo del espacio: {es_floor} %.",
        "en": f"{name} has {remaining:g}% of weekly quota left. Space floor: {floor:g}%.",
    }


def record_crossings(agora: Any, provider: str, pools: list[dict]) -> list[dict]:
    """Save one notice in #general per measured excursion. Caller holds the report transaction."""
    if provider not in PROVIDERS:
        return []
    weekly = _weekly(provider, _latest(provider, pools))
    remaining = weekly["remaining_percent"]
    if remaining is None:
        return []
    notices = []
    for row in agora.db.query("SELECT space_id, floors FROM agora_quota_floors ORDER BY space_id"):
        floor = json.loads(row["floors"]).get(provider)
        if floor is None:
            continue
        sid = row["space_id"]
        if remaining >= floor:
            agora.db.execute("DELETE FROM agora_quota_floor_alerts WHERE space_id=? AND provider_id=?", (sid, provider))
            continue
        inserted = agora.db.execute(
            "INSERT OR IGNORE INTO agora_quota_floor_alerts(space_id, provider_id) VALUES(?,?)", (sid, provider))
        if not inserted.rowcount:
            continue
        space = agora.for_space(sid)
        foreman = space.quota_foreman(sid)
        if foreman:
            recipients = [foreman]
        else:
            ranks = agora_ranks.ranks_in(space)
            recipients = sorted(name for name, rank in ranks.items() if rank == "director")
            recipients = recipients or sorted(name for name, rank in ranks.items() if rank == "lead") or [provider]
        thread = agora_channels._ensure_thread(space, agora_channels.GENERAL, "system")
        lang = "en" if str(space.config.language).startswith("en") else "es"
        message = space._msg(thread, "system", "comment", _text(provider, remaining, floor)[lang], recipients)
        notices.append({"space_id": sid, "thread_id": thread, "message_id": message, "author": "system",
                        "kind": "comment"})
    return notices


def below_floor(agora: Any, provider: str, pools: list[dict] | None = None) -> dict | None:
    if agora is None or provider not in PROVIDERS:
        return None
    _, floors = _policy(agora)
    floor = floors.get(provider)
    if floor is None:
        return None
    if pools is None:
        pools = agora._quota_pools().get(provider, [])
    reading = _weekly(provider, _latest(provider, pools))
    remaining = reading["remaining_percent"]
    if remaining is None or remaining >= floor:
        return None
    return {**reading, "provider_id": provider, "floor_percent": floor}


def worker_guard(agora: Any, provider: str, actor: str, args: dict, provider_leaders: set[str]) -> dict | None:
    reading = below_floor(agora, provider)
    if reading is None:
        return None
    confirmation = {key: reading[key] for key in ("provider_id", "floor_percent")}
    authorized = actor in provider_leaders or can_manage(agora, actor)
    supplied = args.get("confirm_quota_floor")
    if authorized and supplied == confirmation:
        return None
    refused = supplied is not None and not authorized
    text = _text(provider, reading["remaining_percent"], reading["floor_percent"])
    text["es"] += " El responsable debe confirmar expresamente la creación."
    text["en"] += " The directing lead must explicitly confirm the launch."
    lang = "en" if args.get("lang") == "en" else "es"
    return {"ok": False, "status": 403 if refused else 409, "code": "quota_below_floor",
            "error": text[lang], "error_i18n": text, "quota_floor": reading, "confirm_quota_floor": confirmation}
