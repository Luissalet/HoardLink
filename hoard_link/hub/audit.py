"""The family audit: does every app speak the same contract, and where not.

One report, one line per app, so drift is visible instead of discovered
the day a rule fails: which agent contract the app answers (the shared
``/api/agent/tools`` + ``/api/agent/call``, or the older one-route-per-
tool shape), whether its bearer token file exists, whether its health
answer carries the family block (``hoard_link`` version and ``events``
support — what the vendored library adds), which version of the library
it vendors and whether that lags the hub's, whether its data folder is
where backups expect it and is git-ignored, and how big it is.

Read-only. Everything is best-effort: an app that is down still gets a
line, with what can be told from disk.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from . import contract, procs
from .registry import App

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
_SKIP_DIRS = {"node_modules", "venv", ".venv", "dist", "static", ".git", "__pycache__", "data", "frontend", "client"}


def library_version() -> str:
    try:
        from hoard_link import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "?"


def vendored_version(folder: str, max_depth: int = 3) -> Optional[dict[str, str]]:
    """The ``hoard_link/__init__.py`` an app vendors (path + version), if any."""
    base_depth = folder.rstrip("/\\").count(os.sep)
    for root, dirs, names in os.walk(folder):
        depth = root.count(os.sep) - base_depth
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")] if depth < max_depth else []
        if os.path.basename(root) == "hoard_link" and "__init__.py" in names and "hub" not in dirs:
            try:
                text = open(os.path.join(root, "__init__.py"), "r", encoding="utf-8").read()
            except OSError:
                continue
            m = _VERSION_RE.search(text)
            return {"path": root, "version": m.group(1) if m else "?"}
    return None


def _gitignored(folder: str, rel: str) -> Optional[bool]:
    path = os.path.join(folder, ".gitignore")
    try:
        lines = [l.strip() for l in open(path, "r", encoding="utf-8-sig").read().splitlines()]
    except OSError:
        return None
    rel = rel.strip("/\\").replace("\\", "/")
    for l in lines:
        if not l or l.startswith("#"):
            continue
        pat = l.strip("/").rstrip("/")
        if pat in (rel, rel + "/", rel.split("/")[0], "**/" + rel):
            return True
    return False


def audit_app(app: App, hub_url: str = "", *, probe: bool = True) -> dict[str, Any]:
    line: dict[str, Any] = {"id": app.id, "name": app.name, "url": app.url, "folder": app.folder}
    line["token_file"] = app.token_file
    line["token_present"] = bool(contract.read_token(app.token_file))
    line["data_dir"] = app.data_dir
    line["data_dir_exists"] = os.path.isdir(app.data_dir)
    rel_data = os.path.relpath(app.data_dir, app.folder) if app.data_dir.startswith(app.folder) else None
    line["data_gitignored"] = _gitignored(app.folder, rel_data) if rel_data else None
    line["data_size"] = contract.data_dir_size(app.data_dir) if line["data_dir_exists"] else None
    vend = vendored_version(app.folder)
    line["vendored_hoard_link"] = vend
    lib = library_version()
    line["vendored_lags"] = bool(vend and vend["version"] != "?" and _vtuple(vend["version"]) < _vtuple(lib))
    line["stack"] = str(app.family.get("stack") or ("node" if os.path.isfile(os.path.join(app.folder, "server", "index.js")) or os.path.isfile(os.path.join(app.folder, "server", "app.js")) else "python"))
    line["agent_contract"] = app.agent_contract
    if not probe:
        return line
    h = procs.health(app)
    line["state"] = "running" if h.state == "healthy" else h.state
    body = h.body if isinstance(h.body, dict) else {}
    fam = body.get("hoard_link") if isinstance(body.get("hoard_link"), dict) else None
    line["health_family_block"] = fam
    line["events"] = bool(fam and fam.get("events"))
    line["contract"] = "unknown" if app.agent_contract else "none"
    line["tools"] = None
    if h.state == "healthy" and app.agent_contract:
        cat = contract.app_tools(app, timeout=4.0)
        line["contract"] = cat.get("contract", "unknown")
        if cat.get("ok"):
            line["tools"] = [t.get("name") for t in cat.get("tools") or [] if isinstance(t, dict)]
            line["tools_long_first_line"] = [t.get("name") for t in cat.get("tools") or []
                                            if isinstance(t, dict) and len(str(t.get("description") or "").split("\n")[0]) > 110]
    return line


def _vtuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    return tuple(out)


def audit(apps: list[App], hub_url: str = "", *, probe: bool = True) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=max(2, len(apps))) as pool:
        lines = list(pool.map(lambda a: audit_app(a, hub_url, probe=probe), apps))
    lib = library_version()
    summary = {
        "library_version": lib,
        "apps": len(lines),
        "running": sum(1 for l in lines if l.get("state") == "running"),
        "shared_contract": sorted(l["id"] for l in lines if l.get("contract") == "shared"),
        "per_tool_contract": sorted(l["id"] for l in lines if l.get("contract") == "per-tool"),
        "contract_unknown": sorted(l["id"] for l in lines if l.get("contract") == "unknown"),
        "no_contract": sorted(l["id"] for l in lines if not l.get("agent_contract", True)),
        "no_token": sorted(l["id"] for l in lines if not l["token_present"] and l.get("agent_contract", True)),
        "no_events": sorted(l["id"] for l in lines if l.get("state") == "running" and not l.get("events") and l.get("agent_contract", True)),
        "vendored_lagging": sorted(l["id"] for l in lines if l.get("vendored_lags")),
        "not_vendoring": sorted(l["id"] for l in lines if l["stack"] == "python" and not l.get("vendored_hoard_link") and l.get("agent_contract", True)),
        "data_not_gitignored": sorted(l["id"] for l in lines if l.get("data_gitignored") is False),
        "data_missing": sorted(l["id"] for l in lines if not l["data_dir_exists"]),
        "tools_long_first_line": {l["id"]: l["tools_long_first_line"] for l in lines if l.get("tools_long_first_line")},
        "data_bytes": sum((l.get("data_size") or {}).get("bytes", 0) for l in lines),
    }
    names: dict[str, list[str]] = {}
    for l in lines:
        for t in l.get("tools") or []:
            names.setdefault(t, []).append(l["id"])
    summary["tool_names_shared_by_apps"] = {t: a for t, a in names.items() if len(a) > 1}
    summary["recommendations"] = recommendations(summary)
    return {"ok": True, "summary": summary, "apps": lines}


def recommendations(s: dict[str, Any]) -> list[str]:
    out = []
    if s["per_tool_contract"]:
        out.append("per-tool contract (no /api/agent/call): " + ", ".join(s["per_tool_contract"]) +
                   " — call hoard_link.family.install_fastapi(app, id, data_dir, mcp_source=...) so rules and the proxy reach them with a token.")
    if s["no_token"]:
        out.append("no data/mcp-token: " + ", ".join(s["no_token"]) + " — the hub cannot call them on behalf of others.")
    if s["no_events"]:
        out.append("running but not emitting events: " + ", ".join(s["no_events"]) + " — vendor hoard_link ≥ 0.4, call family.configure() at startup and family.record_call() per agent call.")
    if s["vendored_lagging"]:
        out.append("vendored hoard_link older than the hub's: " + ", ".join(s["vendored_lagging"]) + " — run scripts/sync_vendored.py.")
    if s["not_vendoring"]:
        out.append("python apps without a vendored hoard_link: " + ", ".join(s["not_vendoring"]) + ".")
    if s["data_not_gitignored"]:
        out.append("data folder not in .gitignore: " + ", ".join(s["data_not_gitignored"]) + " — tokens and databases could be committed.")
    if s["tools_long_first_line"]:
        out.append("tool descriptions whose first line exceeds 110 characters (tool-RAG truncates): " +
                   ", ".join(f"{a} ({len(t)})" for a, t in s["tools_long_first_line"].items()))
    return out
