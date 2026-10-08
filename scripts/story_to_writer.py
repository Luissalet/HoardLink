"""Export one Scheherazade session and import it into Writer Desktop.

Both apps must be running. The Hub resolves app ports and authenticates each
owner. Direct legacy mode is explicit and needs both apps' own tokens.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hoard_link._hubclient import fetch_detailed, hub_url

MAX_CHAPTER = 3_000_000  # Writer's HTTP bridge accepts at most 4 MB per call.


def loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("Solo se admiten servidores locales")
    return value.rstrip("/")


def post_json(url: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    loopback_url(url)
    status, result, why = fetch_detailed(url, body, method="POST", timeout=35, headers=headers)
    if status is None or not 200 <= status < 300:
        raise RuntimeError(f"El servicio devolvió HTTP {status}" if status else f"El servicio no responde: {why}")
    if not isinstance(result, dict):
        raise RuntimeError("Respuesta no reconocida del servicio")
    return result


def export_chapter(base_url: str, world: str, session: str, *, token: str = "", call=None) -> str:
    parts = []
    offset = 0
    while True:
        arguments = {
            "world": world, "session": session, "format": "md", "offset": offset, "max_chars": 20000,
        }
        if call:
            response = call("scheherazade", "session_export", arguments)
            if not response.get("ok"):
                raise RuntimeError("El Hub no pudo exportar el capítulo")
            page = response.get("result") or {}
        else:
            page = post_json(f"{base_url}/api/agent/session_export", arguments, token)
        chunk = page.get("text")
        if page.get("kind") != "chapter" or page.get("offset") != offset or not isinstance(chunk, str):
            raise RuntimeError("La exportación de Scheherazade no es un capítulo continuo")
        parts.append(chunk)
        if sum(map(len, parts)) > MAX_CHAPTER:
            raise RuntimeError("El capítulo supera el tamaño que admite Writer")
        if not page.get("truncated"):
            break
        next_offset = page.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset or next_offset != offset + len(chunk):
            raise RuntimeError("Paginación incompleta del capítulo")
        offset = next_offset
    text = "".join(parts)
    if not text or len(text) != page.get("total_chars"):
        raise RuntimeError("El capítulo exportado está incompleto")
    return text


def import_chapter(writer_url: str, token: str, *, project: str, world: str,
                   session: str, title: str, content: str, refresh: bool, call=None) -> dict:
    body = {"tool": "wh_import_story_session", "args": {
        "projectId": project, "worldId": world, "sessionId": session, "title": title,
        "content": content, "refresh": refresh,
    }}
    if len(json.dumps(body).encode("utf-8")) > 4_000_000:
        raise RuntimeError("El capítulo supera el límite del puente de Writer")
    result = (call("writer", "wh_import_story_session", body["args"]) if call else
              post_json(f"{writer_url}/api/call", body, token))
    if not result.get("ok"):
        raise RuntimeError(f"Writer no importó el capítulo: {result.get('code') or 'error'}")
    return result["result"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pasar un capítulo de Scheherazade a Writer Desktop")
    parser.add_argument("--world", required=True, help="ID del mundo de Scheherazade")
    parser.add_argument("--session", required=True, help="ID de la sesión")
    parser.add_argument("--project", required=True, help="ID del proyecto de Writer")
    parser.add_argument("--title", help="Título en Writer; por defecto se toma del capítulo")
    parser.add_argument("--refresh", action="store_true", help="Actualizar si el capítulo local sigue intacto")
    parser.add_argument("--hub-url", help="Hub local; por defecto usa el descubrimiento compartido")
    parser.add_argument("--hub-token-file", type=Path, default=REPO / "data/mcp-token")
    parser.add_argument("--direct", action="store_true", help="Puentes antiguos sin Hub; exige SCHEHERAZADE_TOKEN y WH_BRIDGE_TOKEN")
    parser.add_argument("--scheherazade-url", default="http://127.0.0.1:8816")
    parser.add_argument("--writer-url", default="http://127.0.0.1:8766")
    args = parser.parse_args(argv)
    try:
        scheherazade = loopback_url(args.scheherazade_url)
        writer = loopback_url(args.writer_url)
        token = os.environ.get("WH_BRIDGE_TOKEN", "") if args.direct else ""
        source_token = os.environ.get("SCHEHERAZADE_TOKEN", "") if args.direct else ""
        call = None
        if args.direct:
            if not token or not source_token:
                raise ValueError("El modo directo necesita SCHEHERAZADE_TOKEN y WH_BRIDGE_TOKEN")
        else:
            base = loopback_url(hub_url(args.hub_url))
            # Verify identity before sending the Hub token to a discovered port.
            status, identity, _ = fetch_detailed(base + "/api/health", timeout=3)
            if status != 200 or not isinstance(identity, dict) or identity.get("service") != "hoard-hub":
                raise ValueError("No responde un Hoard Hub en la dirección configurada")
            try:
                hub_token = args.hub_token_file.read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise ValueError("No se pudo leer el token propio del Hub") from None
            if not hub_token:
                raise ValueError("El token propio del Hub está vacío")
            call = lambda app, tool, arguments: post_json(base + f"/api/apps/{app}/call",
                    {"tool": tool, "arguments": arguments, "timeout_s": 30}, hub_token)
        chapter = export_chapter(scheherazade, args.world, args.session, token=source_token, call=call)
        title = args.title or next((line[2:].strip() for line in chapter.splitlines() if line.startswith("# ")), args.session)
        result = import_chapter(writer, token, project=args.project, world=args.world,
                                session=args.session, title=title, content=chapter, refresh=args.refresh, call=call)
        print(json.dumps({"state": result.get("state"), "id": result.get("id"), "title": result.get("title")}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, urllib.error.URLError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
