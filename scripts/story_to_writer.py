"""Export one Scheherazade session and import it into Writer Desktop.

Both apps must be running. Set WH_BRIDGE_TOKEN in this process's environment;
the token is sent only to Writer's loopback bridge and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

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
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"El servicio devolvió HTTP {error.code}") from error
    if not isinstance(result, dict):
        raise RuntimeError("Respuesta no reconocida del servicio")
    return result


def export_chapter(base_url: str, world: str, session: str) -> str:
    parts = []
    offset = 0
    while True:
        page = post_json(f"{base_url}/api/agent/session_export", {
            "world": world, "session": session, "format": "md", "offset": offset, "max_chars": 20000,
        })
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
                   session: str, title: str, content: str, refresh: bool) -> dict:
    body = {"tool": "wh_import_story_session", "args": {
        "projectId": project, "worldId": world, "sessionId": session, "title": title,
        "content": content, "refresh": refresh,
    }}
    if len(json.dumps(body).encode("utf-8")) > 4_000_000:
        raise RuntimeError("El capítulo supera el límite del puente de Writer")
    result = post_json(f"{writer_url}/api/call", body, token)
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
    parser.add_argument("--scheherazade-url", default="http://127.0.0.1:8816")
    parser.add_argument("--writer-url", default="http://127.0.0.1:8766")
    args = parser.parse_args(argv)
    try:
        scheherazade = loopback_url(args.scheherazade_url)
        writer = loopback_url(args.writer_url)
        token = os.environ.get("WH_BRIDGE_TOKEN", "")
        if not token:
            raise ValueError("Falta WH_BRIDGE_TOKEN para Writer Desktop")
        chapter = export_chapter(scheherazade, args.world, args.session)
        title = args.title or next((line[2:].strip() for line in chapter.splitlines() if line.startswith("# ")), args.session)
        result = import_chapter(writer, token, project=args.project, world=args.world,
                                session=args.session, title=title, content=chapter, refresh=args.refresh)
        print(json.dumps({"state": result.get("state"), "id": result.get("id"), "title": result.get("title")}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, urllib.error.URLError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
