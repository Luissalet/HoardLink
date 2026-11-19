"""Draft mode of each station: how it makes a cheap version of what it delivers, how long that takes, and what the
draft leaves for the expensive pass to anchor to.

A station declares it in its own manifest, inside each production handoff (``x-family.production.handoffs[].draft``)::

    "draft": {"how": {"es": "...", "en": "..."}, "seconds": 70, "gpu": true,
              "settings": {"template": "qwen21_edit", "steps": 8, "width": 512, "height": 768},
              "produces": "draft-image", "anchors": ["composition"], "measured": "cartographer, 19-11"}

``hub_station_catalog`` already passes that block through. Until a Hoard declares its own, the hub carries a starting
declaration in ``station_drafts.json`` (``declared_in: "hub"``); a manifest's block always wins (``"manifest"``).
``hub_station_drafts`` lists every handoff with its draft mode and, given ``from_type`` and ``to_type``, the chains with
the sum of their draft times and the steps that have no draft mode yet.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .facets import Facet

DATA = os.path.join(os.path.dirname(__file__), "station_drafts.json")


def _text(value: Any) -> Optional[dict[str, str]]:
    if isinstance(value, str) and value.strip():
        return {"es": value.strip(), "en": value.strip()}
    if isinstance(value, dict) and all(isinstance(value.get(k), str) and value[k].strip() for k in ("es", "en")):
        return {"es": value["es"].strip(), "en": value["en"].strip()}
    return None


def normalize(raw: Any) -> Optional[dict[str, Any]]:
    """A valid draft declaration, or None. It needs how (text or {es, en}) and seconds (> 0)."""
    if not isinstance(raw, dict):
        return None
    how = _text(raw.get("how"))
    seconds = raw.get("seconds")
    if not how or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    out: dict[str, Any] = {"how": how, "seconds": float(seconds), "gpu": bool(raw.get("gpu", False))}
    if isinstance(raw.get("settings"), dict):
        out["settings"] = raw["settings"]
    for key in ("produces", "tool", "measured"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = raw[key].strip()
    anchors = raw.get("anchors")
    if isinstance(anchors, list):
        out["anchors"] = [a for a in anchors if isinstance(a, str) and a.strip()]
    return out


def load_defaults(path: Optional[str] = None) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        with open(path or DATA, encoding="utf-8") as f:
            rows = json.load(f).get("drafts") or []
    except (OSError, ValueError):
        return {}
    out = {}
    for row in rows:
        draft = normalize(row)
        if draft and isinstance(row.get("app"), str) and isinstance(row.get("accepts"), str):
            out[(row["app"], row["accepts"])] = draft
    return out


def with_drafts(stations: list[dict[str, Any]], defaults: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Each handoff of the catalogue with its draft mode: the manifest's, else the hub's starting one, else None."""
    out = []
    for station in stations:
        handoffs = []
        for handoff in station["handoffs"]:
            own = normalize(handoff.get("draft"))
            if own:
                draft = {**own, "declared_in": "manifest"}
            elif (station["app"], handoff["accepts"]) in defaults:
                draft = {**defaults[(station["app"], handoff["accepts"])], "declared_in": "hub"}
            else:
                draft = None
            handoffs.append({**handoff, "draft": draft})
        out.append({**station, "handoffs": handoffs})
    return out


def chain_totals(chain: list[dict[str, Any]], stations: list[dict[str, Any]]) -> dict[str, Any]:
    """The draft seconds of a chain and the steps without a draft mode."""
    index = {(s["app"], h["accepts"]): h.get("draft") for s in stations for h in s["handoffs"]}
    steps, total, gpu, missing = [], 0.0, 0.0, []
    for step in chain:
        draft = index.get((step["app"], step["accepts"]))
        steps.append({**step, "draft": draft})
        if draft:
            total += draft["seconds"]
            gpu += draft["seconds"] if draft.get("gpu") else 0.0
        else:
            missing.append(f"{step['app']}: {step['accepts']} → {step['produces']}")
    return {"steps": steps, "draft_seconds": round(total, 1), "draft_gpu_seconds": round(gpu, 1), "without_draft": missing}


class StationDraftsFacet(Facet):
    id = "station_drafts"

    def drafts(self, args: dict[str, Any]) -> dict[str, Any]:
        production = self.hub.facet("production")
        if production is None:
            return {"ok": False, "error": "The production catalogue is not loaded"}
        query = {k: args[k] for k in ("app", "from_type", "to_type", "limit") if args.get(k)}
        catalog = production.catalog(query)
        if not catalog.get("ok"):
            return catalog
        stations = with_drafts(catalog["stations"], load_defaults())
        result: dict[str, Any] = {"ok": True, "stations": stations}
        if "chains" in catalog:
            result.update(from_type=catalog["from_type"], to_type=catalog["to_type"],
                          chains=[chain_totals(chain, stations) for chain in catalog["chains"]])
        declared = [h for s in stations for h in s["handoffs"] if h["draft"]]
        result["summary"] = {"handoffs": sum(len(s["handoffs"]) for s in stations), "with_draft": len(declared),
                             "in_manifest": sum(1 for h in declared if h["draft"]["declared_in"] == "manifest")}
        return result

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        string = {"type": "string", "minLength": 1}
        return [{
            "name": "hub_station_drafts",
            "description": "Each station's draft mode (how, seconds, GPU, anchors) and draft time of chains. "
                           "Modo borrador de cada puesto y cuánto tarda una cadena en borrador.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
                "app": string, "from_type": string, "to_type": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }]

    def handlers(self) -> dict[str, Any]:
        return {"hub_station_drafts": self.drafts}
