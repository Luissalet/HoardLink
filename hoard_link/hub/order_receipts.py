"""Tool calls, draft procedures and benchmark history scoped to production orders."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .facets import Facet, Request
from .production import _tool
from . import skill_bench


logger = logging.getLogger(__name__)
CONTEXT = "_order_receipt"
ENV_CONTEXT = "HOARD_ORDER_RECEIPT"
SECRET_FIELDS = {"authorization", "cookie", "password", "secret", "access_token", "refresh_token", "api_key",
                 "bearer_token", "token", "mcp_token", "client_secret"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, space_id TEXT NOT NULL, task_id INTEGER,
    station TEXT NOT NULL, run_id TEXT, app TEXT NOT NULL, tool TEXT NOT NULL, started REAL NOT NULL,
    duration_ms INTEGER, ok INTEGER, arguments TEXT NOT NULL, reply TEXT,
    undo TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS receipt_order ON receipts(order_id, id);
CREATE INDEX IF NOT EXISTS receipt_run ON receipts(run_id, id);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, order_id INTEGER NOT NULL, task_id INTEGER NOT NULL, slug TEXT NOT NULL,
    text TEXT NOT NULL, receipts TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY, order_id INTEGER NOT NULL, space_id TEXT NOT NULL, task_id INTEGER,
    slug TEXT NOT NULL, draft_id TEXT, skill_sha256 TEXT NOT NULL, baseline TEXT NOT NULL,
    fields TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS benchmark_skill ON benchmarks(slug, created);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if str(key).lower().replace("-", "_") in SECRET_FIELDS else _redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _receipt(row):
    return {**{k: row[k] for k in row.keys() if k not in ("arguments", "reply", "undo", "ok")},
            "ok": None if row["ok"] is None else bool(row["ok"]), "arguments": json.loads(row["arguments"]),
            "reply": json.loads(row["reply"]) if row["reply"] is not None else None}


def _positive(value, field):
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**63:
        raise ValueError(f"{field} must be a positive integer")
    return int(value)


def request_context():
    """Optional common context inherited by configured CLI or stdio MCP clients."""
    value = os.environ.get(ENV_CONTEXT)
    return json.loads(value) if value else None


class OrderReceiptsFacet(Facet):
    id = "order_receipts"
    ui_scripts = ("order_receipts.js",)

    def __init__(self, hub):
        super().__init__(hub)
        self.path = Path(hub.config.data_dir) / "order-receipts.sqlite"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def order(self, args):
        order_id = _positive(args.get("order_id"), "order_id")
        agora = self.hub.facet("agora").agora
        row = agora.db.one("SELECT id, space_id FROM agora_orders WHERE id=?", (order_id,))
        if row is None or (args.get("space_id") and args["space_id"] != row["space_id"]):
            raise ValueError("Unknown order in this space")
        return dict(row)

    def context(self, args, app):
        if not isinstance(args, dict):
            raise ValueError("Receipt context must be an object")
        run_id = args.get("run_id")
        if run_id:
            with self.db() as db:
                run = db.execute("SELECT * FROM benchmarks WHERE id=? AND state='running'", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Unknown or finished benchmark")
            baseline = json.loads(run["baseline"])
            return {key: baseline[key] for key in ("order_id", "space_id", "task_id", "station")} | {"run_id": run_id}
        order = self.order(args)
        task_id = args.get("task_id")
        station = str(args.get("station") or app or "hub")
        if task_id is not None:
            task_id = _positive(task_id, "task_id")
            agora = self.hub.facet("agora").agora
            job = agora.db.one("SELECT station FROM agora_order_jobs WHERE task_id=? AND order_id=? AND space_id=?",
                               (task_id, order["id"], order["space_id"]))
            if job is None:
                raise ValueError("Job does not belong to this order")
            station = job["station"] or station
        return {"order_id": order["id"], "space_id": order["space_id"], "task_id": task_id,
                "station": station, "run_id": None}

    def capture(self, context, app, tool, arguments, invoke):
        started = time.time()
        clock = time.monotonic()
        saved_args = _json(_redact(arguments))
        token = uuid4().hex
        receipt_id = None
        try:
            with self.db() as db:
                row = db.execute(
                    "INSERT INTO receipts(order_id,space_id,task_id,station,run_id,app,tool,started,arguments,undo) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (context["order_id"], context["space_id"], context["task_id"], context["station"],
                     context["run_id"], app, tool, started, saved_args, token),
                )
                receipt_id = row.lastrowid
        except sqlite3.Error:
            logger.exception("Could not start receipt for order %s and tool %s", context["order_id"], tool)
            receipt_id = None
        try:
            reply = invoke()
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if receipt_id is None:
            return reply, {"receipt_error": "Tool finished but its receipt could not be saved"}
        meta = {"receipt_id": receipt_id, "receipt_undo": {"tool": "hub_order_receipt_undo", "arguments": {"token": token}}}
        try:
            with self.db() as db:
                ok = not (isinstance(reply, dict) and reply.get("ok") is False)
                db.execute("UPDATE receipts SET duration_ms=?,ok=?,reply=? WHERE id=?",
                           (round((time.monotonic() - clock) * 1000), ok, _json(_redact(reply)), receipt_id))
        except sqlite3.Error:
            logger.exception("Could not finish receipt %s", receipt_id)
            meta["receipt_error"] = "Tool finished but the reply could not be saved"
        return reply, meta

    def call(self, args):
        from . import tools

        app = str(args.get("app") or "")
        tool = str(args.get("tool") or "")
        arguments = args.get("arguments", {})
        if not tool or tool in (CALL, "hub_skill_bench_start") or not isinstance(arguments, dict):
            raise ValueError("Supply a tool name and an arguments object")
        context = self.context(args, app)
        wire_tool = "hub_call_app" if app else tool
        wire_args = {"app": app, "tool": tool, "arguments": arguments} if app else arguments
        if app:
            wire_args.update({k: args[k] for k in ("timeout_s", "reason") if k in args})
        reply, meta = self.capture(context, app, tool, arguments, lambda: tools.call(self.hub, wire_tool, wire_args))
        return {"ok": not (isinstance(reply, dict) and reply.get("ok") is False), "reply": reply, **meta}

    def list(self, args):
        order = self.order(args)
        cursor = args.get("before") or 2**63 - 1
        limit = max(1, min(int(args.get("limit", 30)), 100))
        task = args.get("task_id")
        params = [order["id"], int(cursor)]
        where = "order_id=? AND id<?"
        if task is not None:
            where += " AND task_id=?"
            params.append(_positive(task, "task_id"))
        if not args.get("include_bench"):
            where += " AND run_id IS NULL"
        with self.db() as db:
            rows = db.execute(f"SELECT id,task_id,station,run_id,app,tool,started,duration_ms,ok "
                              f"FROM receipts WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            drafts = db.execute("SELECT id,task_id,slug,created FROM drafts WHERE order_id=? ORDER BY created DESC",
                                (order["id"],)).fetchall()
            runs = db.execute("SELECT * FROM benchmarks WHERE order_id=? ORDER BY created DESC", (order["id"],)).fetchall()
        return {"ok": True, "receipts": [{**dict(row), "ok": None if row["ok"] is None else bool(row["ok"])}
                                         for row in rows[:limit]],
                "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
                "drafts": [dict(row) for row in drafts], "benchmarks": [skill_bench.status(self, row) for row in runs]}

    def read(self, args):
        order = self.order(args)
        with self.db() as db:
            row = db.execute("SELECT * FROM receipts WHERE order_id=? AND id=?",
                             (order["id"], _positive(args.get("receipt_id"), "receipt_id"))).fetchone()
        if row is None:
            raise ValueError("Unknown receipt for this order")
        return {"ok": True, "receipt": _receipt(row)}

    def draft(self, args):
        order = self.order(args)
        context = self.context(args, "")
        task_id = _positive(context["task_id"], "task_id")
        job = self.hub.facet("agora").agora.for_space(order["space_id"]).job({"task_id": task_id})["job"]
        production = self.hub.facet("production")
        stations = production.catalog({"app": context["station"]})["stations"] if production else []
        handoffs = [edge for station in stations if station["app"] == context["station"] for edge in station["handoffs"]]
        candidates = [edge for edge in handoffs if set(edge["produces"]).intersection(job["produces"])]
        types_in = sorted({piece["kind"] for piece in job["input_pieces"]} or {edge["accepts"] for edge in candidates})
        types_out = job["produces"]
        slug = skill_bench.slug(args.get("slug") or f"order-{order['id']}-job-{task_id}")
        with self.db() as db:
            rows = db.execute("SELECT * FROM receipts WHERE order_id=? AND task_id=? AND run_id IS NULL "
                              "AND ok IS NOT NULL ORDER BY id",
                              (order["id"], task_id)).fetchall()
        receipts = [_receipt(row) for row in rows]
        if not any(row["ok"] for row in receipts):
            raise ValueError("This job needs at least one successful receipt to draft a skill")
        text = skill_bench.draft_text(slug, context["station"], types_in, types_out, job.get("check_how"), receipts)
        draft_id = uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?)",
                       (draft_id, order["id"], task_id, slug, text, _json([r["id"] for r in receipts]), time.time()))
        return {**self.read_draft({**args, "draft_id": draft_id}),
                "undo": {"tool": "hub_order_receipt_undo", "arguments": {"draft_id": draft_id}}}

    def read_draft(self, args):
        order = self.order(args)
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=? AND order_id=?",
                             (args.get("draft_id"), order["id"])).fetchone()
        if row is None:
            raise ValueError("Unknown draft for this order")
        return {"ok": True, "draft": {**dict(row), "receipts": json.loads(row["receipts"])}}

    def undo(self, args):
        token, draft_id = args.get("token"), args.get("draft_id")
        if bool(token) == bool(draft_id):
            raise ValueError("Supply one receipt undo token or one draft id")
        with self.db() as db:
            if token:
                changed = db.execute("DELETE FROM receipts WHERE undo=?", (token,)).rowcount
            else:
                changed = db.execute("DELETE FROM drafts WHERE id=?", (draft_id,)).rowcount
        return {"ok": bool(changed), **({"error": "Unknown or already undone reference"} if not changed else {})}

    def get(self, req: Request):
        readers = {"order_receipts": self.list, "order_receipt": self.read, "order_skill_draft": self.read_draft,
                   "skill_bench": lambda args: skill_bench.history(self, args)}
        prefix = "/api/production/"
        if not req.path.startswith(prefix) or (read := readers.get(req.path[len(prefix):])) is None:
            return None
        if req.caller() not in ("hub", "ui"):
            return {"ok": False, "status": 401, "error": "Use the Hub page or the Hub bearer token"}
        args = {key: values[0] for key, values in req.query.items()}
        try:
            return read(args)
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}

    def post(self, req: Request):
        writers = {"/api/production/order_skill_draft": self.draft,
                   "/api/production/order_receipt_undo": self.undo,
                   "/api/production/skill_bench": lambda args: skill_bench.start(self, args),
                   "/api/production/skill_bench_undo": lambda args: skill_bench.undo(self, args)}
        write = writers.get(req.path)
        if write is None:
            return None
        if req.caller() not in ("hub", "ui"):
            return {"ok": False, "status": 401, "error": "Use the Hub page or the Hub bearer token"}
        try:
            return write(req.body)
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}

    @classmethod
    def tools(cls):
        string = {"type": "string"}
        integer = {"type": "integer"}
        order = {"order_id": integer, "space_id": string}
        job = {**order, "task_id": integer}
        return [
            _tool(CALL, "Call a tool and save its order receipt. Ejecutar y guardar el recibo.",
                  {**job, "station": string, "app": string, "tool": string, "arguments": {"type": "object"},
                   "run_id": string, "reason": string, "timeout_s": {"type": "number"}}, ("tool",), read=False),
            _tool("hub_order_receipts", "List an order’s receipts, drafts and skill tests. Ver recibos, borradores y pruebas.",
                  {**job, "before": integer, "limit": integer, "include_bench": {"type": "boolean"}}, ("order_id",)),
            _tool("hub_order_receipt", "Read a tool receipt. Leer un recibo.", {**order, "receipt_id": integer},
                  ("order_id", "receipt_id")),
            _tool("hub_order_skill_draft", "Draft a skill from one job’s receipts. Borrador de skill a partir de recibos.",
                  {**job, "slug": string}, ("order_id", "task_id"), read=False),
            _tool("hub_order_skill_draft_read", "Read a draft skill. Leer un borrador.", {**order, "draft_id": string},
                  ("order_id", "draft_id")),
            _tool("hub_order_receipt_undo", "Remove a saved receipt or draft. Quitar un recibo o borrador.",
                  {"token": string, "draft_id": string}, read=False),
            _tool("hub_skill_bench_start", "Test a skill on a recorded step. Probar una skill con un paso registrado.",
                  {**order, "receipt_id": integer, "draft_id": string, "slug": string, "step": string,
                   "inputs": {"type": "object"}, "compare": {"type": "array", "items": string}},
                  ("order_id", "receipt_id", "step"), read=False),
            _tool("hub_skill_bench", "Read skill test results. Leer resultados de pruebas de skills.",
                  {"slug": string, "run_id": string}),
            _tool("hub_skill_bench_undo", "Stop or remove a skill test. Detener o quitar una prueba de skill.",
                  {"run_id": string}, ("run_id",), read=False),
        ]

    def handlers(self):
        return {"hub_order_receipts": self.list, "hub_order_receipt": self.read, CALL: self.call,
                "hub_order_skill_draft": self.draft, "hub_order_skill_draft_read": self.read_draft,
                "hub_order_receipt_undo": self.undo, "hub_skill_bench_start": lambda args: skill_bench.start(self, args),
                "hub_skill_bench": lambda args: skill_bench.history(self, args),
                "hub_skill_bench_undo": lambda args: skill_bench.undo(self, args)}


CALL = "hub_order_call"
