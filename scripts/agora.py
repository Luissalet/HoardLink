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
    python scripts/agora.py --as builder servers          # every space as a server: what moves, who is around, what is new for you
    python scripts/agora.py --as builder --space faustus channels
    python scripts/agora.py --as builder --space faustus say general "@reviewer the build is green"   # @id mentions, @here everyone around
    python scripts/agora.py --as builder --space faustus read general --limit 30
    python scripts/agora.py --as builder --space faustus channel ideas --topic "what to build next" --category chat
    python scripts/agora.py --space faustus feed --kind deliveries   # what was delivered (reviews | activity)
    python scripts/agora.py watch                         # what each agent is really doing, from its own transcripts
    python scripts/agora.py leaders                       # state of each leader's CLI (working / waiting / idle / login expired / error)
    python scripts/agora.py watch-bind codex:<session id> codex-sparks   # assign a transcript session to an agent ("-" unbinds)
    python scripts/agora.py --as successor handover --from dead-agent --to successor --reason "context exhausted" [--tasks 1,2] [--no-reviews] [--force]

Every long text accepts ``--<name>-file PATH`` (``-`` reads stdin): PowerShell eats ``$`` and nested quotes.
Agent id: ``--as`` or ``AGORA_AGENT``. Hub: ``HOARD_HUB_URL`` or ``data/url``; token ``HOARD_HUB_TOKEN_FILE`` or
``data/mcp-token`` next to this repository. ``--json`` prints the raw answer.

Quota snapshots are read with ``usage`` and reported with ``usage-report --data-file snapshot.json``. The JSON file
contains ``provider_id``, opaque ``pool_id``/``pool_label``, ``source`` and a ``windows`` array; this command adds
the current ``--as`` identity and the Hub assigns ``reported_at``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
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


_SELECTED_SPACE = ""


def call(method: str, path: str, body: dict | None = None, timeout: float = 150.0) -> dict:
    if _SELECTED_SPACE:
        if method == "POST":
            body = {"space_id": _SELECTED_SPACE, **(body or {})}
        else:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"space_id": _SELECTED_SPACE})
    data = json.dumps(body or {}, allow_nan=False).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(_url() + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + _token(), "User-Agent": "hoard-agora-cli"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read() or b"{}")
            if isinstance(result, dict):
                result.setdefault("status", exc.code)
            return result
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"hub not reachable at {_url()}: {exc}"}


def fetch_file(file_id: str, out: str | None) -> dict:
    """Save a file a message carries: to ``out`` (a file, or a folder) or to this folder under its own name."""
    req = urllib.request.Request(_url() + "/api/agora/file/" + urllib.parse.quote(file_id.strip().lower(), safe=""),
                                 headers={"Authorization": "Bearer " + _token(), "User-Agent": "hoard-agora-cli"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            blob = resp.read()
            shown = resp.headers.get("Content-Disposition") or ""
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read() or b"{}")
        except ValueError:
            result = {}
        return {"ok": False, "status": exc.code, "error": (result.get("error") if isinstance(result, dict) else None) or f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"hub not reachable at {_url()}: {exc}"}
    name = shown.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in shown else file_id
    target = os.path.join(out, name) if out and os.path.isdir(out) else (out or name)
    with open(target, "wb") as fh:
        fh.write(blob)
    return {"ok": True, "name": name, "path": os.path.abspath(target), "size": len(blob)}


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

def quota_badge(q: dict | None) -> str:
    """Out of quota or nearly so, next to the agent (the hub warns, nothing is blocked)."""
    if not isinstance(q, dict):
        return ""
    if q.get("state") == "exhausted":
        return " ⛔ " + (f"sin cuota hasta {when_iso(q.get('reset_at'))}" if q.get("reset_at") else "sin cuota")
    if q.get("state") == "low":
        return f" ⚠ cuota baja ({q.get('remaining_percent'):.0f} %)" if q.get("remaining_percent") is not None else " ⚠ cuota baja"
    return ""


def when_iso(value) -> str:
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d-%m %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def print_quota_warnings(r: dict) -> None:
    for note in r.get("quota_warnings") or []:
        print(f"  ⛔ AVISO DE CUOTA: {note}")


def deps_badge(t: dict) -> str:
    """' ⏳ espera #12, #13' while blockers are pending; ' ✓ deps' once they are all through."""
    if t.get("status") in ("done", "dropped") or not t.get("blocked_by"):
        return ""
    waiting = t.get("waiting_on") or []
    return f" ⏳ espera {', '.join(f'#{i}' for i in waiting)}" if waiting else " ✓ deps"


def print_dep_notes(r: dict) -> None:
    for note in r.get("dep_warnings") or []:
        print(f"  ⏳ {note}")
    if r.get("unblocked"):
        print("  🔓 desbloqueadas: " + ", ".join(f"#{i}" for i in r["unblocked"]))


def print_orphans(found: list) -> None:
    if not found:
        return
    print(f"\nHUÉRFANAS ({len(found)}: nadie las mueve)")
    for f in found:
        why = ("sin revisor" if f.get("kind") == "review_unassigned"
               else f.get("why") if not f.get("who") else f"{f.get('who')} {f.get('why')}")
        tail = f" → {f['suggest']}: {f['command']}" if f.get("suggest") else " → nadie libre ahora"
        print(f"  #{f['task_id']:<4} [{f.get('status'):<11}] {short(f.get('title'), 50)} · {why}{tail}")


def print_board(b: dict, full: bool = False) -> None:
    print("AGENTES")
    for a in b.get("agents", []):
        flag = " (sin señales)" if a.get("stale") else ""
        print(f"  {a['id']:<14} {a.get('state') or '-':<8} {when(a.get('last_seen'))}{flag}{quota_badge(a.get('quota'))}"
              f"  {short(a.get('doing'), 90)}")
        for g in a.get("lock_groups") or [{"label": lk} for lk in a.get("locks") or []]:
            print(f"      🔒 {g['label']}" + (f"  (tarea #{g['task_id']})" if g.get("task_id") else ""))
    print_orphans(b.get("orphans") or [])
    print("\nTAREAS")
    for t in b.get("tasks", []):
        owner = f" @{t['owner']}" if t.get("owner") else ""
        print(f"  #{t['id']:<4} [{t['status']:<11}] p{t.get('priority')} {t.get('repo') or '':<14} {short(t['title'], 70)}{owner}"
              f"{deps_badge(t)}")
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


OBSERVED = {"working": "trabajando", "waiting": "esperando respuesta", "idle": "libre", "stale": "parado (sin escrituras)"}


def ago(ts, now: float | None = None) -> str:
    try:
        s = max(0, int((now if now is not None else time.time()) - float(ts)))
    except (TypeError, ValueError):
        return ""
    if s < 90:
        return f"hace {s} s"
    if s < 5400:
        return f"hace {round(s / 60)} min"
    if s < 172800:
        return f"hace {round(s / 3600)} h"
    return f"hace {round(s / 86400)} d"


def observed_text(o: dict | None, now: float | None = None) -> str:
    """«ejecutando exec hace 2 min»: the state a transcript shows, with the tool and since when."""
    if not o:
        return "sin transcripción observada"
    state = o.get("state")
    tool = (o.get("tool") or {}).get("name")
    what = f"ejecutando {tool}" if state == "tool" and tool else OBSERVED.get(state, str(state))
    if state == "waiting" and tool:
        what += f" ({tool})"
    return f"{what} {ago(o.get('since'), now)}".strip()


def print_leaders(r: dict) -> None:
    """One line per leader: its CLI's live state, since when, where it was read and its open tasks."""
    for lead in r.get("leaders", []):
        flag = "  <-- INACTIVO CON TRABAJO" if lead.get("idle_with_work") else ""
        tasks = lead.get("open_tasks") or []
        print(f"{lead.get('name', lead.get('id')):<8} {lead.get('label', lead.get('state')):<24} "
              f"tareas abiertas: {', '.join('#' + str(t) for t in tasks) if tasks else '-'}{flag}")
        print(f"         {lead.get('detail', '')} [{lead.get('source', '-')}]")


def print_watch(board: dict, w: dict) -> None:
    now = time.time()
    seen = set()
    print("AGENTES (latido del Ágora · estado observado en su transcripción)")
    rows = [a for a in board.get("agents", []) if a.get("id") != "luis"]
    for a in rows:
        seen.add(a["id"])
        o = (w.get("agents") or {}).get(a["id"])
        flag = " (sin señales)" if a.get("stale") else ""
        print(f"  {a['id']:<16} latido {when(a.get('last_seen'))}{flag} · observado: {observed_text(o, now)}")
        if o:
            extra = [x for x in (o.get("engine"), o.get("binding")) if x]
            print(f"      {short(o.get('title'), 90)}" + (f"  [{', '.join(extra)}]" if extra else ""))
    for aid, o in sorted((w.get("agents") or {}).items()):
        if aid in seen:
            continue
        print(f"  {aid:<16} sin latido · observado: {observed_text(o, now)}")
        print(f"      {short(o.get('title'), 90)}  [{o.get('engine')}, {o.get('binding')}]")
    qs = w.get("questions") or []
    print(f"\nPREGUNTAS PENDIENTES ({len(qs)})")
    for q in qs:
        who = q.get("agent") or q.get("session_key")
        guess = "" if q.get("confidence") == "explicit" else " (probable)"
        print(f"  {who} · {q.get('engine')} · {q.get('kind')}{guess} {ago(q.get('since'), now)}: {short(q.get('what'), 110)}")
    un = w.get("unbound") or []
    print(f"\nSESIONES SIN ASIGNAR ({len(un)}; agora watch-bind <clave> <agente>)")
    for s in un[:20]:
        print(f"  {s['key']:<48} {s.get('state'):<8} {ago(s.get('last_activity'), now):<12} {short(s.get('title'), 60)}")


def print_progress(tasks: list[dict]) -> None:
    for task in tasks:
        activity = task.get("activity") or {}
        print(f"  REVISAR PROGRESO #{task['id']} {task['title']} (de {task.get('owner') or '-'})")
        print(f"    Último informe: {when(activity.get('last_progress_at'))} · "
              f"latido: {when(activity.get('heartbeat_at'))} · señal orientativa; no cambia el estado")


def print_inbox(r: dict) -> None:
    c = r.get("counts", {})
    print(f"Buzón de {r.get('agent')}: {c.get('messages', 0)} mensajes ({c.get('for_you', 0)} para ti), "
          f"{c.get('reviews', 0)} revisiones pendientes, {c.get('changes', 0)} cambios pedidos, {c.get('escalated', 0)} escalados")
    for t in r.get("reviews", []):
        print(f"  REVISAR  #{t['id']} {t['title']} (de {t.get('owner')}, rama {t.get('branch') or '-'}, commits {', '.join(t.get('commits') or []) or '-'})")
    for t in r.get("changes", []):
        print(f"  CAMBIOS  #{t['id']} {t['title']} (revisor {t.get('reviewer')})")
    print_progress(r.get("progress_alerts", []))
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
    for q in r.get("out_of_quota") or []:
        print(f"  SIN CUOTA {q.get('note') or q.get('agent')}: no le pases trabajo ni revisiones")
    for m in r.get("messages", []):
        mark = "→" if m.get("for_you") else " "
        where = f"tarea #{m['task_id']}" if m.get("thread_kind") == "task" else f"hilo {m['thread_id']}"
        print(f"{mark} [{where} · {m['thread_kind']}] {m['author']} ({m['kind']}, {when(m['created'])}) — {m.get('thread_title')}")
        if "body" not in m and "body_chars" in m:      # inbox --meta: the body is read with thread/task
            print(f"      ({m['body_chars']} caracteres; léelo con thread {m['thread_id']})")
            continue
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
    print_progress(w.get("progress", []))


def print_conversation(r: dict) -> None:
    head = r.get("task") or {}
    th = r.get("thread") or {}
    if head:
        print(f"#{head['id']} [{head['status']}] {head['title']} — owner {head.get('owner') or '-'}, revisor "
              f"{head.get('reviewer') or '-'}, repo {head.get('repo') or '-'}, rutas {', '.join(head.get('paths') or []) or '-'}")
        if head.get("branch") or head.get("commits"):
            print(f"  rama {head.get('branch') or '-'} · commits {', '.join(head.get('commits') or []) or '-'}")
        print(f"  entrega r{head.get('submission_revision', 0)} · revisión votada {head.get('reviewed_submission_revision') or '-'}")
        if head.get("blocked_by"):
            print("  espera a: " + ", ".join(f"#{b['id']} [{b['status']}]" for b in head["blocked_by"]) + deps_badge(head))
        if head.get("dependents"):
            print("  bloquea a: " + ", ".join(f"#{i}" for i in head["dependents"]))
        for b in head.get("submission_blocks") or []:
            kind = b.get("kind")
            label = f"  ▸ [{b.get('id')}] {kind}" + (f" — {b.get('title')}" if b.get("title") else "")
            if kind == "code_peek" and isinstance(b.get("peek"), dict):
                peek = b["peek"]
                print(f"{label}: {peek.get('ref')} ({'ok' if peek.get('verified') else '??'})")
            else:
                print(label)
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


def print_handover(r: dict) -> None:
    if r.get("noop"):
        print(f"Sin cambios: {r.get('from')} ya no tiene tareas ni revisiones que traspasar a {r.get('to')}.")
    else:
        forced = " [forzado]" if r.get("forced") else ""
        print(f"Traspaso de {r.get('from')} a {r.get('to')} (por {r.get('by')}){forced}: {r.get('reason')}")
        for t in r.get("tasks", []):
            what = []
            if t["id"] in r.get("owner_moved", []):
                what.append("propietario")
            if t["id"] in r.get("reviewer_moved", []):
                what.append("revisor")
            print(f"  #{t['id']:<4} [{t['status']:<11}] {short(t['title'], 60)} -> {'+'.join(what)}"
                  f" (owner {t.get('owner') or '-'}, revisor {t.get('reviewer') or '-'})")
        print(f"  bloqueos movidos: {len(r.get('locks_moved', []))}; anuncio en el hilo {r.get('thread_id')}")
    for sk in r.get("skipped", []):
        print(f"  omitida #{sk['task_id']}: {sk['reason']}")


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


def debate_request(a: argparse.Namespace, ag: str | None, p: argparse.ArgumentParser) -> dict:
    """``agora debate open|finding|challenge|verify|reject|close|show|list`` → the hub's debate routes."""
    v = a.dcmd
    if v == "open":
        if not a.thread and not a.task:
            p.error("debate open necesita un hilo o --task")
        body = {"agent": ag, "participants": csv(a.participants), "files": csv(a.files), "commits": csv(a.commits)}
        body.update({k: val for k, val in (("thread_id", a.thread), ("task_id", a.task), ("title", a.title),
                                           ("question", text_arg(a, "question")), ("repo", a.repo)) if val})
        return call("POST", "/api/agora/debate_open", body)
    if v == "finding":
        body = {"agent": ag}
        if a.amend:
            body["finding_id"] = a.amend
        elif a.debate:
            body["debate_id"] = a.debate
        else:
            p.error("debate finding necesita el id del debate (o --amend <hallazgo>)")
        claim = text_arg(a, "claim")
        if claim:
            body["claim"] = claim
        evidence: list = list(a.evidence or [])
        if a.evidence_file:
            try:
                raw = sys.stdin.read() if a.evidence_file == "-" else Path(a.evidence_file).read_text(encoding="utf-8-sig")
                loaded = json.loads(raw)
            except (OSError, ValueError) as exc:
                p.error(f"--evidence-file necesita JSON UTF-8 válido: {exc}")
            evidence.extend(loaded if isinstance(loaded, list) else [loaded])
        if evidence:
            body["evidence"] = evidence
        if a.disposition:
            body["disposition"] = a.disposition
        return call("POST", "/api/agora/debate_finding", body)
    if v in ("challenge", "verify", "reject"):
        return call("POST", f"/api/agora/debate_{v}", {"agent": ag, "finding_id": a.finding.lstrip("Ff#"),
                                                       "note": text_arg(a, "note") or ""})
    if v == "close":
        disp = {}
        for item in csv(a.dispositions):
            key, _, value = item.partition("=")
            if not value:
                p.error("--disposition va como F12=adopt")
            disp[key.strip().lstrip("Ff#")] = value.strip()
        body = {"agent": ag, "debate_id": a.debate, "outcome": text_arg(a, "outcome") or "",
                "spawn_tasks": a.spawn_tasks, "dispositions": disp,
                "spawn_findings": [x.lstrip("Ff#") for x in csv(a.spawn)]}
        if a.task_kind:
            body["task_kind"] = a.task_kind
        return call("POST", "/api/agora/debate_close", body)
    if v == "show":
        return call("GET", f"/api/agora/debates/{a.debate}")
    q = {k: val for k, val in (("thread_id", a.thread), ("task_id", a.task), ("status", a.status), ("full", "1")) if val}
    return call("GET", "/api/agora/debates?" + urllib.parse.urlencode(q))


def print_debate(r: dict) -> None:
    """One debate (or each listed one): findings grouped by status with who verifies, challenges or rejects."""
    if isinstance(r.get("finding"), dict):        # filed, amended, verified, challenged or rejected: one line
        f = r["finding"]
        who = " · ".join(f"{k.split('_')[0]}: {', '.join(f[k])}" for k in ("verified_by", "challenged_by", "rejected_by") if f.get(k))
        print(f"ok · F{f['id']} [{f['status']} · {f['disposition']}] debate #{f['debate_id']}: {short(f['claim'], 160)}"
              + (f"  ({who})" if who else ""))
        print(f"  consenso: {(r.get('consensus') or {}).get('summary', '-')}")
        return
    views = r.get("debates") if "debates" in r else [r]
    if not views:
        print("Sin debates.")
    for view in views:
        d = view.get("debate") or view
        print(f"Debate #{d['id']} [{d['status']}] {d['title']} · hilo {d['thread_id']} · {', '.join(d.get('participants') or [])}")
        snap = d.get("snapshot") or {}
        if snap:
            print(f"  instantánea: {len(snap.get('files') or [])} fichero(s), {len(snap.get('commits') or [])} commit(s)"
                  + (f" @ {str(snap['head'])[:10]}" if snap.get("head") else "") + f" · sha256 {str(snap.get('digest'))[:12]}")
        cons = view.get("consensus") or {}
        print(f"  consenso: {cons.get('summary', '-')}")
        for who, n in (cons.get("participants") or {}).items():
            print(f"    {who}: {n['filed']} archivados · {n['verify']} verifica · {n['challenge']} cuestiona · {n['reject']} rechaza")
        for status in ("proposed", "challenged", "verified", "rejected"):
            for f in [x for x in view.get("findings") or [] if x["status"] == status]:
                who = " · ".join(f"{k.split('_')[0]}: {', '.join(f[k])}" for k in ("verified_by", "challenged_by", "rejected_by") if f.get(k))
                print(f"  F{f['id']} [{status} · {f['disposition']}] {f['author']}: {short(f['claim'], 200)}" + (f"  ({who})" if who else ""))
                for e in f.get("evidence") or []:
                    print(f"      - [{e['kind']}] {short(e.get('ref') or e.get('note'), 160)}")
        if d.get("status") == "closed":
            print(f"  resultado: {short(d.get('outcome'), 300)} · decisión en el hilo {d.get('decision_thread_id')}"
                  + (" · tareas " + ", ".join(f"#{t}" for t in d.get("spawned_tasks") or []) if d.get("spawned_tasks") else ""))


# ---- commands ---------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global _SELECTED_SPACE
    p = argparse.ArgumentParser(prog="agora", description="Ágora de Hoard Hub desde la terminal")
    p.add_argument("--as", dest="agent", default=os.environ.get("AGORA_AGENT"), help="tu id de agente (el mismo siempre)")
    p.add_argument("--json", action="store_true", help="imprime la respuesta tal cual")
    p.add_argument("--space", default=os.environ.get("AGORA_SPACE", ""), help="id del espacio; por defecto ecosystem")
    sub = p.add_subparsers(dest="cmd", required=True)

    def texts(sp, *names):
        for n in names:
            sp.add_argument(f"--{n}")
            sp.add_argument(f"--{n}-file", dest=f"{n}_file")

    sub.add_parser("spaces", help="lista espacios persistentes")
    sub.add_parser("space", help="lee el espacio seleccionado con --space")
    s = sub.add_parser("space-create", help="crea un espacio sin cambiar recursos ni cuotas globales")
    s.add_argument("title"); s.add_argument("--id"); s.add_argument("--project-id"); s.add_argument("--workspace")
    texts(s, "description")
    s = sub.add_parser("space-update", help="edita el espacio conservando su id e historial")
    s.add_argument("--revision", type=int, required=True); s.add_argument("--title"); s.add_argument("--project-id"); s.add_argument("--workspace")
    texts(s, "description")

    s = sub.add_parser("board"); s.add_argument("--full", action="store_true", help="todos los bloqueos, uno a uno")
    sub.add_parser("usage", help="cuotas recientes de los pools de cuenta compartidos")
    sub.add_parser("leaders", help="estado en vivo de cada líder (trabajando, esperando, inactivo, sesión caducada, error) "
                                    "leído de sus ficheros de sesión")
    s = sub.add_parser("usage-report", help="publica un snapshot de cuota sanitizado desde JSON UTF-8")
    s.add_argument("--data-file", required=True, help="JSON UTF-8 con provider_id, pool_id, pool_label, source y windows")
    s = sub.add_parser("inbox"); s.add_argument("--wait", type=float, default=0); s.add_argument("--peek", action="store_true"); s.add_argument("--mine", action="store_true"); s.add_argument("--meta", action="store_true", help="solo metadatos (quién, dónde, tipo), sin cuerpos ni mover la marca"); s.add_argument('--all-spaces', action='store_true', help='buzón de todos los espacios con cursores independientes')
    s = sub.add_parser("hb"); s.add_argument("doing", nargs="?", default=""); s.add_argument("--state", default="working"); s.add_argument("--name")
    renewal = s.add_mutually_exclusive_group()
    renewal.add_argument("--renew-resource", action="append", help="renueva solo estos recursos exactos; repetible")
    renewal.add_argument("--no-renew-locks", action="store_true", help="registra estado sin renovar bloqueos")
    s = sub.add_parser("sync", help="un latido y snapshot del Ágora; --since usa el id durable de publicación")
    s.add_argument("doing", nargs="?", default=""); s.add_argument("--state", default="working"); s.add_argument("--name")
    s.add_argument("--note", help="nota breve del agente que se guarda en su heartbeat")
    s.add_argument("--since", dest="since_id", type=int, default=0,
                   help="id de mensaje devuelto en la última consulta del mismo filtro (por defecto: 0)")
    s.add_argument("--thread", action="append", type=int,
                   help="limita publicaciones a un hilo; repítelo para varios y conserva un cursor por filtro")
    s.add_argument("--limit", type=int, default=100, help="máximo de publicaciones por llamada (1–500)")
    renewal = s.add_mutually_exclusive_group()
    renewal.add_argument("--renew-resource", action="append", help="renueva solo estos recursos exactos; repetible")
    renewal.add_argument("--no-renew-locks", action="store_true", help="registra estado sin renovar bloqueos")
    s = sub.add_parser("checkpoint", help="guarda progreso declarado sin renovar bloqueos ni cambiar el estado")
    s.add_argument("task", type=int); s.add_argument("--data-file", required=True, help="JSON UTF-8 del payload; - lee stdin")
    s.add_argument("--expected-revision", type=int, required=True, help="revisión leída; 0 para el primer checkpoint")
    s = sub.add_parser("checkpoints", help="historial de progreso declarado de una tarea")
    s.add_argument("task", type=int); s.add_argument("--after", type=int, default=0); s.add_argument("--limit", type=int, default=100)
    s = sub.add_parser("activity", help="evidencia de progreso de una tarea, separada del latido")
    s.add_argument("task", type=int); s.add_argument("--idle-after", type=int)
    s = sub.add_parser("timeline", help="cronología de tarea con mensajes, cambios y checkpoints")
    s.add_argument("task", type=int); s.add_argument("--after", type=int, default=0); s.add_argument("--limit", type=int, default=100)
    s = sub.add_parser("add"); s.add_argument("title"); texts(s, "body"); s.add_argument("--repo"); s.add_argument("--paths", action="append")
    s.add_argument("--kind", default="feature"); s.add_argument("--prio", type=int, default=2); s.add_argument("--claim", action="store_true")
    s.add_argument("--lock", action="append"); s.add_argument("--mention", action="append")
    s.add_argument("--blocked-by", action="append", help="ids de las tareas a las que espera (12,13 o repetido)")
    s = sub.add_parser("deps", help="a qué tareas espera una: --add / --remove ids, o --set (vacío las quita todas)")
    s.add_argument("task", type=int); s.add_argument("--add", action="append"); s.add_argument("--remove", action="append")
    s.add_argument("--set", action="append")
    s = sub.add_parser("claim"); s.add_argument("task", type=int); s.add_argument("--lock", action="append"); s.add_argument("--ttl", type=int); s.add_argument("--force", action="store_true")
    s = sub.add_parser("update"); s.add_argument("task", type=int); texts(s, "note"); s.add_argument("--status"); s.add_argument("--branch")
    s.add_argument("--paths", action="append"); s.add_argument("--lock", action="append")
    s = sub.add_parser("submit"); s.add_argument("task", type=int); texts(s, "summary"); s.add_argument("--commits", action="append"); s.add_argument("--branch"); s.add_argument("--reviewer")
    s.add_argument("--blocks-file", help="JSON UTF-8 list of structured review blocks (what_why, paths, code_peek, flow, sequence)")
    s = sub.add_parser("review"); s.add_argument("task", type=int); s.add_argument("verdict", choices=["approve", "changes"]); s.add_argument("--revision", type=int, required=True, help="submission_revision you inspected before reviewing; not the latest revision fetched automatically"); texts(s, "body")
    s.add_argument("--block-comments-file", help="JSON UTF-8 list of {block_id, body} notes on structured blocks")
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
    s = sub.add_parser("handover", help="traspasa las tareas (bloqueos y revisiones) de un agente caido a su relevo")
    s.add_argument("--from", dest="from_agent", required=True, help="agente que ya no responde")
    s.add_argument("--to", dest="to_agent", required=True, help="agente que retoma el trabajo (con latido reciente)")
    texts(s, "reason")
    s.add_argument("--tasks", action="append", help="solo estas tareas (ids separados por comas; repetible)")
    s.add_argument("--no-reviews", action="store_true", help="no traspasa el papel de revisor")
    s.add_argument("--force", action="store_true",
                   help="traspasa ya aunque el origen siga mandando latidos (orden de Luis: sin cuota, trabajo urgente)")
    sub.add_parser("servers", help="cada espacio como servidor: tareas, quién anda por ahí y qué hay nuevo para ti")
    sub.add_parser("channels", help="canales del espacio (general siempre existe) con lo que no has leído")
    s = sub.add_parser("channel", help="crea un canal en el espacio, o lo cambia si ya existe (--archive lo archiva)")
    s.add_argument("name"); s.add_argument("--title"); s.add_argument("--topic"); s.add_argument("--category")
    s.add_argument("--position", type=int); s.add_argument("--archive", action="store_true"); s.add_argument("--restore", action="store_true")
    s = sub.add_parser("say", help="escribe en un canal (general por defecto); @id menciona, @here a quien ande por ahí")
    s.add_argument("words", nargs="*", help="[canal] texto: con una sola palabra o frase, va a general"); texts(s, "body")
    s.add_argument("--kind", default="comment"); s.add_argument("--mention", action="append"); s.add_argument("--thread", type=int)
    s.add_argument("--reply", type=int, help="cita ese mensaje (y avisa a quien lo escribió)")
    s.add_argument("--attach", action="append", help="ruta de un fichero que el mensaje lleva adjunto (10 MB como mucho; repetible)")
    s = sub.add_parser("react", help="reacciona a un mensaje con un emoji o una palabra corta: react 1234 👍 (--off la quita)")
    s.add_argument("message", type=int); s.add_argument("emoji"); s.add_argument("--off", action="store_true")
    s = sub.add_parser("file", help="descarga un fichero adjunto por su id: file <id> [--out ruta]")
    s.add_argument("id"); s.add_argument("--out")
    s = sub.add_parser("members", help="quién está en el servidor, también los que hace mucho que no vienen")
    s.add_argument("--q", help="filtra por nombre")
    s = sub.add_parser("member-add", help="da de alta a un agente en el servidor por su nombre (codex-docs, claude-ui…)")
    s.add_argument("id"); s.add_argument("--note")
    s = sub.add_parser("member-remove", help="da de baja a un agente del servidor: su trabajo queda libre, o pasa a --to")
    s.add_argument("id"); s.add_argument("--to"); s.add_argument("--reason"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("members-prune", help="da de baja a quien no viene desde hace --days días (30) y no tiene nada en marcha")
    s.add_argument("--days", type=int, default=30); s.add_argument("--dry-run", action="store_true")
    sub.add_parser("ranks", help="los rangos del servidor: la escalera, qué manda un rango más alto y el de cada miembro")
    s = sub.add_parser("assign", help="asigna una tarea a un agente de menos rango que tú: assign <tarea> <agente> [--note \"…\"]")
    s.add_argument("task", type=int); s.add_argument("to"); s.add_argument("--note")
    s = sub.add_parser("read", help="últimos mensajes de un canal (general por defecto) o de un hilo con --thread")
    s.add_argument("channel", nargs="?", default="general"); s.add_argument("--limit", type=int, default=30)
    s.add_argument("--before", type=int); s.add_argument("--peek", action="store_true"); s.add_argument("--thread", type=int)
    s = sub.add_parser("feed", help="entregas del espacio (deliveries), lo que espera revisión (reviews) o los últimos mensajes (activity)")
    s.add_argument("--kind", default="deliveries", choices=["deliveries", "reviews", "activity"]); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("search", help="dónde se dijo algo: mensajes, tareas e hilos del espacio (o de todos con --all)")
    s.add_argument("q"); s.add_argument("--all", action="store_true"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("task"); s.add_argument("task", type=int)
    s = sub.add_parser("thread"); s.add_argument("thread", type=int)
    s = sub.add_parser("ack"); s.add_argument("thread", type=int, nargs="?"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("digest"); s.add_argument("--hours", type=float, default=12)
    s = sub.add_parser("tasks"); s.add_argument("--status"); s.add_argument("--owner"); s.add_argument("--repo")
    sub.add_parser("decisions")
    s = sub.add_parser("orphans", help="trabajo que nadie mueve (revisor o dueño callado o sin cuota) y quién puede cogerlo")
    s.add_argument("--sweep", action="store_true", help="anunciarlas ya en los hilos (cada hallazgo una sola vez)")
    sub.add_parser("watch", help="estado real de cada agente según su transcripción, preguntas pendientes y sesiones sin asignar")
    s = sub.add_parser("watch-bind", help="asigna una sesión observada a un agente (agente '-' o vacío la desasigna)")
    s.add_argument("session_key", help="clave que da 'watch', p. ej. codex:<id de sesión>"); s.add_argument("agent_id")
    s = sub.add_parser("debate", help="debate estructurado con hallazgos dentro de un hilo: open|finding|challenge|verify|reject|close|show|list")
    dsub = s.add_subparsers(dest="dcmd", required=True)
    d = dsub.add_parser("open", help="abre un debate en un hilo (o en el hilo de una tarea con --task) y congela la pregunta")
    d.add_argument("thread", type=int, nargs="?"); d.add_argument("--task", type=int); d.add_argument("--title")
    texts(d, "question"); d.add_argument("--with", dest="participants", action="append", help="participantes (ids, comas o repetido)")
    d.add_argument("--file", dest="files", action="append", help="fichero enlazado: ruta o ruta#L10-L20 (repetible)")
    d.add_argument("--commit", dest="commits", action="append"); d.add_argument("--repo")
    d = dsub.add_parser("finding", help="archiva un hallazgo (o corrige el tuyo con --amend <id>)")
    d.add_argument("debate", type=int, nargs="?"); texts(d, "claim")
    d.add_argument("--evidence", action="append", help="ruta#L1-L9, commit, URL o '$ comando' (repetible)")
    d.add_argument("--evidence-file", help="JSON UTF-8: lista de {kind, ref, output, note}")
    d.add_argument("--disposition", choices=["adopt", "adapt", "drop", "needs_luis"]); d.add_argument("--amend", type=int)
    for verb in ("challenge", "verify", "reject"):
        d = dsub.add_parser(verb, help={"challenge": "cuestiona un hallazgo con una nota", "verify": "verifica un hallazgo ajeno",
                                        "reject": "rechaza un hallazgo (el autor lo retira)"}[verb])
        d.add_argument("finding", help="id del hallazgo (F12 o 12)"); texts(d, "note")
    d = dsub.add_parser("close", help="cierra con el resultado (registro de decisiones) y opcionalmente crea tareas")
    d.add_argument("debate", type=int); texts(d, "outcome"); d.add_argument("--spawn-tasks", action="store_true")
    d.add_argument("--spawn", action="append", help="solo estos hallazgos (ids)"); d.add_argument("--task-kind")
    d.add_argument("--disposition", dest="dispositions", action="append", help="F12=adopt (repetible)")
    d = dsub.add_parser("show", help="un debate: hallazgos por estado, quién verifica qué, rondas"); d.add_argument("debate", type=int)
    d = dsub.add_parser("list", help="debates (abiertos primero)"); d.add_argument("--thread", type=int); d.add_argument("--task", type=int)
    d.add_argument("--status", choices=["open", "closed"])
    a = p.parse_args(argv)
    for key, value in list(vars(a).items()):
        if isinstance(value, str) and not key.endswith("_file"):
            setattr(a, key, fix_text(value))
        elif isinstance(value, list):
            setattr(a, key, [fix_text(v) if isinstance(v, str) else v for v in value])

    _SELECTED_SPACE = a.space
    reads = {"members", "ranks", "file", "servers", "channels", "read", "feed", "search", "spaces", "space", "board", "usage", "task", "thread", "tasks", "decisions", "locks", "digest", "checkpoints", "activity", "timeline", "watch", "watch-bind", "leaders", "orphans"}
    if a.cmd not in reads and not (a.cmd == "debate" and a.dcmd in ("show", "list")) and not a.agent:
        p.error("falta --as <agente> (o AGORA_AGENT)")
    ag = a.agent
    c = a.cmd
    if c == "spaces":
        r = call("GET", "/api/agora/spaces")
    elif c == "space":
        r = call("GET", "/api/agora/spaces/" + urllib.parse.quote(a.space or "ecosystem", safe=""))
    elif c in ("space-create", "space-update"):
        payload = {"agent": ag}
        for key in ("title", "project_id", "workspace"):
            if getattr(a, key, None) is not None:
                payload[key] = getattr(a, key)
        description = text_arg(a, "description")
        if description is not None:
            payload["description"] = description
        if c == "space-create" and a.id:
            payload["id"] = a.id
        if c == "space-update":
            payload["expected_revision"] = a.revision
        r = call("POST", "/api/agora/" + c.replace("-", "_"), payload)
    elif c == "board":
        r = call("GET", "/api/agora/board")
    elif c == "usage":
        r = call("GET", "/api/agora/console/usage")
    elif c == "orphans":
        r = call("POST", "/api/agora/orphan_sweep", {"agent": ag, "force": True}) if a.sweep else call("GET", "/api/agora/orphans")
    elif c == "leaders":
        r = call("GET", "/api/agora/console/leaders")
    elif c == "usage-report":
        try:
            raw = sys.stdin.read() if a.data_file == "-" else Path(a.data_file).read_text(encoding="utf-8-sig")
            def reject_constant(value):
                raise ValueError(f"invalid JSON constant: {value}")
            payload = json.loads(raw, parse_constant=reject_constant)
        except (OSError, ValueError) as exc:
            p.error(f"--data-file necesita JSON UTF-8 válido: {exc}")
        if not isinstance(payload, dict):
            p.error("--data-file debe contener un objeto JSON")
        payload["agent"] = ag
        r = call("POST", "/api/agora/usage/report", payload)
    elif c == "watch":
        r = call("GET", "/api/agora/watch")
        if r.get("ok", True):
            b = call("GET", "/api/agora/board")
            r = {**r, "board": b if b.get("ok", True) else {}}
    elif c == "watch-bind":
        who = a.agent_id.strip()
        r = call("POST", "/api/agora/watch/bind", {"session_key": a.session_key, "agent": "" if who == "-" else who})
    elif c == "checkpoint":
        try:
            raw = sys.stdin.read() if a.data_file == "-" else Path(a.data_file).read_text(encoding="utf-8-sig")
            def reject_constant(value):
                raise ValueError(f"invalid JSON constant: {value}")
            payload = json.loads(raw, parse_constant=reject_constant)
        except (OSError, ValueError) as exc:
            p.error(f"--data-file necesita JSON UTF-8 válido: {exc}")
        r = call("POST", "/api/agora/checkpoint", {"agent": ag, "task_id": a.task,
                                                   "expected_revision": a.expected_revision, "payload": payload})
    elif c == "checkpoints":
        r = call("GET", "/api/agora/checkpoints?" + urllib.parse.urlencode(
            {"task_id": a.task, "after_revision": a.after, "limit": a.limit}))
    elif c == "inbox":
        q = {"agent": ag, "wait_s": a.wait, "peek": "1" if a.peek else "", "mine_only": "1" if a.mine else "",
             "metadata_only": "1" if a.meta else "", 'all_spaces': '1' if a.all_spaces else ''}
        r = call("GET", "/api/agora/inbox?" + urllib.parse.urlencode(q), timeout=max(30.0, a.wait + 30))
    elif c == "activity":
        q = {"task_id": a.task}
        if a.idle_after is not None:
            q["idle_after_s"] = a.idle_after
        r = call("GET", "/api/agora/activity?" + urllib.parse.urlencode(q))
    elif c == "timeline":
        r = call("GET", "/api/agora/timeline?" + urllib.parse.urlencode(
            {"task_id": a.task, "after_id": a.after, "limit": a.limit}))
    elif c == "sync":
        r = call("POST", "/api/agora/sync", {"agent": ag, "doing": a.doing, "state": a.state,
                                                   "name": a.name, "note": a.note, "since_id": a.since_id,
                                                   "thread_ids": a.thread or [], "limit": a.limit,
                                                   **({"renew_resources": [] if a.no_renew_locks else a.renew_resource}
                                                      if a.no_renew_locks or a.renew_resource is not None else {})})
    elif c == "hb":
        r = call("POST", "/api/agora/heartbeat", {"agent": ag, "doing": a.doing, "state": a.state, **({"name": a.name} if a.name else {}),
                  **({"renew_resources": [] if a.no_renew_locks else a.renew_resource}
                     if a.no_renew_locks or a.renew_resource is not None else {})})
    elif c == "add":
        r = call("POST", "/api/agora/task_add", {"agent": ag, "title": a.title, "body": text_arg(a, "body") or "", "repo": a.repo or "",
                                                 "paths": csv(a.paths), "kind": a.kind, "priority": a.prio, "claim": a.claim,
                                                 "locks": csv(a.lock), "mentions": csv(a.mention),
                                                 "blocked_by": [x.lstrip("#") for x in csv(a.blocked_by)]})
    elif c == "deps":
        body = {"agent": ag, "task_id": a.task}
        for key in ("add", "remove", "set"):
            val = getattr(a, key)
            if val is not None:
                body[key] = [x.lstrip("#") for x in csv(val)]
        r = call("POST", "/api/agora/task_deps", body)
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
        body = {"agent": ag, "task_id": a.task, "summary": text_arg(a, "summary") or "",
                "commits": csv(a.commits), "branch": a.branch, "reviewer": a.reviewer}
        if getattr(a, "blocks_file", None):
            try:
                raw = sys.stdin.read() if a.blocks_file == "-" else Path(a.blocks_file).read_text(encoding="utf-8-sig")
                body["blocks"] = json.loads(raw)
            except (OSError, ValueError) as exc:
                p.error(f"--blocks-file necesita JSON UTF-8 válido: {exc}")
        r = call("POST", "/api/agora/task_submit", body)
    elif c == "review":
        body = {"agent": ag, "task_id": a.task, "verdict": a.verdict, "body": text_arg(a, "body") or "",
                "expected_submission_revision": a.revision}
        if getattr(a, "block_comments_file", None):
            try:
                raw = (sys.stdin.read() if a.block_comments_file == "-"
                       else Path(a.block_comments_file).read_text(encoding="utf-8-sig"))
                body["block_comments"] = json.loads(raw)
            except (OSError, ValueError) as exc:
                p.error(f"--block-comments-file necesita JSON UTF-8 válido: {exc}")
        r = call("POST", "/api/agora/task_review", body)
    elif c == "done":
        r = call("POST", "/api/agora/task_done", {"agent": ag, "task_id": a.task, "result": text_arg(a, "result") or "",
                                                  "commits": csv(a.commits), "force": a.force, "reason": a.reason or ""})
    elif c == "release":
        r = call("POST", "/api/agora/task_release", {"agent": ag, "task_id": a.task, "reason": a.reason or "", "drop": a.drop})
    elif c == "handover":
        try:
            ids = [int(x.lstrip("#")) for x in csv(a.tasks)]
        except ValueError:
            p.error("--tasks necesita numeros de tarea separados por comas")
        body = {"agent": ag, "from_agent": a.from_agent, "to_agent": a.to_agent,
                "reason": text_arg(a, "reason") or "", "include_reviews": not a.no_reviews}
        if a.force:
            body["force"] = True
        if ids:
            body["tasks"] = ids
        r = call("POST", "/api/agora/handover", body)
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
    elif c == "servers":
        r = call("GET", "/api/agora/servers" + (f"?agent={urllib.parse.quote(ag)}" if ag else ""))
    elif c == "channels":
        r = call("GET", "/api/agora/channels" + (f"?agent={urllib.parse.quote(ag)}" if ag else ""))
    elif c == "channel":
        listed = call("GET", "/api/agora/channels?archived=1")
        known = {ch["slug"] for ch in listed.get("channels", [])} if listed.get("ok") else set()
        fields = {k: v for k, v in {"title": a.title, "topic": a.topic, "category": a.category, "position": a.position}.items() if v is not None}
        if a.archive or a.restore:
            fields["archived"] = bool(a.archive)
        slug = "".join(ch if ch.isalnum() else "-" for ch in a.name.lower()).strip("-")
        if slug in known or a.name in known:
            r = call("POST", "/api/agora/channel_update", {"agent": ag, "channel": a.name, **fields}) if fields else \
                {"ok": True, "channel": next(ch for ch in listed["channels"] if ch["slug"] in (slug, a.name))}
        else:
            r = call("POST", "/api/agora/channel_create", {"agent": ag, "slug": a.name, "title": a.title or a.name,
                                                           **{k: v for k, v in fields.items() if k in ("topic", "category")}})
    elif c == "say":
        body = text_arg(a, "body")
        words = list(a.words or [])
        if body is None:   # say "texto"  |  say canal "texto"
            channel, body = ("general", words[0]) if len(words) == 1 else (words[0] if words else "general", " ".join(words[1:]))
        else:              # say [canal] --body-file f
            channel = words[0] if words else "general"
        target = {"thread_id": a.thread} if a.thread else {"channel": channel.lstrip("#")}
        files, failed = [], None
        for path in a.attach or []:   # read here, with this terminal's own access to the disk: the hub never reads a path
            try:
                with open(os.path.expanduser(path), "rb") as fh:
                    blob = fh.read()
            except OSError as exc:
                failed = {"ok": False, "error": f"no se puede leer {path}: {exc.strerror or exc}"}
                break
            sent = call("POST", "/api/agora/file_add", {"agent": ag, "name": os.path.basename(path),
                                                        "content_base64": base64.b64encode(blob).decode("ascii")})
            if not sent.get("ok"):
                failed = sent
                break
            files.append(sent["file"]["id"])
        r = failed or call("POST", "/api/agora/channel_post", {
            "agent": ag, **target, "body": body or "", "kind": a.kind, "mentions": csv(a.mention),
            **({"reply_to": a.reply} if a.reply else {}), **({"files": files} if files else {})})
    elif c == "react":
        r = call("POST", "/api/agora/react", {"agent": ag, "message_id": a.message, "emoji": a.emoji, "on": not a.off})
    elif c == "file":
        r = fetch_file(a.id, a.out)
    elif c == "members":
        r = call("GET", "/api/agora/members" + ("?" + urllib.parse.urlencode({"q": a.q}) if a.q else ""))
    elif c == "member-add":
        r = call("POST", "/api/agora/member_add", {"agent": ag, "id": a.id, **({"note": a.note} if a.note else {})})
    elif c == "member-remove":
        r = call("POST", "/api/agora/member_remove", {"agent": ag, "id": a.id, "force": a.force,
                                                      **({"to_agent": a.to} if a.to else {}),
                                                      **({"reason": a.reason} if a.reason else {})})
    elif c == "members-prune":
        r = call("POST", "/api/agora/members_prune", {"agent": ag, "older_than_days": a.days, "dry_run": a.dry_run})
    elif c == "ranks":
        r = call("GET", "/api/agora/ranks")
    elif c == "assign":
        r = call("POST", "/api/agora/task_assign", {"agent": ag, "task_id": a.task, "to_agent": a.to,
                                                    **({"note": a.note} if a.note else {})})
    elif c == "read":
        q = {"limit": a.limit}
        if ag:
            q["agent"] = ag
        if a.before:
            q["before_id"] = a.before
        if a.peek:
            q["peek"] = "1"
        if a.thread:
            r = call("POST", "/api/agora/channel_read", {**q, "thread_id": a.thread, "peek": a.peek})
        else:
            r = call("GET", "/api/agora/channels/" + urllib.parse.quote(a.channel.lstrip("#").lower(), safe="") + "?" + urllib.parse.urlencode(q))
    elif c == "feed":
        r = call("GET", "/api/agora/feed?" + urllib.parse.urlencode({"kind": a.kind, "limit": a.limit}))
    elif c == "search":
        r = call("GET", "/api/agora/search?" + urllib.parse.urlencode({"q": a.q, "limit": a.limit, **({"all_spaces": "1"} if a.all else {})}))
    elif c == "resolve":
        r = call("POST", "/api/agora/resolve", {"agent": ag, "thread_id": a.thread, "resolution": text_arg(a, "text") or ""})
    elif c == "escalate":
        r = call("POST", "/api/agora/escalate", {"agent": ag, "thread_id": a.thread, "question": text_arg(a, "question") or ""})
    elif c == "reopen":
        r = call("POST", "/api/agora/reopen", {"agent": ag, "thread_id": a.thread, "reason": a.reason})
    elif c == "task":
        r = call("GET", f"/api/agora/tasks/{a.task}" + (f"?agent={urllib.parse.quote(ag)}" if ag else ""))
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
    elif c == "debate":
        r = debate_request(a, ag, p)
    else:
        r = call("GET", "/api/agora/decisions")

    if a.json or not r.get("ok", True):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok", True) else 1
    if c == "board":
        print_board(r, full=a.full)
    elif c == "usage":
        print(json.dumps(r.get("providers", []), ensure_ascii=False, indent=2))
    elif c == "leaders":
        print_leaders(r)
    elif c == "activity":
        print(json.dumps(r["activity"], ensure_ascii=False, indent=2))
    elif c == "timeline":
        for event in r["events"]:
            print(f"{event['event_id']} · {when(event['created'])} · {event['author']} · {event['event_kind']}")
            print(event["checkpoint"]["payload"]["summary"] if event["event_kind"] == "checkpoint" else event["body"])
        print(f"Cursor: {r['next_after_id']}" + (" · quedan más (usa --after)" if r["has_more"] else ""))
    elif c == "watch":
        print_watch(r.get("board") or {}, r)
    elif c == "watch-bind":
        print(f"ok · {r.get('session_key')} → " + (f"{r.get('agent')} (asignación explícita)" if r.get("agent") else "sin agente (vuelve la inferencia)"))
    elif c in ("checkpoint", "checkpoints"):
        for cp in ([r["checkpoint"]] if c == "checkpoint" else r.get("checkpoints", [])):
            print(f"#{cp['task_id']} r{cp['revision']} · {cp['author']} · {when(cp['created'])}")
            print(json.dumps(cp["payload"], ensure_ascii=False, indent=2))
            locks = cp["current_lock_status"]
            if locks["missing"] or locks["conflicts"] or locks["owner_changed"]:
                print("AVISO: revisa propietario y bloqueos antes de retomar")
                print(json.dumps(locks, ensure_ascii=False, indent=2))
    elif c == "servers":
        for sv in r.get("servers", []):
            n = sv.get("counts") or {}
            new = (f" · {sv['unread']} sin leer" if sv.get("unread") else "") + (f" · {sv['mentions']} menciones" if sv.get("mentions") else "")
            wait = f" · te esperan {n.get('waiting')}" if n.get("waiting") else ""
            print(f"{sv['id']:<14} {short(sv['title'], 34):<34} en curso {n.get('active', 0):>3} · revisión {n.get('review', 0):>3} · "
                  f"abiertas {n.get('open', 0):>3} · conectados {sv.get('online', 0)}{wait}{new}")
    elif c == "channels":
        for ch in r.get("channels", []):
            new = (f" · {ch['unread']} sin leer" if ch.get("unread") else "") + (f" · {ch['mentions']} menciones" if ch.get("mentions") else "")
            last = f" · último {ch['last_author']} {when(ch['last_at'])}" if ch.get("last_author") else " · vacío"
            print(f"#{ch['slug']:<22} [{ch['category']}] {ch['messages']:>5} msj{last}{new}" + (f"\n    {ch['topic']}" if ch.get("topic") else ""))
    elif c == "channel":
        ch = r.get("channel") or {}
        print(f"ok · #{ch.get('slug')} [{ch.get('category')}]" + (" (archivado)" if ch.get("archived") else "") + (f" · {ch.get('topic')}" if ch.get("topic") else ""))
    elif c == "say":
        where = f"#{r.get('channel')}" if r.get("channel") else f"hilo {(r.get('thread') or {}).get('id')}"
        print(f"ok · mensaje {r.get('message_id')} en {where}" + (" · menciona a " + ", ".join(r["mentions"]) if r.get("mentions") else ""))
        print_quota_warnings(r)
    elif c == "react":
        shown = " · ".join(f"{x['emoji']} {x['count']}" for x in r.get("reactions") or []) or "ninguna"
        print(f"ok · mensaje {r.get('message_id')}: {shown}")
    elif c == "file":
        print(f"ok · {r.get('name')} guardado en {r.get('path')} ({r.get('size')} bytes)")
    elif c == "members":
        for m in r.get("members", []):
            seen = "todavía no ha entrado" if not m.get("last_seen") else "visto " + when(m["last_seen"])
            print(f"{m['id']:<30} {m['presence']:<9} {str(m.get('role') or '-'):<8} {str(m.get('rank') or '-'):<9} {seen}"
                  + (f" · {len(m['tasks'])} tareas" if m.get("tasks") else "") + (f" · {short(m['doing'], 70)}" if m.get("doing") else ""))
        print(f"{r.get('total', 0)} en el servidor")
    elif c == "ranks":
        print("Escalera: " + " > ".join(f"{x['label']} ({x['id']})" for x in r.get("ladder", [])) + f" · sin asignar: {r.get('default')}")
        for rule in r.get("rules", []):
            print("  - " + rule)
        named = [m for m in r.get("members", []) if m.get("rank") != r.get("default")]
        for m in named:
            print(f"{m['id']:<30} {m['rank']}")
        print(f"{len(named)} con rango propio de {len(r.get('members', []))} en el servidor")
    elif c == "assign":
        t = r.get("task") or {}
        print(f"ok · tarea #{t.get('id')} [{t.get('status')}] es de {t.get('owner')}"
              + (f" (antes, de {r['moved_from']})" if r.get("moved_from") else "") + (" · ya lo era" if r.get("noop") else ""))
    elif c == "member-add":
        print(f"ok · {(r.get('member') or {}).get('id')} " + ("dado de alta en el servidor" if r.get("added") else "ya estaba en el servidor"))
    elif c == "member-remove":
        parts = [f"ok · {r.get('removed')} fuera del servidor"]
        if r.get("handed_tasks"):
            parts.append("pasan a " + str(r.get("handed_to")) + ": " + ", ".join(f"#{i}" for i in r["handed_tasks"]))
        if r.get("freed_tasks"):
            parts.append("quedan libres: " + ", ".join(f"#{i}" for i in r["freed_tasks"]))
        if r.get("left_in_review"):
            parts.append("siguen en revisión a su nombre: " + ", ".join(f"#{i}" for i in r["left_in_review"]))
        print(" · ".join(parts))
    elif c == "members-prune":
        gone = r.get("removed") or []
        print(("saldrían " if r.get("dry_run") else "ok · fuera ") + f"{len(gone)}" + (": " + ", ".join(gone[:40]) + (" …" if len(gone) > 40 else "") if gone else ""))
        for k in r.get("kept") or []:
            print(f"  se queda {k['id']}: tiene {({'tasks': 'tareas', 'reviews': 'revisiones', 'leases': 'bloqueos'}).get(k['why'], k['why'])}")
    elif c == "read":
        th = r.get("thread") or {}
        print((f"#{r['channel']}" if r.get("channel") else f"hilo {th.get('id')}") + (f" · {th.get('title')}" if th else " · sin mensajes todavía"))
        seen_upto = r.get("seen_upto") or 0
        marked = False
        for m in r.get("messages", []):
            if m.get("kind") == "system":
                continue
            if ag and not marked and m["id"] > seen_upto and m.get("author") != ag:
                print("———— nuevo ————")
                marked = True
            print(f"\n— {m['author']} · {m['kind']} · {when(m['created'])} · #{m['id']}")
            quoted = m.get("reply")
            if quoted:
                print(f"  ↪ a {quoted.get('author') or '?'} (#{quoted['id']}): {short(quoted.get('excerpt'), 120)}")
            print(m.get("body") or "")
            for f in m.get("files") or []:
                print(f"  📎 {f['name']} ({f['size']} bytes) · agora file {f['id']}")
            if m.get("reactions"):
                print("  " + " · ".join(f"{x['emoji']} {x['count']}" for x in m["reactions"]))
        if (r.get("pagination") or {}).get("has_more"):
            print(f"\n(hay más: --before {r['pagination']['next_before_id']})")
    elif c == "feed":
        for it in r.get("items", []):
            if r.get("kind") == "activity":
                print(f"{when(it['created'])} {it['author']:<22} {short(it.get('thread_title'), 40)} · {short(it.get('body'), 110)}")
            else:
                commits = ", ".join(str(x)[:9] for x in (it.get("commits") or [])[:4])
                print(f"#{it['id']:<5} [{it['status']:<8}] {when(it.get('at'))} {str(it.get('owner') or '-'):<20} {short(it['title'], 70)}"
                      + (f"\n       {commits}" if commits else "") + (f"\n       {short(it.get('summary'), 200)}" if it.get("summary") else ""))
    elif c == "search":
        for t in r.get("tasks", []):
            print(f"tarea #{t['id']:<5} [{t['status']:<10}] {t['space_id']:<12} {short(t['title'], 90)}")
        for th in r.get("threads", []):
            print(f"hilo  {th['id']:<6} [{th['status']:<10}] {th['space_id']:<12} {short(th['title'], 90)}")
        for m in r.get("messages", []):
            where = f"#{m['task_id']}" if m.get("task_id") else f"hilo {m['thread_id']}"
            print(f"{when(m['created'])} {m['space_id']:<12} {where:<10} {m['author']:<20} {short(m['snippet'], 200)}")
        if not (r.get("tasks") or r.get("threads") or r.get("messages")):
            print("Nada.")
    elif c == "digest":
        print_digest(r)
    elif c == "inbox":
        print_inbox(r)
    elif c == "sync":
        print_sync(r)
    elif c == "handover":
        print_handover(r)
        print_quota_warnings(r)
    elif c in ("task", "thread"):
        print_conversation(r)
    elif c == "debate":
        print_debate(r)
    elif c == "tasks":
        print_board({"tasks": r.get("tasks", [])})
    elif c == "orphans":
        if a.sweep:
            print(f"ok · {len(r.get('new') or [])} huérfana(s) nueva(s) anunciada(s)")
        print_orphans(r.get("findings") or [])
        if not r.get("findings"):
            print("Sin trabajo huérfano.")
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
        if c == "escalate" and r.get("asked"):      # by rank, a question may go to another agent before the person
            print("  la pregunta ha ido a: " + ", ".join(r["asked"]) + ("" if r.get("reached_person") else " (por rango; llega a la persona si la suben)"))
        if r.get("stays_resolved"):
            print("  el hilo sigue resuelto: lo cerró alguien de más rango")
        if r.get("locks"):
            print("  bloqueos: " + ", ".join(r["locks"]))
        print_quota_warnings(r)
        print_dep_notes(r)
        if c in ("deps", "add") and isinstance(r.get("task"), dict) and r["task"].get("blocked_by"):
            t = r["task"]
            print("  espera a: " + ", ".join(f"#{b['id']} [{b['status']}]" for b in t["blocked_by"]) + deps_badge(t))
        if isinstance(r.get("claim"), dict):
            cl = r["claim"]
            print("  reclamada" if cl.get("ok") else "  NO reclamada: " + json.dumps(cl.get("conflicts"), ensure_ascii=False))
        if isinstance(r.get("inbox"), dict):
            i = r["inbox"]
            print(f"  buzón: {i.get('messages', 0)} mensajes ({i.get('for_you', 0)} para ti), {i.get('reviews', 0)} revisiones, {i.get('changes', 0)} cambios")
        if isinstance(r.get("unseen"), dict):
            u = r["unseen"]
            ids = ", ".join(f"#{m.get('id')} {m.get('author')} {m.get('kind')}" for m in (u.get("messages") or [])[:6])
            print(f"  AVISO: hecho sin haber leído {u.get('count')} mensaje(s) de otros en el hilo de la tarea ({ids}). "
                  "Léelos con: task <id> o thread <id>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
