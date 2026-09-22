"""Best-effort probes for shared servers on loopback.

Every function here is a coroutine that takes an ``httpx.AsyncClient`` and
returns a plain dict on success or ``None`` on any failure (connection
refused, timeout, bad JSON, unexpected shape). None of them ever raises —
that is the whole point: probing five ports nobody may be listening on
must be cheap and quiet.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

PROBE_TIMEOUT_S = 1.0
LLAMACPP_PORTS = range(8080, 8091)
OLLAMA_PORT = 11434
OPENAI_COMPAT_PORT = 1234


async def _get_json(
    client: httpx.AsyncClient, url: str, headers: Optional[dict[str, str]] = None
) -> Optional[Any]:
    try:
        resp = await client.get(url, headers=headers, timeout=PROBE_TIMEOUT_S)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    json_body: Any,
    headers: Optional[dict[str, str]] = None,
) -> Optional[Any]:
    try:
        resp = await client.post(
            url, json=json_body, headers=headers, timeout=PROBE_TIMEOUT_S
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def probe_llamacpp_port(client: httpx.AsyncClient, port: int) -> Optional[dict]:
    base = f"http://127.0.0.1:{port}"
    props = await _get_json(client, f"{base}/props")
    if props is None:
        return None
    models = await _get_json(client, f"{base}/v1/models")
    slots = await _get_json(client, f"{base}/slots")
    return {
        "port": port,
        "url": base,
        "props": props,
        "models": models,
        "slots": slots if isinstance(slots, list) else None,
    }


async def probe_llamacpp(client: httpx.AsyncClient) -> list[dict]:
    """Probe every candidate llama.cpp port in parallel.

    Returns the list of ports that answered ``/props`` (usually 0 or 1).
    """
    results = await asyncio.gather(
        *(probe_llamacpp_port(client, p) for p in LLAMACPP_PORTS)
    )
    return [r for r in results if r is not None]


def llamacpp_busy(server: dict) -> bool:
    slots = server.get("slots")
    if not slots:
        return False
    return any(bool(s.get("is_processing")) for s in slots)


def llamacpp_supports_vision(server: dict) -> bool:
    props = server.get("props") or {}
    modalities = props.get("modalities") or []
    return "vision" in modalities


async def probe_ollama(client: httpx.AsyncClient) -> Optional[dict]:
    base = f"http://127.0.0.1:{OLLAMA_PORT}"
    ps = await _get_json(client, f"{base}/api/ps")
    if ps is None:
        return None
    resident_raw = ps.get("models") or []
    tags = await _get_json(client, f"{base}/api/tags")
    resident: list[dict] = []
    for m in resident_raw:
        name = m.get("model") or m.get("name")
        if not name:
            continue
        show = await _post_json(client, f"{base}/api/show", {"model": name})
        capabilities = (show or {}).get("capabilities") or []
        resident.append({"name": name, "capabilities": capabilities, "raw": m})
    return {
        "url": base,
        "resident": resident,
        "tags": (tags or {}).get("models") or [],
    }


async def probe_openai_compat(
    client: httpx.AsyncClient, port: int = OPENAI_COMPAT_PORT
) -> Optional[dict]:
    base = f"http://127.0.0.1:{port}"
    models = await _get_json(client, f"{base}/v1/models")
    if models is None:
        return None
    ids = [m.get("id") for m in (models.get("data") or []) if m.get("id")]
    if not ids:
        return None
    return {"url": base, "models": ids}


async def probe_comfy(
    client: httpx.AsyncClient, url: str = "http://127.0.0.1:8188"
) -> Optional[dict]:
    base = url.rstrip("/")
    stats = await _get_json(client, f"{base}/system_stats")
    if stats is None:
        return None
    checkpoints_info = await _get_json(
        client, f"{base}/object_info/CheckpointLoaderSimple"
    )
    checkpoints: list[str] = []
    if isinstance(checkpoints_info, dict):
        try:
            node = checkpoints_info.get("CheckpointLoaderSimple", {})
            checkpoints = node["input"]["required"]["ckpt_name"][0]
        except (KeyError, IndexError, TypeError):
            checkpoints = []
    return {"url": base, "system_stats": stats, "checkpoints": checkpoints}
