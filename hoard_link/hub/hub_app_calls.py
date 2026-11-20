"""The hub as a station other apps can read from: ``POST /api/apps/hoardhub/call``.

An app of the family calls the tools of another app through ``/api/apps/<id>/call`` with its own token. The hub's own
tools (the Agora's tasks and feed, order receipts and metrics, the station catalogue...) were only reachable with the
hub's bearer token on ``/api/agent/call``, so an app could not take them as a data source: Nightingale's
``data_ingest {kind: "hoard", app: "hoardhub", tool: "hub_agora_tasks"}`` answered «unknown app». This facet answers
that route, under the hub's manifest id and its short names, with the same reply as ``/api/agent/call``.
"""

from __future__ import annotations

from .facets import Facet, Reply

#: The ids an app may use for the hub: its manifest id and the short names in use.
HUB_IDS = ("hoardhub", "hoard-hub", "hub")


class HubAppCallsFacet(Facet):
    id = "hub_app_calls"

    def post(self, req):
        parts = req.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "apps"] or parts[3] != "call" or parts[2] not in HUB_IDS:
            return None
        if self.hub.get(parts[2]) is not None:
            return None          # an app really registered under that id keeps its route
        if req.caller() is None:
            return Reply({"ok": False, "error": "a family bearer token is required: the hub's data/mcp-token or any app's own"},
                         401)
        from . import agent_routes

        body = req.body if isinstance(req.body, dict) else {}
        name = str(body.get("tool") or body.get("name") or "")
        args = body.get("arguments") or body.get("args") or {}
        result, status = agent_routes.invoke(self.hub, name, args if isinstance(args, dict) else {})
        return Reply({**result, "app": "hoardhub"}, status)
