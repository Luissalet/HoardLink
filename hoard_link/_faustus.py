"""Talking to a (possibly absent) Faustus instance.

Faustus is the local AI workspace, reachable on loopback with an
optional bearer token. Every helper here returns ``(status, json)`` with
``status is None`` meaning "could not connect at all" (refused, timed out,
DNS) — that is distinct from a real 401/403, which the caller needs to
tell apart to produce an accurate reason string.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

TIMEOUT_S = 1.5


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[Optional[int], Any]:
    try:
        resp = await client.request(
            method, url, json=json_body, headers=headers, timeout=TIMEOUT_S
        )
    except httpx.HTTPError:
        return None, None
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


async def get(
    client: httpx.AsyncClient, url: str, headers: Optional[dict[str, str]] = None
) -> tuple[Optional[int], Any]:
    return await _request(client, "GET", url, headers=headers)


async def post(
    client: httpx.AsyncClient,
    url: str,
    json_body: Any,
    headers: Optional[dict[str, str]] = None,
) -> tuple[Optional[int], Any]:
    return await _request(client, "POST", url, json_body=json_body, headers=headers)


def auth_headers(token: Optional[str]) -> Optional[dict[str, str]]:
    return {"Authorization": f"Bearer {token}"} if token else None


async def find_reachable(
    client: httpx.AsyncClient, candidate_urls: tuple[str, ...]
) -> Optional[str]:
    """Return the first candidate whose /api/health reports healthy."""
    for url in candidate_urls:
        status, data = await get(client, f"{url}/api/health")
        if status == 200 and isinstance(data, dict) and data.get("status") == "healthy":
            return url
    return None


def find_model_item(models: list[dict], capability: str) -> Optional[dict]:
    for item in models:
        if item.get("model_type") == capability:
            return item
    return None


def api_for_backend(backend: Optional[str]) -> str:
    if backend == "ollama":
        return "ollama"
    return "openai"
