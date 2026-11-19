"""Limits and receipts for work on a production order."""
from __future__ import annotations

from . import agora_orders as orders

BUDGET_MIGRATION = """
    CREATE TABLE IF NOT EXISTS agora_order_budgets (
        order_id INTEGER PRIMARY KEY, retries INTEGER NOT NULL, gpu_seconds INTEGER NOT NULL, returns INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS agora_order_attempts (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, task_id INTEGER NOT NULL, key TEXT NOT NULL,
        station TEXT NOT NULL, agent TEXT NOT NULL, created REAL NOT NULL, closed REAL,
        result TEXT NOT NULL DEFAULT 'running', gpu_seconds INTEGER NOT NULL,
        UNIQUE(order_id, task_id, key)
    );
"""


def _nonnegative(args, key):
    value = args.get(key)
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise orders._err(f"{key} must be a non-negative integer")
    return value


def _budget(agora, oid):
    row = agora.db.one("SELECT * FROM agora_order_budgets WHERE order_id=?", (oid,))
    return dict(row) if row else None


def configure(agora, args):
    agent = agora._agent(args.get("agent"))
    retries, gpu_seconds, returns = (_nonnegative(args, key) for key in ("retries", "gpu_seconds", "returns"))
    with agora.db.tx():
        row = orders._order_row(agora, args.get("order_id"))
        oid = row["id"]
        journal = orders._Journal(agora, agent, "order_budget")
        journal.before("agora_order_budgets", oid)
        agora.db.execute("INSERT INTO agora_order_budgets(order_id,retries,gpu_seconds,returns) VALUES(?,?,?,?) "
                         "ON CONFLICT(order_id) DO UPDATE SET retries=excluded.retries, gpu_seconds=excluded.gpu_seconds, "
                         "returns=excluded.returns, reason=''", (oid, retries, gpu_seconds, returns))
        check_returns(agora, row, journal, agent)
        token = journal.save()
    return {"ok": True, "budget": _budget(agora, oid), "undo": token}


def _hold(agora, row, reason, journal, agent):
    oid = row["id"]
    journal.before("agora_order_budgets", oid)
    agora.db.execute("UPDATE agora_order_budgets SET reason=? WHERE order_id=?", (reason, oid))
    if row["status"] in ("delivered", "cancelled"):
        return
    pending = agora.db.scalar("SELECT COUNT(*) FROM agora_gates WHERE order_id=? AND status='open'", (oid,), 0)
    if not pending:
        rows = agora.db.query(
            "SELECT p.id FROM agora_pieces p WHERE p.order_id=? AND p.status IN ('checked','provisional') "
            "AND NOT EXISTS(SELECT 1 FROM agora_pieces n WHERE n.supersedes=p.id) ORDER BY p.id DESC LIMIT ?",
            (oid, orders.MAX_ITEMS),
        )
        if rows:
            gid = agora.db.insert("agora_gates", {
                "order_id": oid, "space_id": agora.space_id, "title": row["title"],
                "note": "", "status": "open", "opened_by": agent, "created": agora.clock(),
            })
            journal.created("agora_gates", gid)
            for pos, piece in enumerate(rows):
                agora.db.insert("agora_gate_pieces", {"gate_id": gid, "piece_id": piece["id"], "position": pos})
                journal.created("agora_gate_pieces", None, where={"gate_id": gid, "piece_id": piece["id"]})
    orders._set_status(agora, oid, "waiting", journal, only_from=("open", "producing"))


def check_returns(agora, row, journal, agent):
    budget = _budget(agora, row["id"])
    if not budget or budget["reason"]:
        return
    count = agora.db.scalar("SELECT count(*) FROM agora_piece_defects WHERE order_id=?", (row["id"],), 0)
    if count and count >= budget["returns"]:
        _hold(agora, row, "returns", journal, agent)


def _usage(agora, oid, tid):
    return agora.db.one(
        "SELECT coalesce(sum(gpu_seconds),0) AS gpu, "
        "sum(CASE WHEN task_id=? AND result='fail' THEN 1 ELSE 0 END) AS failures "
        "FROM agora_order_attempts WHERE order_id=?", (tid, oid),
    )


def attempt(agora, args):
    agent = agora._agent(args.get("agent"))
    action = args.get("action")
    if action not in ("begin", "pass", "fail"):
        raise orders._err("action must be begin, pass or fail")
    gpu = _nonnegative(args, "gpu_seconds") if "gpu_seconds" in args else None
    key = orders._txt(args.get("key"), 120, "key", required=True)
    tid = orders._int(args.get("task_id"), "task_id")
    with agora.db.tx():
        row = orders._order_row(agora, args.get("order_id"))
        oid = row["id"]
        job = agora.db.one("SELECT station FROM agora_order_jobs WHERE order_id=? AND task_id=? AND removed=0", (oid, tid))
        if job is None:
            raise orders._err("task_id is not a job of this order", 404)
        previous = agora.db.one(
            "SELECT * FROM agora_order_attempts WHERE order_id=? AND task_id=? AND key=?", (oid, tid, key))
        if previous and previous["agent"] != agent:
            raise orders._err("Another agent holds this attempt", 409)
        budget = _budget(agora, oid)
        journal = orders._Journal(agora, agent, "order_attempt")
        check_returns(agora, row, journal, agent)
        reason = (_budget(agora, oid) or {}).get("reason", "")
        usage = _usage(agora, oid, tid)
        failures = usage["failures"] or 0
        if action == "begin":
            if previous:
                return {"ok": True, "attempt": dict(previous), "existing": True, "hold": reason}
            if row["status"] in ("cancelled", "delivered"):
                raise orders._err("That order is closed", 409)
            if agora.db.one("SELECT id FROM agora_order_attempts WHERE task_id=? AND result='running'", (tid,)):
                raise orders._err("This station job already has an active attempt", 409)
            if budget and not reason:
                if failures > budget["retries"]:
                    reason = "retries"
                elif usage["gpu"] + (gpu or 0) > budget["gpu_seconds"]:
                    reason = "gpu"
                if reason:
                    _hold(agora, row, reason, journal, agent)
            if reason:
                return {"ok": True, "state": "waiting", "hold": reason, "undo": journal.save()}
            entry = agora.db.insert("agora_order_attempts", {
                "order_id": oid, "task_id": tid, "key": key, "station": job["station"], "agent": agent,
                "created": agora.clock(), "gpu_seconds": gpu or 0,
            })
            journal.created("agora_order_attempts", entry)
        else:
            if previous is None:
                raise orders._err("Begin the attempt before reporting it", 404)
            if previous["result"] == action:
                return {"ok": True, "attempt": dict(previous), "existing": True, "hold": reason}
            if previous["result"] != "running":
                raise orders._err("This attempt already has an outcome", 409)
            entry = previous["id"]
            journal.before("agora_order_attempts", entry)
            actual = previous["gpu_seconds"] if gpu is None else gpu
            agora.db.execute("UPDATE agora_order_attempts SET result=?, closed=?, gpu_seconds=? WHERE id=?",
                             (action, agora.clock(), actual, entry))
            if budget and not reason:
                if action == "fail" and failures + 1 > budget["retries"]:
                    reason = "retries"
                elif actual and usage["gpu"] - previous["gpu_seconds"] + actual >= budget["gpu_seconds"]:
                    reason = "gpu"
                if reason:
                    _hold(agora, row, reason, journal, agent)
        token = journal.save()
        result = dict(agora.db.one("SELECT * FROM agora_order_attempts WHERE id=?", (entry,)))
    return {"ok": True, "attempt": result, "hold": reason, "undo": token}


def metrics(agora, args):
    where, params = ("", ()) if args.get("all_spaces") in (True, "1") else ("WHERE o.space_id=?", (agora.space_id,))
    chains = agora.db.query(
        "WITH first_piece AS (SELECT piece_id,min(id) AS decision_id FROM agora_order_decisions GROUP BY piece_id), "
        "first_gate AS (SELECT order_id,min(gate_id) AS gate_id FROM agora_order_decisions GROUP BY order_id), "
        "gate_counts AS (SELECT d.order_id, count(*) AS decided, sum(d.decision='keep') AS kept "
        "FROM agora_order_decisions d JOIN first_gate g ON d.gate_id=g.gate_id GROUP BY d.order_id), "
        "piece_counts AS (SELECT d.order_id,count(*) AS decided,sum(d.decision='keep' AND p.revision=1) AS kept "
        "FROM first_piece f JOIN agora_order_decisions d ON d.id=f.decision_id JOIN agora_pieces p ON p.id=f.piece_id "
        "GROUP BY d.order_id) "
        "SELECT o.template AS chain,count(*) AS orders,count(g.order_id) AS orders_decided, "
        "sum(CASE WHEN g.kept=g.decided AND NOT EXISTS(SELECT 1 FROM agora_gates WHERE id=(SELECT gate_id FROM first_gate "
        "WHERE order_id=o.id) AND status='open') THEN 1 ELSE 0 END) AS orders_kept_first, "
        "coalesce(sum(p.decided),0) AS pieces_decided,coalesce(sum(p.kept),0) AS pieces_kept_first "
        f"FROM agora_orders o LEFT JOIN gate_counts g ON g.order_id=o.id LEFT JOIN piece_counts p ON p.order_id=o.id {where} "
        "GROUP BY o.template ORDER BY o.template", params,
    )
    stations = agora.db.query(
        "SELECT a.station,count(*) AS attempts,sum(a.result='pass') AS passed,sum(a.result='fail') AS failed, "
        "sum(a.gpu_seconds) AS gpu_seconds,sum(CASE WHEN a.result='fail' THEN a.gpu_seconds ELSE 0 END) AS failed_gpu_seconds, "
        "sum(CASE WHEN a.result='pass' THEN a.closed-a.created ELSE 0 END) AS advance_seconds, "
        "sum(CASE WHEN a.result='fail' THEN a.closed-a.created ELSE 0 END) AS failed_seconds "
        f"FROM agora_order_attempts a JOIN agora_orders o ON o.id=a.order_id {where} GROUP BY a.station ORDER BY a.station", params,
    )
    holds = agora.db.query(
        "SELECT o.id,o.title,o.space_id,b.reason, "
        "(SELECT count(*) FROM agora_gates g WHERE g.order_id=o.id AND g.status='open') AS gates "
        f"FROM agora_orders o JOIN agora_order_budgets b ON b.order_id=o.id {where} "
        f"{'AND' if where else 'WHERE'} b.reason<>'' AND o.status NOT IN ('delivered','cancelled') ORDER BY o.id", params,
    )
    return {"ok": True, "chains": [dict(row) for row in chains], "stations": [dict(row) for row in stations],
            "holds": [dict(row) for row in holds]}
