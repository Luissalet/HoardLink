"""Bounded, read-only list queries for agents with small context windows."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import agora_decision_history, agora_orders

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
# Keep the cursor inside the 24,000-character budget of the agent's Agora reader.
PAGE_BYTE_BUDGET = 24_000 - 512
TASK_FIELDS = ("id", "title", "status", "repo", "owner", "updated", "closed_at", "submission_revision")
TASK_DATES = ("updated", "created", "closed_at")
TASK_DECODED_FIELDS = frozenset((
    "paths", "commits", "reviewed", "reviewed_commits", "submission_blocks", "origin_snapshot",
    "labels", "context", "repositories", "commits_by_repo",
))
TASK_COMPUTED_FIELDS = frozenset((
    "etag", "checkpoint_summary", "blocked_by", "waiting_on", "dependents", "activity",
))
DECISION_DATES = ("updated", "created")
THREAD_FIELDS = ("id", "title", "kind", "status", "resolved_by", "updated", "created", "withdrawn", "space_id")
PRODUCTION_FIELDS = ("id", "order_id", "gate_id", "piece_id", "decision", "created", "piece_title", "piece_kind",
                     "space_id")
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TIMESTAMP = re.compile(_DATE + r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-5][0-9])")


def properties(dates: tuple[str, ...]) -> dict[str, Any]:
    return {
        "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                   "description": "Return only these fields."},
        "compact": {"type": "boolean", "description": "Return one row of metadata per item."},
        "since": {"type": "string", "description": "Inclusive ISO date (UTC) or timestamp with Z or a UTC offset."},
        "until": {"type": "string", "description": "Exclusive ISO date (UTC) or timestamp with Z or a UTC offset."},
        "date_field": {"type": "string", "enum": list(dates),
                       "description": f"Timestamp to filter. Defaults to {dates[0]}."},
        "count_only": {"type": "boolean", "description": "Return the total matching count without reading any page."},
    }


def enabled(args: dict[str, Any]) -> bool:
    return bool(args.get("compact") or args.get("count_only")
                or any(key in args for key in ("fields", "since", "until", "date_field")))


def http_args(args: dict[str, Any]) -> dict[str, Any]:
    if "fields" in args:
        args["fields"] = [name.strip() for name in args["fields"].split(",")]
    for key in ("compact", "count_only"):
        if key in args:
            args[key] = args[key].lower() in ("1", "true", "yes")
    return args


def _timestamp(value: Any, key: str) -> float:
    from .agora import AgoraError

    if isinstance(value, str):
        try:
            if re.fullmatch(_DATE, value):
                return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
            if _TIMESTAMP.fullmatch(value):
                return datetime.fromisoformat(value).timestamp()
        except (ValueError, OverflowError):
            pass
    raise AgoraError(f"{key} must be YYYY-MM-DD (UTC) or an ISO timestamp with Z or an explicit UTC offset")


def _date_filters(args, columns):
    from .agora import AgoraError

    field = args.get("date_field", next(iter(columns)))
    if not isinstance(field, str) or field not in columns:
        raise AgoraError(f"date_field must be one of {', '.join(columns)}")
    since = _timestamp(args["since"], "since") if "since" in args else None
    until = _timestamp(args["until"], "until") if "until" in args else None
    if since is not None and until is not None and since > until:
        raise AgoraError("since must be at or before until")
    where, params = [], []
    for bound, op in ((since, ">="), (until, "<")):
        if bound is not None:
            where.append(f"{columns[field]}{op}?")
            params.append(bound)
    return where, params


def _fields(args, defaults, allowed):
    from .agora import AgoraError

    fields = args.get("fields", list(defaults))
    if (not isinstance(fields, list) or not 1 <= len(fields) <= 50
            or any(not isinstance(field, str) for field in fields)):
        raise AgoraError("fields must be a nonempty list of at most 50 field names")
    for field in fields:
        if field not in allowed:
            raise AgoraError(f"Unknown list field: {field[:80]}")
    return tuple(dict.fromkeys(fields))


def _limit(value):
    from .agora import AgoraError

    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or value < 1:
        raise AgoraError("limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _cursor(value, scopes=("id",)):
    from .agora import AgoraError

    if value in (None, ""):
        return None
    if isinstance(value, str):
        scope, _, raw = value.partition(":")
        if scope in scopes and re.fullmatch(r"[1-9][0-9]{0,18}", raw):
            before = int(raw)
            if before <= 2**63 - 1:
                return scope, before
    raise AgoraError("cursor must be the next_cursor returned by this list query")


def _page(rows, limit, project, scope=lambda row: "id"):
    from .agora import AgoraError

    items, size = [], 0
    for row in rows[:limit]:
        item = project(row)
        row_size = len(json.dumps(item)) + 2
        if size + row_size > PAGE_BYTE_BUDGET:
            if not items:
                raise AgoraError(f"List item {row['id']} exceeds the size limit. Request fewer fields or open the item.",
                                 413, item_id=row["id"])
            break
        items.append(item)
        size += row_size
    last = rows[len(items) - 1] if 0 < len(items) < len(rows) else None
    return items, f"{scope(last)}:{last['id']}" if last is not None else None


def tasks(ag, args, where, params):
    date_where, date_params = _date_filters(args, {name: name for name in TASK_DATES})
    condition = " AND ".join([*where, *date_where])
    params = [*params, *date_params]
    if args.get("count_only"):
        count = ag.db.scalar(f"SELECT COUNT(*) FROM tasks WHERE {condition}", params)
        return {"ok": True, "count": count}

    columns = {row["name"] for row in ag.db.query("PRAGMA table_info(tasks)")}
    fields = _fields(args, TASK_FIELDS, columns | TASK_COMPUTED_FIELDS)
    needs_full_row = not set(fields).isdisjoint(TASK_COMPUTED_FIELDS | TASK_DECODED_FIELDS)
    select = "*" if needs_full_row else ", ".join(dict.fromkeys(("id", *fields)))
    limit = _limit(args.get("limit", DEFAULT_LIMIT))
    cursor = _cursor(args.get("cursor"))
    if cursor is not None:
        condition += " AND id<?"
        params.append(cursor[1])
    rows = ag.db.query(f"SELECT {select} FROM tasks WHERE {condition} ORDER BY id DESC LIMIT ?",
                       (*params, limit + 1))

    def project(row):
        task = ag._task_dict(row) if needs_full_row else row
        return {field: task[field] for field in fields}

    items, cursor = _page(rows, limit, project)
    return {"ok": True, "tasks": items, "next_cursor": cursor}


def decisions(ag, args):
    thread_dates = {name: name for name in DECISION_DATES}
    production_dates = dict.fromkeys(DECISION_DATES, "created")
    thread_where, thread_params = _date_filters(args, thread_dates)
    production_where, production_params = _date_filters(args, production_dates)
    thread_condition = " AND ".join(["space_id=?", "status='resolved'", "kind<>'task'", *thread_where])
    production_condition = " AND ".join(["space_id=?", *production_where])
    sources = (f"SELECT id, 0 AS stream FROM threads WHERE {thread_condition} UNION ALL "
               f"SELECT id, 1 AS stream FROM agora_order_decisions WHERE {production_condition}")
    params = [ag.space_id, *thread_params, ag.space_id, *production_params]
    if args.get("count_only"):
        return {"ok": True, "count": ag.db.scalar(f"SELECT COUNT(*) FROM ({sources})", params)}

    allowed = {*THREAD_FIELDS, *PRODUCTION_FIELDS, "resolution", "history", "note", "situation", "decided_by"}
    selected = _fields(args, (), allowed) if "fields" in args else None
    thread_fields = selected or THREAD_FIELDS
    production_fields = selected or PRODUCTION_FIELDS
    limit = _limit(args.get("limit", DEFAULT_LIMIT))
    cursor = _cursor(args.get("cursor"), ("thread", "production"))
    condition = ""
    if cursor is not None:
        stream = int(cursor[0] == "production")
        condition = " WHERE (stream, -id) > (?, ?)"
        params.extend((stream, -cursor[1]))

    with ag.db.tx(immediate=False):
        rows = ag.db.query(f"SELECT id, stream FROM ({sources}){condition} ORDER BY stream, id DESC LIMIT ?",
                           (*params, limit + 1))
        thread_ids = [row["id"] for row in rows[:limit] if row["stream"] == 0]
        history = (agora_decision_history.histories(ag, thread_ids, decisions_only=True)
                   if "history" in thread_fields else {})
        names: dict[str, str] = {}

        def project(row):
            if row["stream"] == 0:
                thread = dict(ag.db.one(
                    "SELECT id, title, kind, status, resolved_by, updated, created, space_id, resolution "
                    "FROM threads WHERE id=?", (row["id"],)))
                agora_decision_history.thread_state(thread)
                thread["history"] = history.get(row["id"], [])
                return {field: thread[field] for field in thread_fields if field in thread}
            production = dict(ag.db.one(
                "SELECT d.*, p.title AS piece_title, p.kind AS piece_kind FROM agora_order_decisions d "
                "LEFT JOIN agora_pieces p ON p.id=d.piece_id WHERE d.id=?", (row["id"],)))
            if {"situation", "decided_by"}.intersection(production_fields):
                production = agora_orders._decision_record(ag, production, names)
            return {field: production[field] for field in production_fields if field in production}

        items, next_cursor = _page(rows, limit, project,
                                   lambda row: "production" if row["stream"] else "thread")
    split = sum(row["stream"] == 0 for row in rows[:len(items)])
    return {"ok": True, "decisions": items[:split], "production_decisions": items[split:],
            "next_cursor": next_cursor}
