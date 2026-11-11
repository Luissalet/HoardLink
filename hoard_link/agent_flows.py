"""Read written agent flows from the skills an app actually carries.

This is a catalogue of procedures, never a claim that a flow has run.
Sections remain verbatim Markdown, including nested steps and references.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote

from . import skills


def written_flow(row: dict[str, Any], text: str) -> dict[str, Any]:
    meta, body = skills.parse(text)
    sections = {}
    for match in re.finditer(r"^##\s+([^\n]+)\n(.*?)(?=^##\s|\Z)", body, re.M | re.S):
        sections[match[1].strip().casefold()] = match[2].strip()
    fields = {"trigger": sections.get("when to use", ""), "steps": sections.get("procedure", ""),
              "outputs": sections.get("outputs", "") or sections.get("verification", ""),
              "verification": sections.get("verification", "")}
    missing = [key for key in ("trigger", "steps", "verification") if not fields[key]]
    slug = row["slug"]
    return {"id": slug, "skill": slug, "name": row.get("name") or slug,
            "description": row.get("description") or meta.get("description", ""),
            "version": row.get("version") or meta.get("version", ""), "why": row.get("why", ""),
            **fields, "outputs_source": "Outputs" if sections.get("outputs") else "Verification",
            "status": "incomplete" if missing else "written", "missing": missing,
            "source": "SKILL.md", "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def catalogue(shelf: skills.Shelf, *, base: str, query: str = "") -> list[dict[str, Any]]:
    rows = shelf.list()
    shelf.max_age_s = float("inf")  # one refresh per catalogue, not once per skill
    result = []
    words = query.casefold().split()
    for row in rows:
        try:
            flow = written_flow(row, shelf.read(row["slug"])["text"])
        except (KeyError, OSError, UnicodeError):
            flow = {"id": row["slug"], "skill": row["slug"], "name": row.get("name") or row["slug"],
                    "status": "unavailable", "missing": ["source"], "trigger": "", "steps": "", "outputs": "", "verification": ""}
        flow["skill_url"] = base + "/read/" + quote(row["slug"], safe="")
        if all(word in " ".join(str(v) for v in flow.values()).casefold() for word in words):
            result.append(flow)
    return result
