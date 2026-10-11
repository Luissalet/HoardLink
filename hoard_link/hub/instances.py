"""Instances: other running copies of an app whose tokens count as that app.

The hub knows an app's bearer token from the ``mcp-token`` in the app's
registered data folder. A second copy of the same app started on another data
folder (a QA instance, a demo, a copy on a branch) writes its own token there,
so every family call it makes - "transcribe this", "save that event" - came
back ``the hub refused this app's token``. Listing the copy here fixes that:
its token file is read like the registered one and a caller with that token
is the app.

The list lives in ``<hub data>/instance-tokens.json`` and is managed through

* ``GET /api/instances`` - the list, with whether each token file has a token;
* ``POST /api/instances/add`` ``{app, data_dir | token_file, label?}``;
* ``POST /api/instances/remove`` ``{app?, data_dir? | token_file?}``;
* the agent tools ``hub_instance_list`` / ``hub_instance_add`` / ``hub_instance_remove``.

Adding or removing takes the hub page, the hub's own token or an agent call;
any family caller may read the list.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

from .contract import read_token
from .facets import Facet, Request

FILE_NAME = "instance-tokens.json"
TOKEN_NAME = "mcp-token"


class InstancesFacet(Facet):
    id = "instances"

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.Lock()

    # -- storage ------------------------------------------------------------
    @property
    def path(self) -> str:
        return os.path.join(self.hub.config.data_dir, FILE_NAME)

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        rows = raw.get("instances") if isinstance(raw, dict) else raw
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("app") and r.get("token_file")]

    def _save(self, rows: list[dict[str, Any]]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"instances": rows}, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- operations ---------------------------------------------------------
    def list(self) -> dict[str, Any]:
        rows = self._load()
        out = []
        for r in rows:
            row = dict(r)
            row["token_present"] = bool(read_token(r["token_file"]))
            row["registered"] = self.hub.get(r["app"]) is not None
            out.append(row)
        return {"ok": True, "instances": out}

    @staticmethod
    def _token_file(token_file: Any, data_dir: Any) -> str:
        if token_file:
            return os.path.abspath(str(token_file))
        if data_dir:
            return os.path.join(os.path.abspath(str(data_dir)), TOKEN_NAME)
        return ""

    def add(self, app: Any, *, token_file: Any = "", data_dir: Any = "", label: Any = "") -> dict[str, Any]:
        app_id = str(app or "").strip()
        if not app_id:
            return {"ok": False, "error": "app is required"}
        if self.hub.get(app_id) is None:
            return {"ok": False, "error": f"unknown app: {app_id}", "status": 404}
        path = self._token_file(token_file, data_dir)
        if not path:
            return {"ok": False, "error": "data_dir or token_file is required"}
        registered = self.hub.get(app_id).token_file
        if registered and os.path.normcase(os.path.abspath(registered)) == os.path.normcase(path):
            return {"ok": True, "instance": {"app": app_id, "token_file": path}, "note": "this is the app's own token file"}
        with self._lock:
            rows = [r for r in self._load()
                    if not (r["app"] == app_id and os.path.normcase(r["token_file"]) == os.path.normcase(path))]
            row = {"app": app_id, "token_file": path, "label": str(label or "").strip(), "added": time.time()}
            rows.append(row)
            self._save(rows)
        out = dict(row)
        out["token_present"] = bool(read_token(path))
        return {"ok": True, "instance": out}

    def remove(self, *, app: Any = "", token_file: Any = "", data_dir: Any = "") -> dict[str, Any]:
        app_id = str(app or "").strip()
        path = self._token_file(token_file, data_dir)
        if not app_id and not path:
            return {"ok": False, "error": "app, data_dir or token_file is required"}
        with self._lock:
            rows = self._load()
            keep = [r for r in rows if not ((not app_id or r["app"] == app_id) and
                                            (not path or os.path.normcase(r["token_file"]) == os.path.normcase(path)))]
            removed = len(rows) - len(keep)
            if removed:
                self._save(keep)
        return {"ok": True, "removed": removed}

    def owner(self, token: str) -> Optional[str]:
        """The app whose listed instance holds ``token``, else None."""
        if not token:
            return None
        for r in self._load():
            tok = read_token(r["token_file"])
            if tok and tok == token and self.hub.get(r["app"]) is not None:
                return str(r["app"])
        return None

    # -- HTTP ---------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path != "/api/instances":
            return None
        if req.caller() is None and not req.agent():
            return {"ok": False, "status": 401, "error": "a family bearer token (or the hub page) is required"}
        return self.list()

    def post(self, req: Request) -> Optional[Any]:
        if req.path not in ("/api/instances/add", "/api/instances/remove"):
            return None
        if req.caller() not in ("ui", "hub") and not req.agent():
            return {"ok": False, "status": 403, "error": "the hub page or the hub's token"}
        b = req.body or {}
        if req.path.endswith("/add"):
            return self.add(b.get("app"), token_file=b.get("token_file"), data_dir=b.get("data_dir"),
                            label=b.get("label"))
        return self.remove(app=b.get("app"), token_file=b.get("token_file"), data_dir=b.get("data_dir"))

    # -- agent tools ----------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        where = {"app": {"type": "string", "description": "App id as the hub lists it (e.g. prospero)."},
                 "data_dir": {"type": "string", "description": "The instance's data folder (its mcp-token is used)."},
                 "token_file": {"type": "string", "description": "Or the token file itself."}}
        return [
            {"name": "hub_instance_list",
             "description": "Other running copies of apps (QA, demo) whose tokens the hub accepts as the app / "
                            "instancias de prueba cuyos tokens cuentan como la app.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True}},
            {"name": "hub_instance_add",
             "description": "Let a copy of an app started on another data folder talk to the family as that app / "
                            "aceptar el token de una instancia aislada de una app.",
             "inputSchema": {"type": "object", "properties": {**where, "label": {"type": "string"}},
                             "required": ["app"], "additionalProperties": False}},
            {"name": "hub_instance_remove",
             "description": "Stop accepting an instance's token / dejar de aceptar el token de una instancia.",
             "inputSchema": {"type": "object", "properties": where, "additionalProperties": False}},
        ]

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "hub_instance_list": lambda a: self.list(),
            "hub_instance_add": lambda a: self.add(a.get("app"), token_file=a.get("token_file"),
                                                   data_dir=a.get("data_dir"), label=a.get("label")),
            "hub_instance_remove": lambda a: self.remove(app=a.get("app"), token_file=a.get("token_file"),
                                                         data_dir=a.get("data_dir")),
        }