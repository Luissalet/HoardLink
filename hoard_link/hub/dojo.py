"""Forward the Dojo's agent tools through the active installation link."""

import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .. import where
from .config import AGENT_ID_RE

OPERATIONS = {
    "list": ("GET", ""),
    "attempt": ("GET", "/attempts/{attempt_id}"),
    "compare": ("POST", "/compare"),
    "exam_create": ("POST", "/exams"),
    "exam_key": ("GET", "/exams/{exam_id}/key"),
    "exam_import": ("POST", "/benchmarks"),
    "launch": ("POST", "/attempts"),
    "stop": ("POST", "/attempts/{attempt_id}/stop"),
    "mark": ("POST", "/marks"),
    "attempt_import": ("POST", "/history/attempts"),
    "mark_import": ("POST", "/history/marks"),
    "round_import": ("POST", "/history/rounds"),
    "teach": ("POST", "/lessons"),
    "grade_propose": ("POST", "/proposals"),
    "undo": ("POST", "/undo"),
}

TOOL = {
    "name": "hub_dojo",
    "description": "Use Dojo as a teacher: exams and keys, launch, compare attempts, mark, lessons, round import and undo. Keywords: examen, nota.",
    "inputSchema": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        "arguments": {"type": "object", "description": "Arguments of the matching dojo_<operation> tool."},
    }, "required": ["operation"], "additionalProperties": False},
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(hub):
    placement = hub.placement.state()
    if (placement.get("job") or {}).get("running"):
        return ""
    if placement.get("location") == "sparks":
        return (placement.get("urls") or {}).get("faustus", "").rstrip("/")
    return where.reachable(hub.config.faustus_urls)


def call(hub, args, agent):
    if (not AGENT_ID_RE.fullmatch(agent) or agent in ("hub", "ui", "system", "studio", hub.config.person_profile["id"])):
        return {"ok": False, "error": "Set your own agent identity / Indica tu propio id de agente"}
    operation = str(args.get("operation") or "")
    arguments = args.get("arguments", {})
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        return {"ok": False, "error": "Invalid Dojo operation or arguments"}
    method, path = OPERATIONS[operation]
    field = path.partition("{")[2].partition("}")[0]
    if field:
        ident = arguments.get(field)
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
            return {"ok": False, "error": f"An {field.removesuffix('_id')} id is required"}
        path = path.format(**{field: ident})
        arguments = {name: value for name, value in arguments.items() if name != field}
    base = _url(hub)
    address = urlsplit(base)
    if address.scheme != "https" and (address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost", "::1")):
        return {"ok": False, "error": "The active Dojo address is unavailable or needs HTTPS"}
    request = Request(base + "/api/dojo" + path, method=method, headers={
        "Authorization": "Bearer " + hub.token, "X-Agent-Id": agent,
        "Accept": "application/json", "Content-Type": "application/json",
    }, data=json.dumps(arguments).encode() if method == "POST" and arguments else None)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=45) as response:
            result = json.load(response)
        if operation == "list" and isinstance(result, dict) and result.get("ok", True) is True:
            exams = result.get("exams")
            if isinstance(exams, list):
                hub.dojo_list_receipt = (base, time.time(), len(exams))
        return result
    except HTTPError as exc:
        with exc:
            try:
                detail = json.load(exc).get("detail", exc.reason)
            except (ValueError, AttributeError):
                detail = exc.reason
        return {"ok": False, "status": exc.code, "error": detail}
    except (OSError, ValueError):
        return {"ok": False, "error": "The active Dojo did not return a response"}
