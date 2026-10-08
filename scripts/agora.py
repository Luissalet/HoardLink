#!/usr/bin/env python3
"""Ágora from a shell: the coordination space of the coding agents and the person, served by Hoard Hub.

Standard library only, so any agent with a terminal can use it (no MCP needed)::

    python scripts/agora.py --as reviewer board
    python scripts/agora.py --as reviewer inbox --wait 60
    python scripts/agora.py --as builder hb "Lumiere: contact sheet" --state working
    python scripts/agora.py --as builder sync "Lumiere: contact sheet" --since 184 --thread 32
    python scripts/agora.py --as builder add "Fix stream cancel" --repo Faustus --paths src/agent_loop.py --claim
    python scripts/agora.py --as builder claim 12 --lock path:Faustus/src/ --lock model:principal
    python scripts/agora.py --as builder submit 12 --summary-file notes.md --commits abc123
    python scripts/agora.py --as reviewer review 12 approve --body "pytest 40/40, checked in the browser"
    python scripts/agora.py --as builder done 12 --result "merged into master" --commits abc123
    python scripts/agora.py --as reviewer open "q8 or q4 for live tests?" --kind debate --body-file prop.md --mention builder
    python scripts/agora.py --as builder post 7 --kind disagree --body-file answer.md
    python scripts/agora.py --as reviewer escalate 7 --question "q8 at 500k (slow, faithful) or q4 (fast)?"

Every long text accepts ``--<name>-file PATH`` (``-`` reads stdin): PowerShell eats ``$`` and nested quotes.
Agent id: ``--as`` or ``AGORA_AGENT``. Hub: ``HOARD_HUB_URL`` or ``data/url``; token ``HOARD_HUB_TOKEN_FILE`` or
``data/mcp-token`` next to this repository. ``--json`` prints the raw answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HOARD_HUB_DATA_DIR") or REPO / "data")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


def _url() -> str:
    env = os.environ.get("HOARD_HUB_URL")
    if env:
        return env.rstrip("/")
    try:
        return (DATA / "url").read_text(encoding="utf-8").strip().rstrip("/") or "http://127.0.0.1:8810"
    except OSError:
        return "http://127.0.0.1:8810"


def _token() -> str:
    path = os.environ.get("HOARD_HUB_TOKEN_FILE") or str(DATA / "mcp-token")
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def call(method: str, path: str, body: dict | None = None, timeout: float = 150.0) -> dict:
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(_url() + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + _token(), "User-Agent": "hoard-agora-cli"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"hub not reachable at {_url()}: {exc}"}


_MOJIBAKE = ("Ã", "Â", "â€", "├", "┬", "┤", "Ô")


def fix_text(value: str) -> str:
    """Undo the classic Windows double encoding of command-line text (UTF-8 bytes read as cp1252/cp850 by a
    BOM-less PowerShell 5.1 script or a batch file): «Â¿Ãgora» → «¿Ágora». Text that is already right is kept."""
    if not isinstance(value, str) or not any(m in value for m in _MOJIBAKE):
        return value
    for enc in ("cp1252", "cp850"):
        raw = bytearray()
        try:
            for ch in value:
                try:
                    raw += ch.encode(enc)
                except UnicodeEncodeError:
                    if ord(ch) < 256:
                        raw.append(ord(ch))
                    else:
                        raise
            fixed = bytes(raw).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != value:
            return fixed
    return value


def text_arg(args: argparse.Namespace, name: str) -> str | None:
    path = getattr(args, f"{name}_file", None)
    if path:
        return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return getattr(args, name, None)


def csv(value: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in value or []:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d-%m %H:%M")
    except (TypeError, ValueError):
        return "-"


def short(s, n=160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- printers ---------------------------------------------------------------------------------------------------

def print_board(b: dict, full: bool = False) -> None:
    print("AGENTES")
    for a in b.get("agents", []):
        flag = " (sin señales)" if a.get("stale") else ""
        print(f"  {a['id']:<14} {a.get('state') or '-':<8} {when(a.get('last_seen'))}{flag}  {short(a.get('doing'), 90)}")
        for g in a.get("lock_groups") or [{"label": lk} for lk in a.get("locks") or []]:
            print(f"      🔒 {g['label']}" + (f"  (tarea #{g['task_id']})" if g.get("task_id") else ""))
    print("\nTAREAS")
    for t in b.get("tasks", []):
        owner = f" @{t['owner']}" if t.get("owner") else ""
        print(f"  #{t['id']:<4} [{t['status']:<11}] p{t.get('priority')} {t.get('repo') or '':<14} {short(t['title'], 70)}{owner}")
    if b.get("locks") and (full or not b.get("lock_groups")):
        print("\nBLOQUEOS")
        for lk in b["locks"]:
            print(f"  {lk['resource']:<45} {lk['owner']:<12} tarea {lk.get('task_id') or '-':<5} hasta {when(lk['expires'])}")
    elif b.get("lock_groups"):
        print(f"\nBLOQUEOS ({len(b['locks'])}; --full para verlos uno a uno)")
        for g in b["lock_groups"]:
            print(f"  {short(g['label'], 70):<70} {g['owner']:<12} tarea {g.get('task_id') or '-':<5} hasta {when(g['expires'])}")
    print("\nHILOS")
    for th in b.get("threads", []):
        print(f"  {th['id']:<4} [{th['status']:<9}] {th['kind']:<9} {short(th['title'], 70)} ({th.get('messages')} msj, último {th.get('last_author')})")
    if b.get("decisions"):
        print("\nDECISIONES RECIENTES")
        for d in b["decisions"]:
            print(f"  {d['id']:<4} {short(d['title'], 50)} → {short(d['resolution'], 80)} ({d.get('resolved_by')})")


def print_inbox(r: dict) -> None:
    c = r.get("counts", {})
    print(f"Buzón de {r.get('agent')}: {c.get('messages', 0)} mensajes ({c.get('for_you', 0)} para ti), "
          f"{c.get('reviews', 0)} revisiones pendientes, {c.get('changes', 0)} cambios pedidos, {c.get('escalated', 0)} escalados")
    for t in r.get("reviews", []):
        print(f"  REVISAR  #{t['id']} {t['title']} (de {t.get('owner')}, rama {t.get('branch') or '-'}, commits {', '.join(t.get('commits') or []) or '-'})")
    for t in r.get("changes", []):
        print(f"  CAMBIOS  #{t['id']} {t['title']} (revisor {t.get('reviewer')})")
    if r.get("pending"):
        print(f"  MENCIONES SIN VER ({len(r['pending'])}; se quitan al abrir el hilo con thread, contestar o con ack):")
        seen = set()
        for m in r["pending"]:
            if m["thread_id"] in seen:
                continue
            seen.add(m["thread_id"])
            n = sum(1 for x in r["pending"] if x["thread_id"] == m["thread_id"])
            print(f"    hilo {m['thread_id']} · {short(m.get('thread_title'), 60)} · {n} de {m['author']}" + ("…" if n > 1 else ""))
    for th in r.get("escalated", []):
        print(f"  ESCALADO hilo {th['id']}: {th['title']}")
    for m in r.get("messages", []):
        mark = "→" if m.get("for_you") else " "
        where = f"tarea #{m['task_id']}" if m.get("thread_kind") == "task" else f"hilo {m['thread_id']}"
        print(f"{mark} [{where} · {m['thread_kind']}] {m['author']} ({m['kind']}, {when(m['created'])}) — {m.get('thread_title')}")
        for line in str(m.get("body") or "").splitlines() or [""]:
            print("      " + line)


def print_digest(r: dict) -> None:
    print(f"Últimas {r.get('hours'):g} h")
    for agent, c in sorted((r.get("agents") or {}).items()):
        print(f"  {agent:<14} abiertas {c['opened']} · reclamadas {c['claimed']} · a revisión {c['submitted']} · "
              f"hechas {c['done']} · revisiones dadas {c['reviews']} · mensajes {c['messages']}")
    s = r.get("done_by_review") or {}
    print(f"Hechas: {len(r.get('done') or [])} (revisadas {s.get('approved', 0)}, equivalentes {s.get('equivalent', 0)}, "
          f"exentas {s.get('exempt', 0)}, "
          f"sin revisión {s.get('unreviewed', 0)})")
    for t in r.get("done") or []:
        state = {"approved": "revisada", "equivalent": "equivalente declarada", "exempt": "exenta",
                 "unreviewed": "SIN REVISIÓN"}.get(t.get("review_state") or "", "-")
        print(f"  #{t['id']:<4} {short(t['title'], 70)} @{t.get('owner')} · {state} · {', '.join(t.get('commits') or []) or '-'}")
    for d in r.get("decisions") or []:
        print(f"  decisión {d['id']}: {short(d['title'], 50)} → {short(d['resolution'], 70)}")
    w = r.get("waiting") or {}
    print(f"Esperando: {len(w.get('reviews') or [])} revisiones, {len(w.get('changes') or [])} con cambios pedidos, "
          f"{len(w.get('escalated') or [])} escaladas a Luis")


def print_conversation(r: dict) -> None:
    head = r.get("task") or {}
    th = r.get("thread") or {}
    if head:
        print(f"#{head['id']} [{head['status']}] {head['title']} — owner {head.get('owner') or '-'}, revisor "
              f"{head.get('reviewer') or '-'}, repo {head.get('repo') or '-'}, rutas {', '.join(head.get('paths') or []) or '-'}")
        if head.get("branch") or head.get("commits"):
            print(f"  rama {head.get('branch') or '-'} · commits {', '.join(head.get('commits') or []) or '-'}")
    if th:
        print(f"Hilo {th['id']} [{th['status']}] {th['kind']}: {th['title']}")
        if th.get("resolution"):
            print(f"  Resolución ({th.get('resolved_by')}): {th['resolution']}")
    for lk in r.get("locks") or []:
        print(f"  🔒 {lk['resource']} ({lk['owner']})")
    if r.get("stances"):
        print("  Posturas: " + ", ".join(f"{k}={v}" for k, v in r["stances"].items()))
    for m in r.get("messages", []):
        print(f"\n— {m['author']} · {m['kind']} · {when(m['created'])}")
        print(m.get("body") or "")


def print_sync(r: dict) -> None:
    state = r.get("agent_state") or {}
    cursor = r.get("cursor") or {}
    scope = cursor.get("thread_ids")
    scope_label = "hilo(s) " + ",".join(str(i) for i in scope) if scope else "todos los hilos"
    print(f"Ágora sincronizada: {state.get('id')} · {state.get('state')} · {short(state.get('doing'), 100)}")
    print(f"Cursor de publicaciones ({scope_label}): {cursor.get('next_since_id')} "
          f"(pasa --since {cursor.get('next_since_id')} con el mismo filtro; guarda cursores aparte si cambias de filtro)"
          + (" · quedan más publicaciones" if cursor.get("has_more") else ""))
    inbox = r.get("inbox") or {}
    counts = inbox.get("counts") or {}
    print(f"Buzón en vista previa: {counts.get('messages', 0)} mensajes, {counts.get('reviews', 0)} revisiones, "
          f"{counts.get('changes', 0)} cambios, {counts.get('pending', 0)} menciones sin ver")
    for task in r.get("leased_tasks") or []:
        print(f"  Tarea propia #{task['id']} [{task['status']}] {short(task['title'], 100)} · "
              f"{len(task.get('locks') or [])} bloqueos activos")
    for post in r.get("posts") or []:
        print(f"  hilo {post['thread_id']} · {post['author']} ({post['kind']}) · {post.get('thread_title')}: "
              f"{short(post.get('body'), 240)}")


# ---- commands ---------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agora", description="Ágora de Hoard Hub desde la terminal")
    p.add_argument("--as", dest="agent", default=os.environ.get("AGORA_AGENT"), help="tu id de agente (el mismo siempre)")
    p.add_argument("--json", action="store_true", help="imprime la respuesta tal cual")
    sub = p.add_subparsers(dest="cmd", required=True)

    def texts(sp, *names):
        for n in names:
            sp.add_argument(f"--{n}")
            sp.add_argument(f"--{n}-file", dest=f"{n}_file")

    s = sub.add_parser("board"); s.add_argument("--full", action="store_true", help="todos los bloqueos, uno a uno")
    s = sub.add_parser("inbox"); s.add_argument("--wait", type=float, default=0); s.add_argument("--peek", action="store_true"); s.add_argument("--mine", action="store_true")
    s = sub.add_parser("hb"); s.add_argument("doing", nargs="?", default=""); s.add_argument("--state", default="working"); s.add_argument("--name")
    s = sub.add_parser("sync", help="un latido y snapshot del Ágora; --since usa el id durable de publicación")
    s.add_argument("doing", nargs="?", default=""); s.add_argument("--state", default="working"); s.add_argument("--name")
    s.add_argument("--note", help="nota breve del agente que se guarda en su heartbeat")
    s.add_argument("--since", dest="since_id", type=int, default=0,
                   help="id de mensaje devuelto en la última consulta del mismo filtro (por defecto: 0)")
    s.add_argument("--thread", action="append", type=int,
                   help="limita publicaciones a un hilo; repítelo para varios y conserva un cursor por filtro")
    s.add_argument("--limit", type=int, default=100, help="máximo de publicaciones por llamada (1–500)")
    s = sub.add_parser("add"); s.add_argument("title"); texts(s, "body"); s.add_argument("--repo"); s.add_argument("--paths", action="append")
    s.add_argument("--kind", default="feature"); s.add_argument("--prio", type=int, default=2); s.add_argument("--claim", action="store_true")
    s.add_argument("--lock", action="append"); s.add_argument("--mention", action="append")
    s = sub.add_parser("claim"); s.add_argument("task", type=int); s.add_argument("--lock", action="append"); s.add_argument("--ttl", type=int); s.add_argument("--force", action="store_true")
    s = sub.add_parser("update"); s.add_argument("task", type=int); texts(s, "note"); s.add_argument("--status"); s.add_argument("--branch")
    s.add_argument("--paths", action="append"); s.add_argument("--lock", action="append")
    s = sub.add_parser("submit"); s.add_argument("task", type=int); texts(s, "summary"); s.add_argument("--commits", action="append"); s.add_argument("--branch"); s.add_argument("--reviewer")
    s = sub.add_parser("review"); s.add_argument("task", type=int); s.add_argument("verdict", choices=["approve", "changes"]); texts(s, "body")
    s = sub.add_parser("done"); s.add_argument("task", type=int); texts(s, "result"); s.add_argument("--commits", action="append"); s.add_argument("--force", action="store_true"); s.add_argument("--reason")
    s = sub.add_parser("release"); s.add_argument("task", type=int); s.add_argument("--reason"); s.add_argument("--drop", action="store_true")
    s = sub.add_parser("lock"); s.add_argument("resources", nargs="+"); s.add_argument("--ttl", type=int); s.add_argument("--note"); s.add_argument("--task", type=int)
    s = sub.add_parser("unlock"); s.add_argument("resources", nargs="+")
    s = sub.add_parser("locks"); s.add_argument("--check")
    s = sub.add_parser("open"); s.add_argument("title"); texts(s, "body"); s.add_argument("--kind", default="debate"); s.add_argument("--task", type=int)
    s.add_argument("--mention", action="append"); s.add_argument("--escalate", action="store_true")
    s = sub.add_parser("post"); s.add_argument("thread", type=int); texts(s, "body"); s.add_argument("--kind", default="comment"); s.add_argument("--mention", action="append")
    s = sub.add_parser("resolve"); s.add_argument("thread", type=int); texts(s, "text")
    s = sub.add_parser("escalate"); s.add_argument("thread", type=int); texts(s, "question")
    s = sub.add_parser("reopen"); s.add_argument("thread", type=int); s.add_argument("--reason", required=True)
    s = sub.add_parser("task"); s.add_argument("task", type=int)
    s = sub.add_parser("thread"); s.add_argument("thread", type=int)
    s = sub.add_parser("ack"); s.add_argument("thread", type=int, nargs="?"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("digest"); s.add_argument("--hours", type=float, default=12)
    s = sub.add_parser("tasks"); s.add_argument("--status"); s.add_argument("--owner"); s.add_argument("--repo")
    sub.add_parser("decisions")
    a = p.parse_args(argv)
    for key, value in list(vars(a).items()):
        if isinstance(value, str) and not key.endswith("_file"):
            setattr(a, key, fix_text(value))
        elif isinstance(value, list):
            setattr(a, key, [fix_text(v) if isinstance(v, str) else v for v in value])

    reads = {"board", "task", "thread", "tasks", "decisions", "locks", "digest"}
    if a.cmd not in reads and not a.agent:
        p.error("falta --as <agente> (o AGORA_AGENT)")
    ag = a.agent
    c = a.cmd
    if c == "board":
        r = call("GET", "/api/agora/board")
    elif c == "inbox":
        q = {"agent": ag, "wait_s": a.wait, "peek": "1" if a.peek else "", "mine_only": "1" if a.mine else ""}
        r = call("GET", "/api/agora/inbox?" + urllib.parse.urlencode(q), timeout=max(30.0, a.wait + 30))
    elif c == "sync":
        r = call("POST", "/api/agora/sync", {"agent": ag, "doing": a.doing, "state": a.state,
                                                   "name": a.name, "note": a.note, "since_id": a.since_id,
                                                   "thread_ids": a.thread or [], "limit": a.limit})
    elif c == "hb":
        r = call("POST", "/api/agora/heartbeat", {"agent": ag, "doing": a.doing, "state": a.state, **({"name": a.name} if a.name else {})})
    elif c == "add":
        r = call("POST", "/api/agora/task_add", {"agent": ag, "title": a.title, "body": text_arg(a, "body") or "", "repo": a.repo or "",
                                                 "paths": csv(a.paths), "kind": a.kind, "priority": a.prio, "claim": a.claim,
                                                 "locks": csv(a.lock), "mentions": csv(a.mention)})
    elif c == "claim":
        r = call("POST", "/api/agora/task_claim", {"agent": ag, "task_id": a.task, "locks": csv(a.lock), "ttl_s": a.ttl, "force": a.force})
    elif c == "update":
        body = {"agent": ag, "task_id": a.task, "note": text_arg(a, "note") or "", "locks": csv(a.lock)}
        if a.status:
            body["status"] = a.status
        if a.branch:
            body["branch"] = a.branch
        if a.paths:
            body["paths"] = csv(a.paths)
        r = call("POST", "/api/agora/task_update", body)
    elif c == "submit":
        r = call("POST", "/api/agora/task_submit", {"agent": ag, "task_id": a.task, "summary": text_arg(a, "summary") or "",
                                                    "commits": csv(a.commits), "branch": a.branch, "reviewer": a.reviewer})
    elif c == "review":
        r = call("POST", "/api/agora/task_review", {"agent": ag, "task_id": a.task, "verdict": a.verdict, "body": text_arg(a, "body") or ""})
    elif c == "done":
        r = call("POST", "/api/agora/task_done", {"agent": ag, "task_id": a.task, "result": text_arg(a, "result") or "",
                                                  "commits": csv(a.commits), "force": a.force, "reason": a.reason or ""})
    elif c == "release":
        r = call("POST", "/api/agora/task_release", {"agent": ag, "task_id": a.task, "reason": a.reason or "", "drop": a.drop})
    elif c == "lock":
        r = call("POST", "/api/agora/lock", {"agent": ag, "resources": a.resources, "ttl_s": a.ttl, "note": a.note or "", "task_id": a.task})
    elif c == "unlock":
        r = call("POST", "/api/agora/unlock", {"agent": ag, "resources": a.resources})
    elif c == "locks":
        r = call("GET", "/api/agora/locks" + ("?" + urllib.parse.urlencode({"check": a.check}) if a.check else ""))
    elif c == "open":
        r = call("POST", "/api/agora/thread_open", {"agent": ag, "title": a.title, "body": text_arg(a, "body") or "", "kind": a.kind,
                                                    "task_id": a.task, "mentions": csv(a.mention), "escalate": a.escalate})
    elif c == "post":
        r = call("POST", "/api/agora/post", {"agent": ag, "thread_id": a.thread, "body": text_arg(a, "body") or "", "kind": a.kind,
                                             "mentions": csv(a.mention)})
    elif c == "resolve":
        r = call("POST", "/api/agora/resolve", {"agent": ag, "thread_id": a.thread, "resolution": text_arg(a, "text") or ""})
    elif c == "escalate":
        r = call("POST", "/api/agora/escalate", {"agent": ag, "thread_id": a.thread, "question": text_arg(a, "question") or ""})
    elif c == "reopen":
        r = call("POST", "/api/agora/reopen", {"agent": ag, "thread_id": a.thread, "reason": a.reason})
    elif c == "task":
        r = call("GET", f"/api/agora/tasks/{a.task}")
    elif c == "thread":
        r = call("GET", f"/api/agora/threads/{a.thread}" + (f"?agent={urllib.parse.quote(ag)}" if ag else ""))
    elif c == "ack":
        if not a.all and not a.thread:
            p.error("ack necesita un hilo o --all")
        r = call("POST", "/api/agora/ack", {"agent": ag, "thread_id": a.thread, "all": a.all})
    elif c == "digest":
        r = call("GET", "/api/agora/digest?" + urllib.parse.urlencode({"hours": a.hours}))
    elif c == "tasks":
        q = {k: v for k, v in {"status": a.status, "owner": a.owner, "repo": a.repo}.items() if v}
        r = call("GET", "/api/agora/tasks" + ("?" + urllib.parse.urlencode(q) if q else ""))
    else:
        r = call("GET", "/api/agora/decisions")

    if a.json or not r.get("ok", True):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok", True) else 1
    if c == "board":
        print_board(r, full=a.full)
    elif c == "digest":
        print_digest(r)
    elif c == "inbox":
        print_inbox(r)
    elif c == "sync":
        print_sync(r)
    elif c in ("task", "thread"):
        print_conversation(r)
    elif c == "tasks":
        print_board({"tasks": r.get("tasks", [])})
    elif c == "decisions":
        for d in r.get("decisions", []):
            print(f"{d['id']:<4} {when(d['updated'])} {d['title']}\n     → {d['resolution']} ({d.get('resolved_by')})")
    elif c == "locks" and "free" in r:
        print("libre" if r["free"] else "ocupado por " + ", ".join(f"{x['owner']} ({x['resource']})" for x in r["held_by"]))
    elif c == "locks":
        print_board({"locks": r.get("locks", [])})
    elif c == "hb":
        i = r.get("inbox") or {}
        print(f"ok · latido de {r.get('agent')} · buzón: {i.get('messages', 0)} mensajes ({i.get('for_you', 0)} para ti), "
              f"{i.get('reviews', 0)} revisiones, {i.get('changes', 0)} cambios, {i.get('pending', 0)} menciones sin ver")
    else:
        brief = r.get("task") or r.get("thread") or {k: v for k, v in r.items() if k != "ok"}
        if isinstance(brief, dict) and "id" in brief:
            print(f"ok · {'tarea #' if 'priority' in brief else 'hilo '}{brief['id']} [{brief.get('status')}] {brief.get('title')}")
        else:
            print("ok · " + json.dumps(brief, ensure_ascii=False))
        if r.get("locks"):
            print("  bloqueos: " + ", ".join(r["locks"]))
        if isinstance(r.get("claim"), dict):
            cl = r["claim"]
            print("  reclamada" if cl.get("ok") else "  NO reclamada: " + json.dumps(cl.get("conflicts"), ensure_ascii=False))
        if isinstance(r.get("inbox"), dict):
            i = r["inbox"]
            print(f"  buzón: {i.get('messages', 0)} mensajes ({i.get('for_you', 0)} para ti), {i.get('reviews', 0)} revisiones, {i.get('changes', 0)} cambios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
