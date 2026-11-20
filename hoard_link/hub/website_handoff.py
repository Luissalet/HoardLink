"""Hand a pinned order piece to the website publication station."""

import hashlib
from pathlib import Path

from . import agora_orders


NAME = "hub_website_publish_piece"
WEB_KINDS = {"web", "web-page", "web-package"}
STRING = {"type": "string", "minLength": 1}
ID = {"type": "integer", "minimum": 1}
TOOL = {
    "name": NAME,
    "description": "Publish an order's web revision. Publica una revisión web de una orden. Returns Mercator undo.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "space_id": STRING,
            "piece_id": ID,
            "revision": ID,
            "package_piece_id": {**ID, "description": "Optional ZIP piece made from the specified web revision."},
            "slug": STRING,
            "title": STRING,
            "start_with_hub": {"type": "boolean", "default": True},
        },
        "required": ["piece_id", "revision", "slug"],
        "additionalProperties": False,
    },
}


def _identity(row):
    return {key: row[key] for key in ("id", "revision", "sha256")}


def _folder_digest(folder):
    # Same package fingerprint Mercator computes on the copied tree.
    digest = hashlib.sha256()
    for member in sorted(path for path in folder.rglob("*") if path.is_file()):
        name = member.relative_to(folder).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        with member.open("rb") as handle:
            digest.update(hashlib.file_digest(handle, "sha256").digest())
    return digest.hexdigest()


def publish_piece(hub, args):
    from .agora import AgoraError, SpaceError

    try:
        agora = hub.facet("agora").agora.for_space(args.get("space_id"))
        web = agora_orders._piece_row(agora, args.get("piece_id"))
        if web["kind"] not in WEB_KINDS or web["revision"] != args.get("revision"):
            return {"ok": False, "error": "Choose an exact registered web revision.", "code": "web_revision"}
        package = web
        if args.get("package_piece_id") is not None:
            package = agora_orders._piece_row(agora, args["package_piece_id"])
            material = any(item["piece"] == web["id"] and item["role"] == "material"
                           for item in agora_orders._parents(package["derives_from"]))
            if package["order_id"] != web["order_id"] or not material or Path(package["path"]).suffix.lower() != ".zip":
                return {"ok": False, "error": "Choose a ZIP made from this web revision in the same order.",
                        "code": "web_package"}
        if any(row["status"] in ("cancelled", "stale") for row in (web, package)):
            return {"ok": False, "error": "Choose a current web piece and package.", "code": "web_stale"}
        path = Path(agora_orders.piece_file(agora, package["id"]))
        sha = package["sha256"]
        if not sha or agora_orders._print(str(path)) != sha:
            return {"ok": False, "error": "The package bytes differ from the registered revision.", "code": "piece_changed"}
        if path.suffix.lower() in (".htm", ".html"):
            if str(path) != package.get("stored"):
                return {"ok": False, "error": "Register a ZIP to publish a web without a retained folder.",
                        "code": "web_package"}
            path = path.parent
            sha = _folder_digest(path)
        elif path.suffix.lower() != ".zip":
            return {"ok": False, "error": "The web revision needs an HTML file or ZIP.", "code": "web_package"}
    except (AgoraError, SpaceError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "code": "web_source_unavailable"}

    provenance = {"space_id": agora.space_id, "order_id": web["order_id"],
                  "web": _identity(web), "package": _identity(package)}
    return hub.call_app("mercator", "website_publish", {
        "slug": args["slug"], "title": args.get("title") or web["title"],
        "source": str(path), "source_sha256": sha, "provenance": provenance,
        "start_with_hub": args.get("start_with_hub", True),
    }, caller="website-handoff", timeout=60)
