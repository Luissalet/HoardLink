"""Terminal client for workers with no MCP connection."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import HubConfig


def _read_setting(path):
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self):
        config = HubConfig.load()
        self.url = (os.environ.get("HOARD_HUB_URL") or
                    _read_setting(Path(config.data_dir) / "url") or config.url).rstrip("/")
        self.token = _read_setting(Path(os.environ.get("HOARD_HUB_TOKEN_FILE") or config.token_file))
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, path, body=None, *, timeout=90):
        data = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": "hoard-hub-cli"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                try:
                    return exc.code, json.load(exc)
                except ValueError:
                    return exc.code, {"error": f"HTTP {exc.code}"}
        except (URLError, OSError):
            return None, {"ok": False, "error": "Hoard Hub did not answer / Hoard Hub no ha respondido"}
        except ValueError:
            return None, {"ok": False, "error": "Invalid Hub response / Respuesta del Hub no válida"}

    def tool(self, name, arguments, *, timeout=90):
        status, body = self.request("/api/agent/tools/" + quote(name, safe=""), arguments, timeout=timeout)
        if status == 404 and body == {"ok": False, "error": "not found"}:
            # Older Hubs only have the shared endpoint. Never retry after loss of observation.
            status, body = self.request("/api/agent/call", {"tool": name, "arguments": arguments}, timeout=timeout)
        return status, body


def _timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 1 <= seconds <= 900:
        raise argparse.ArgumentTypeError("use 1–900 seconds")
    return seconds


def _parser():
    parser = argparse.ArgumentParser(prog="hub.cmd", description="Hoard Hub commands for agents / Órdenes para agentes")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sub

    for name, help_text in [
        ("start", "Start an app"), ("stop", "Stop an app"), ("state", "Read app state"),
        ("tools", "List an app's tools")
    ]:
        command(name, help_text).add_argument("app")
    call = command("call", "Call an app tool")
    call.add_argument("app")
    call.add_argument("tool")
    call.add_argument("--args-file", type=Path, help="UTF-8 JSON object")
    call.add_argument("--timeout", type=_timeout, default=120, help="App call budget in seconds (default 120)")
    call.add_argument("--reason", help="Reason for an app write")
    hub = command("tool", "Call a Hub tool by its MCP name")
    hub.add_argument("tool")
    hub.add_argument("--args-file", type=Path, help="UTF-8 JSON object")
    hub.add_argument("--timeout", type=_timeout, default=90, help="Observation timeout in seconds (default 90)")
    command("stations", "List production stations")
    chain = command("chain", "Find a production chain")
    chain.add_argument("from_type")
    chain.add_argument("to_type")
    programs = command("programs", "List local programs")
    programs.add_argument("--refresh", action="store_true")
    return parser


def _run(client, args, arguments):
    command = args.command
    if command == "tool":
        return client.tool(args.tool, arguments, timeout=args.timeout)
    if command in ("stations", "chain"):
        filters = {"from_type": args.from_type, "to_type": args.to_type} if command == "chain" else {}
        return client.tool("hub_station_catalog", filters)
    if command == "programs":
        return client.tool("hub_programs", {"refresh": args.refresh})
    if command in ("start", "stop"):
        return client.tool(f"hub_{command}_app", {"app": args.app})
    app_path = "/api/apps/" + quote(args.app.strip().lower(), safe="")
    if command == "call":
        return client.request(app_path + "/call", {
            "tool": args.tool, "arguments": arguments, "timeout_s": args.timeout,
            "reason": args.reason,
        }, timeout=args.timeout + 15)
    status, body = client.request(app_path + ("/status" if command == "state" else "/tools"))
    if command == "state" and status == 404 and body == {"ok": False, "error": "not found"}:
        return client.request(app_path)
    return status, body


def _human(command, body):
    if not isinstance(body, dict):
        return json.dumps(body, ensure_ascii=False, indent=2)
    if command == "state" and "state" in body:
        return f"{body['name']} ({body['id']}): {body['state']}"
    if command == "tools" and isinstance(body.get("tools"), list):
        lines = [f"{row['name']}  " + str(row.get("description") or "").partition("\n")[0]
                 for row in body["tools"]]
        return "\n".join(lines) or "No tools / Sin herramientas"
    if command == "stations":
        lines = [f"{row['app']}: {edge['accepts']} → {', '.join(edge['produces'])}"
                 for row in body.get("stations", []) for edge in row["handoffs"]]
        return "\n".join(lines) or "No stations / Sin puestos"
    if command == "chain":
        chains = body.get("chains", [])
        return "\n".join(
            " → ".join([body["from_type"], *(f"{step['app']}: {step['produces']}" for step in steps)])
            for steps in chains
        ) or "No chain / Sin cadena"
    if command == "programs":
        return "\n".join(
            f"{row['name']}  {row['version'] or '—'}  {row['path'] or 'Not found / Sin detectar'}\n"
            f"  {row['headless']}"
            for row in body.get("programs", [])
        )
    return json.dumps(body, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    status = None
    try:
        file = getattr(args, "args_file", None)
        arguments = json.loads(file.read_text(encoding="utf-8-sig")) if file else {}
        if not isinstance(arguments, dict):
            raise ValueError("Arguments must be a JSON object / Los argumentos deben ser un objeto JSON")
        status, body = _run(Client(), args, arguments)
    except (OSError, ValueError) as exc:
        body = {"ok": False, "error": str(exc)}

    result = body.get("result", body) if isinstance(body, dict) else body
    success = status is not None and status < 400 and not any(
        isinstance(value, dict) and value.get("ok") is False for value in (body, result)
    )
    if isinstance(body, dict) and status is not None and status >= 400:
        body.setdefault("status", status)
    if args.json:
        print(json.dumps(body, ensure_ascii=False))
    elif success:
        print(_human(args.command, result if args.command != "call" else body))
    else:
        print(json.dumps(body, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
