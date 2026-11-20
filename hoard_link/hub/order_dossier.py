"""A production brief and source receipts shared by jobs from any order origin."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat


MIGRATION = "ALTER TABLE agora_orders ADD COLUMN dossier TEXT NOT NULL DEFAULT '{}';"

DOSSIER_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "audience": {"type": "string", "maxLength": 2000},
        "constraints": {"type": "array", "maxItems": 60, "items": {"type": "string", "maxLength": 300}},
        "native": {"type": ["object", "null"], "required": ["app", "tool", "payload"], "additionalProperties": False,
                   "properties": {"app": {"type": "string", "maxLength": 120},
                                  "tool": {"type": "string", "maxLength": 120},
                                  "object": {"type": "string", "maxLength": 1000},
                                  "source_rev": {"type": "string", "maxLength": 200},
                                  "payload": {"type": "object"}}},
    },
}
MATERIAL_PROPERTIES = {
    "sha256": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
    "source_rev": {"type": "string", "maxLength": 200},
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def settings(value, previous, text, lines, error):
    """Update the common fields and preserve the exact native contract, when supplied."""
    if not isinstance(value, dict) or value.keys() - DOSSIER_INPUT["properties"].keys():
        raise error("dossier accepts audience, constraints and native context")
    changes = dict(previous)
    if "audience" in value:
        changes["audience"] = text(value["audience"], 2000, "dossier.audience")
    if "constraints" in value:
        changes["constraints"] = lines(value["constraints"], "dossier.constraints")
    if "native" in value:
        native = value["native"]
        if native is None:
            changes.pop("native", None)
        else:
            if not isinstance(native, dict) or native.keys() - DOSSIER_INPUT["properties"]["native"]["properties"].keys():
                raise error("native context needs app, tool and payload, with optional object and source_rev")
            payload = native.get("payload")
            if not isinstance(payload, dict):
                raise error("native payload is the object returned by the source tool")
            try:
                sha = digest(payload)
            except (ValueError, TypeError, RecursionError, UnicodeError):
                raise error("native payload must be valid JSON") from None
            identity = {k: text(native.get(k), limit, f"dossier.native.{k}", required=k in ("app", "tool"))
                        for k, limit in (("app", 120), ("tool", 120), ("object", 1000), ("source_rev", 200))}
            changes["native"] = {**{k: v for k, v in identity.items() if v}, "payload": payload, "sha256": sha}
    return changes


def _file_digest(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _link(path):
    entry = path.lstat()
    return (stat.S_ISLNK(entry.st_mode)
            or getattr(entry, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None))


def _directory_digest(path):
    entries = []

    def unavailable(error):
        raise error

    for base, dirs, names in os.walk(path, onerror=unavailable):
        folder = Path(base)
        dirs[:] = sorted(name for name in dirs if not _link(folder / name))
        for name in names:
            item = folder / name
            if not _link(item) and stat.S_ISREG(item.stat().st_mode):
                entries.append((item.relative_to(path).as_posix(), _file_digest(item)))
    return digest(sorted(entries)), len(entries)


def materials(refs, folder):
    """Fingerprint local inputs at registration. Links and app objects can supply their own receipt."""
    output = []
    for ref in refs:
        item = dict(ref)
        supplied = item.pop("sha256", "")
        if supplied:
            item["declared_sha256"] = supplied
        if ref.get("path"):
            try:
                path = Path(os.path.abspath(Path(folder or ".") / ref["path"]))
                item["resolved_path"] = str(path)
                if _link(path):
                    item["state"] = "unavailable"
                elif path.is_file():
                    item.update(kind="file", state="fingerprinted", sha256=_file_digest(path))
                elif path.is_dir():
                    sha, count = _directory_digest(path)
                    item.update(kind="folder", state="fingerprinted", sha256=sha, files=count)
                else:
                    item["state"] = "unavailable"
            except (OSError, ValueError):
                item["state"] = "unavailable"
            if supplied and item.get("sha256") and supplied != item["sha256"]:
                item["state"] = "changed"
        elif supplied:
            item.update(state="declared", sha256=supplied)
        else:
            item["state"] = "unrecorded"
        output.append(item)
    return output


def ref_receipt(ref, error):
    sha = ref.get("sha256")
    if sha:
        if not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
            raise error("refs.sha256 must be a 64-character SHA-256")
        ref["sha256"] = sha.lower()
    return ref


def dossier(order, saved):
    """Project the request and immutable source receipts into the worker's brief."""
    return {
        "schema": "hoard.production-dossier.v1",
        "request": order["words"] or order["title"], "title": order["title"], "audience": saved.get("audience", ""),
        "deliverables": order["deliverables"], "constraints": saved.get("constraints", []), "keep": order["keep"],
        "materials": saved.get("materials", [{**ref, "state": "unrecorded"} for ref in order["refs"]]),
        **({"native": saved["native"]} if "native" in saved else {}),
    }
