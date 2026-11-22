"""Limits and receipts for work on a production order."""
from __future__ import annotations

import json
import re
from pathlib import Path
from statistics import median

from ..atomic import read_json
from . import agora_ranks
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
    CREATE TABLE IF NOT EXISTS agora_order_attempt_closures (
        attempt_id INTEGER PRIMARY KEY REFERENCES agora_order_attempts(id),
        closed_by TEXT NOT NULL, close_reason TEXT NOT NULL
    );
"""


def setup(agora):
    agora.db.script(BUDGET_MIGRATION)


def _nonnegative(args, key):
    value = args.get(key)
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise orders._err(f"{key} must be a non-negative integer")
    return value


def _budget(agora, oid):
    row = agora.db.one("SELECT * FROM agora_order_budgets WHERE order_id=?", (oid,))
    return dict(row) if row else None


def hold_reason(agora, oid):
    # Agora can also be used without loading Hub facets (the CLI's local tests).
    if not agora.db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agora_order_budgets'"):
        return ""
    return (_budget(agora, oid) or {}).get("reason", "")


def configure(agora, args):
    agent = agora._agent(args.get("agent"))
    retries, gpu_seconds, returns = (_nonnegative(args, key) for key in ("retries", "gpu_seconds", "returns"))
    with agora.db.tx():
        row = orders._order_row(agora, args.get("order_id"))
        oid = row["id"]
        stopped = hold_reason(agora, oid)
        journal = orders._Journal(agora, agent, "order_budget")
        journal.before("agora_order_budgets", oid)
        agora.db.execute("INSERT INTO agora_order_budgets(order_id,retries,gpu_seconds,returns) VALUES(?,?,?,?) "
                         "ON CONFLICT(order_id) DO UPDATE SET retries=excluded.retries, gpu_seconds=excluded.gpu_seconds, "
                         "returns=excluded.returns, reason=''", (oid, retries, gpu_seconds, returns))
        check_returns(agora, row, journal, agent)
        if (stopped and not hold_reason(agora, oid)
                and not agora.db.scalar("SELECT 1 FROM agora_gates WHERE order_id=? AND status='open'", (oid,))):
            orders._set_status(agora, oid, "producing", journal, only_from=("waiting",))
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
    if not agora.db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agora_order_budgets'"):
        return
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


def _may_close_for_worker(agora, tid, agent, worker):
    task = agora.db.one("SELECT owner, reviewer FROM tasks WHERE id=? AND space_id=?", (tid, agora.space_id))
    if not task:
        return False
    owner = task["owner"]
    if agent == owner:
        return True
    if agent == task["reviewer"]:
        return not any(agora_ranks.outranks(agora, holder, agent) for holder in (owner, worker))
    return agora_ranks.outranks(agora, agent, worker) and (not owner or agora_ranks.outranks(agora, agent, owner))


def attempt(agora, args):
    agent = agora._agent(args.get("agent"))
    action = args.get("action")
    if action not in ("begin", "pass", "fail"):
        raise orders._err("action must be begin, pass or fail")
    gpu = _nonnegative(args, "gpu_seconds") if "gpu_seconds" in args else None
    close_reason = orders._txt(args.get("reason"), 2000, "reason")
    key = orders._txt(args.get("key"), 120, "key", required=True)
    tid = orders._int(args.get("task_id"), "task_id")
    with agora.db.tx():
        row = orders._order_row(agora, args.get("order_id"))
        oid = row["id"]
        job = agora.db.one("SELECT station FROM agora_order_jobs WHERE order_id=? AND task_id=? AND removed=0", (oid, tid))
        if job is None:
            raise orders._err("task_id is not a job of this order", 404)
        previous = agora.db.one(
            "SELECT a.*, c.closed_by, c.close_reason FROM agora_order_attempts a "
            "LEFT JOIN agora_order_attempt_closures c ON c.attempt_id=a.id "
            "WHERE a.order_id=? AND a.task_id=? AND a.key=?", (oid, tid, key))
        budget = _budget(agora, oid)
        reason = (budget or {}).get("reason", "")
        if action == "begin":
            if previous and previous["agent"] == agent:
                return {"ok": True, "attempt": dict(previous), "existing": True, "hold": reason}
            if previous and previous["result"] != "running":
                raise orders._err("Another agent holds this attempt", 409)
            if row["status"] in ("cancelled", "delivered"):
                raise orders._err("That order is closed", 409)
            active = agora.db.one("SELECT id, key, agent FROM agora_order_attempts WHERE task_id=? AND result='running'",
                                  (tid,))
            if active:
                raise orders._err("This station job already has an active attempt", 409, active_attempt=dict(active))
        else:
            if previous is None:
                raise orders._err("Begin the attempt before reporting it", 404)
            own_receipt = agent in (previous["agent"], previous["closed_by"])
            if own_receipt and previous["result"] == action:
                return {"ok": True, "attempt": dict(previous), "existing": True, "hold": reason}
            if agent != previous["agent"]:
                if previous["result"] != "running" or not _may_close_for_worker(agora, tid, agent, previous["agent"]):
                    raise orders._err("Another agent holds this attempt", 409)
                if not close_reason:
                    raise orders._err("Give the reason for closing another worker's attempt. / Indica el motivo del "
                                      "cierre del intento de otro trabajador.")
            if previous["result"] != "running":
                raise orders._err("This attempt already has an outcome", 409)

        journal = orders._Journal(agora, agent, "order_attempt")
        check_returns(agora, row, journal, agent)
        reason = (_budget(agora, oid) or {}).get("reason", "")
        usage = _usage(agora, oid, tid)
        failures = usage["failures"] or 0
        if action == "begin":
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
            entry = previous["id"]
            journal.before("agora_order_attempts", entry)
            actual = previous["gpu_seconds"] if gpu is None else gpu
            agora.db.execute("UPDATE agora_order_attempts SET result=?, closed=?, gpu_seconds=? WHERE id=?",
                             (action, agora.clock(), actual, entry))
            agora.db.insert("agora_order_attempt_closures", {
                "attempt_id": entry, "closed_by": agent, "close_reason": close_reason,
            })
            journal.created("agora_order_attempt_closures", entry)
            if budget and not reason:
                if action == "fail" and failures + 1 > budget["retries"]:
                    reason = "retries"
                elif actual and usage["gpu"] - previous["gpu_seconds"] + actual >= budget["gpu_seconds"]:
                    reason = "gpu"
                if reason:
                    _hold(agora, row, reason, journal, agent)
        token = journal.save()
        result = dict(agora.db.one("SELECT * FROM agora_order_attempts WHERE id=?", (entry,)))
        result.update(closed_by=agent if action != "begin" else None,
                      close_reason=close_reason if action != "begin" else None)
    return {"ok": True, "attempt": result, "hold": reason, "undo": token}


def _task_measures(tasks):
    closed = [row for row in tasks if row["status"] == "done"]
    first = [row for row in closed if row["submission_revision"] == 1]
    returned = [row for row in closed if row["submission_revision"] > 1]

    def elapsed(rows, start, end):
        seconds = [row[end] - row[start] for row in rows
                   if row[start] is not None and row[end] is not None and row[end] >= row[start]]
        return median(seconds) if seconds else None

    return {
        "tasks": len(tasks),
        "closed_tasks": len(closed),
        "first_submission_tasks": len(first),
        "returned_tasks": len(returned),
        "resubmissions": sum(row["submission_revision"] - 1 for row in returned),
        "median_close_first_seconds": elapsed(first, "created", "closed_at"),
        "median_close_returned_seconds": elapsed(returned, "created", "closed_at"),
        "median_submit_first_seconds": elapsed(first, "claimed_at", "submitted_at"),
        "median_submit_returned_seconds": elapsed(returned, "claimed_at", "submitted_at"),
    }


def _task_metrics(agora, oid, space):
    sql = (
        "SELECT t.repo,t.context,t.commits,t.reviewed_commits,t.thread_id,t.status,t.submission_revision,"
        "t.created,t.claimed_at,t.submitted_at,t.closed_at,"
        "coalesce(j.station,'') AS station FROM tasks t LEFT JOIN agora_order_jobs j ON j.task_id=t.id "
        "WHERE t.status<>'dropped' AND (j.task_id IS NULL OR j.removed=0)"
    )
    if oid is not None:
        sql, params = sql + " AND j.order_id=?", (oid,)
    elif space:
        sql, params = sql + " AND t.space_id=?", (space,)
    else:
        params = ()
    rows = agora.db.query(sql, params)
    repositories = getattr(agora, "registered_repositories", lambda: [])()
    refs = {ref for row in rows if row["submission_revision"] and not row["repo"]
            for refs in _delivery_refs(row) for ref in refs}
    from .integration import submission_summary
    from .integration_git import declared_repository, repository_commit_index

    service_repo = getattr(agora, "order_service_repository", lambda: None)() if refs else None
    if service_repo and not any(Path(row["path"]).resolve() == Path(service_repo["path"]).resolve()
                                for row in repositories):
        repositories = [*repositories, service_repo]
    commit_index = repository_commit_index(repositories, sorted(refs))
    stations = {repo["name"].casefold(): _repository_station(repo) for repo in repositories}
    stations["hoard hub"] = "hoardhub"
    groups = {}
    station_repos = {}
    for row in rows:
        declared = json.loads(row["context"] or "{}").get("station")
        station = row["station"] or (declared.strip() if isinstance(declared, str) else "")
        if station not in station_repos:
            station_repos[station] = orders._station_repo(agora, station)
        repo = row["repo"] or station_repos[station]
        if not repo and row["submission_revision"]:
            for refs in _delivery_refs(row):
                names = _commit_repositories(commit_index, refs)
                if names:
                    repo = ", ".join(names)
                    station = station or ", ".join(dict.fromkeys(
                        stations[name.casefold()] for name in names if stations.get(name.casefold())))
                    break
            if not repo and row["submitted_at"] is not None:
                task = dict(row)
                for field in ("commits", "reviewed_commits"):
                    task[field] = json.loads(task[field] or "[]")
                repo = declared_repository(agora, submission_summary(agora, task))
                repo = next((entry["name"] for entry in repositories if entry["name"].casefold() == repo.casefold()),
                            repo)
        key = (station or stations.get(repo.casefold(), ""), repo)
        groups.setdefault(key, []).append(row)
    return [{"station": station, "repo": repo, **_task_measures(rows)}
            for (station, repo), rows in sorted(groups.items(), key=lambda item: (not any(item[0]), item[0]))]


def _delivery_refs(row):
    for field in ("commits", "reviewed_commits"):
        refs = json.loads(row[field] or "[]")
        if refs and all(isinstance(ref, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) for ref in refs):
            yield refs


def _commit_repositories(index, refs):
    candidates = [set(index[ref]) for ref in refs]
    common = set.intersection(*candidates)
    if len(common) == 1:
        return sorted(common)
    if all(len(group) == 1 for group in candidates):
        return sorted(set.union(*candidates))
    return []


def _repository_station(repo):
    if repo.get("app"):
        return repo["app"]
    manifest = read_json(Path(repo["path"]) / "faustus-plugin.json", {})
    return manifest.get("id", "") if isinstance(manifest, dict) and isinstance(manifest.get("id"), str) else ""


def metrics(agora, args):
    all_spaces = args.get("all_spaces") in (True, "1")
    space = None if all_spaces else agora.space_id
    oid = orders._int(args.get("order_id"), "order_id", required=False)
    if oid is not None:
        orders._order_row(agora, oid, any_space=all_spaces)
        where, params = "WHERE o.id=?", (oid,)
    else:
        where, params = ("WHERE o.space_id=?", (space,)) if space else ("", ())
    chains = agora.db.query(
        "WITH RECURSIVE lineage(id,root) AS (SELECT id,id FROM agora_pieces WHERE supersedes IS NULL "
        "UNION ALL SELECT p.id,l.root FROM agora_pieces p JOIN lineage l ON p.supersedes=l.id), "
        "first_piece AS (SELECT min(d.id) AS decision_id FROM agora_order_decisions d "
        "JOIN lineage l ON l.id=d.piece_id GROUP BY l.root), "
        "first_gate AS (SELECT order_id,min(gate_id) AS gate_id FROM agora_order_decisions GROUP BY order_id), "
        "gate_counts AS (SELECT d.order_id, count(*) AS decided, sum(d.decision='keep') AS kept "
        "FROM agora_order_decisions d JOIN first_gate g ON d.gate_id=g.gate_id "
        "JOIN agora_gates gate ON gate.id=g.gate_id AND gate.status='decided' GROUP BY d.order_id), "
        "piece_counts AS (SELECT d.order_id,count(*) AS decided,sum(d.decision='keep' AND p.revision=1) AS kept "
        "FROM first_piece f JOIN agora_order_decisions d ON d.id=f.decision_id JOIN agora_pieces p ON p.id=d.piece_id "
        "GROUP BY d.order_id) "
        "SELECT o.template AS chain,count(*) AS orders,count(g.order_id) AS orders_decided, "
        "sum(CASE WHEN g.kept=g.decided THEN 1 ELSE 0 END) AS orders_kept_first, "
        "coalesce(sum(p.decided),0) AS pieces_decided,coalesce(sum(p.kept),0) AS pieces_kept_first "
        f"FROM agora_orders o LEFT JOIN gate_counts g ON g.order_id=o.id LEFT JOIN piece_counts p ON p.order_id=o.id {where} "
        "GROUP BY o.template ORDER BY o.template", params,
    )
    attempts = agora.db.query(
        "SELECT a.station,count(*) AS attempts,sum(a.result='pass') AS passed,sum(a.result='fail') AS failed, "
        "sum(CASE WHEN a.closed IS NOT NULL THEN a.gpu_seconds ELSE 0 END) AS gpu_seconds, "
        "sum(CASE WHEN a.result='fail' THEN a.gpu_seconds ELSE 0 END) AS failed_gpu_seconds, "
        "sum(CASE WHEN a.result='pass' THEN a.closed-a.created ELSE 0 END) AS advance_seconds, "
        "sum(CASE WHEN a.result='fail' THEN a.closed-a.created ELSE 0 END) AS failed_seconds "
        f"FROM agora_order_attempts a JOIN agora_orders o ON o.id=a.order_id {where} GROUP BY a.station ORDER BY a.station", params,
    )
    defects = agora.db.query(
        "SELECT d.station, count(*) AS returns FROM agora_piece_defects d JOIN agora_orders o ON o.id=d.order_id "
        f"{where} GROUP BY d.station ORDER BY d.station", params,
    )
    stations = {row["station"]: dict(row) for row in attempts}
    for row in defects:
        stations.setdefault(row["station"], {"station": row["station"], "attempts": 0})["returns"] = row["returns"]
    holds = agora.db.query(
        "SELECT o.id,o.title,o.space_id,b.reason, "
        "(SELECT count(*) FROM agora_gates g WHERE g.order_id=o.id AND g.status='open') AS gates "
        f"FROM agora_orders o JOIN agora_order_budgets b ON b.order_id=o.id {where} "
        f"{'AND' if where else 'WHERE'} b.reason<>'' AND o.status NOT IN ('delivered','cancelled') ORDER BY o.id", params,
    )
    return {"ok": True, "chains": [dict(row) for row in chains], "stations": [stations[key] for key in sorted(stations)],
            "holds": [dict(row) for row in holds], "task_stations": _task_metrics(agora, oid, space)}
