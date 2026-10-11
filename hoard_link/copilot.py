"""Thin family transport for the Faustus copilot HTTP/stream contract."""
import json
import urllib.request


def multipart_context(body, content_type, origin):
    """Replace only a context field; keep uploaded bytes and MIME framing exact."""
    import re
    from email.message import Message
    from email.parser import BytesHeaderParser
    header = Message()
    header['Content-Type'] = content_type
    boundary = header.get_param('boundary')
    if header.get_content_type() != 'multipart/form-data' or not boundary:
        return body
    separators = list(re.finditer(rb'(?:^|\r\n)--' + re.escape(boundary.encode()) + rb'(?:--)?(?:\r\n|$)', body))
    replacements = []
    for start, end in zip(separators, separators[1:]):
        part = body[start.end():end.start()]
        split = part.find(b'\r\n\r\n')
        if split < 0:
            continue
        headers = BytesHeaderParser().parsebytes(part[:split] + b'\r\n\r\n')
        if headers.get_param('name', header='Content-Disposition') not in ('context', 'hoard_context'):
            continue
        context = json.loads(part[split + 4:].decode('utf-8'))
        if isinstance(context, dict):
            context['origin'] = {**context.get('origin', {}), **origin}
            replacements.append((start.end() + split + 4, end.start(), json.dumps(context, ensure_ascii=False).encode('utf-8')))
    for start, end, replacement in reversed(replacements):
        body = body[:start] + replacement + body[end:]
    return body


class CopilotClient:
    def __init__(self, hub_url, token):
        self.url = hub_url.rstrip("/") + "/api/link/copilot"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, path, *, method="GET", body=None, raw_body=None, content_type=None, headers=None):
        envelope = {"path": path, "method": method, "headers": headers or {}}
        if isinstance(body, (bytes, bytearray)) and raw_body is None:
            raw_body, body = body, None
        if body is not None:
            envelope["body"] = body
        if raw_body is not None:
            import base64
            envelope['raw_body'] = base64.b64encode(raw_body).decode('ascii')
            envelope['content_type'] = content_type or 'application/octet-stream'
        request = urllib.request.Request(self.url, data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, method="POST")
        return self.opener.open(request, timeout=900)

    def request(self, path, *, method="GET", body=None):
        with self.open(path, method=method, body=body) as response:
            return json.load(response)

    def events(self, session_id, *, cursor=None):
        from urllib.parse import quote
        path = "/api/chat/resume/" + quote(session_id, safe="")
        if cursor is not None:
            path += "?cursor=" + str(cursor)
        with self.open(path) as response:
            for line in response:
                yield line.decode("utf-8")


def make_copilot_router(app_id, data_dir, *, faustus_urls=None, token=None, app_name=None):
    """Mount a thin local relay; all panel/chat/agent behavior stays in Faustus.

    FastAPI is optional for the library; only hosting apps import this factory.
    """
    import base64
    import uuid
    from pathlib import Path
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask
    import httpx
    from .config import LinkConfig

    config = LinkConfig.load()
    urls = faustus_urls or config.faustus_urls
    token = token if token is not None else config.faustus_token
    headers = {"Authorization": "Bearer " + token} if token else {}
    router = APIRouter(prefix="/api/copilot")
    space_file = Path(data_dir) / "copilot-space-id"
    space_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with space_file.open("x", encoding="utf-8") as file:
            file.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    space_id = space_file.read_text(encoding="utf-8").strip()
    selected = {"url": None}

    async def locate():
        if selected["url"]:
            return selected["url"]
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            for base in urls:
                try:
                    response = await client.get(base.rstrip("/") + "/api/hoard-copilot/capabilities", headers=headers)
                    if response.status_code == 200 and response.json().get("contract_version"):
                        selected["url"] = base.rstrip("/")
                        return selected["url"]
                except (httpx.HTTPError, ValueError):
                    continue
        raise HTTPException(503, "Faustus copilot is unavailable on the configured endpoints")

    async def relay(path, method="GET", body=None, content_type="application/json", extra_headers=None):
        base = await locate()
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        request = client.build_request(method, base + path, content=body, headers={**headers, **(extra_headers or {}), "Content-Type": content_type})
        try:
            upstream = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            selected["url"] = None
            raise HTTPException(503, "Faustus copilot connection failed") from exc
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
        forwarded = {key: value for key, value in upstream.headers.items() if key.lower() in (
            "content-type", "x-odysseus-run-id", "x-faustus-idempotent-replay", "x-faustus-api-version")}
        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=forwarded, background=BackgroundTask(client.aclose))

    @router.get("/info")
    async def info():
        base = await locate()
        return {"app_id": app_id, "app_name": app_name or app_id, "data_space_id": space_id,
                "panel_url": "/api/copilot/panel.js", "server": "/", "tool_server_id": app_id,
                "settings_url": base + "/studio/settings"}

    @router.get("/panel.js")
    async def panel():
        return await relay("/api/hoard-copilot/panel.js")

    @router.post("/proxy")
    async def proxy(request: Request):
        from urllib.parse import urlsplit
        try:
            envelope = await request.json()
            path = envelope.get("path", "")
            route = urlsplit(path).path
            allowed = route.startswith(("/api/hoard-copilot/", "/api/chat/", "/api/session/", "/api/sessions/")) or route in ("/api/chat_stream", "/api/session", "/api/sessions")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or not allowed:
                raise ValueError("Unknown copilot operation")
            body = None
            if "raw_body" in envelope:
                body = base64.b64decode(envelope["raw_body"], validate=True)
                body = multipart_context(body, envelope.get('content_type', ''), {'app_id': app_id, 'data_space_id': space_id})
            elif "body" in envelope:
                payload = envelope["body"]
                if isinstance(payload, dict):
                    for field in ("context", "hoard_context"):
                        if isinstance(payload.get(field), dict):
                            payload[field]["origin"] = {**payload[field].get("origin", {}), "app_id": app_id, "data_space_id": space_id}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            forwarded_headers = {name: value for name, value in (envelope.get("headers") or {}).items()
                                 if name.lower() in ("x-faustus-client-version", "x-odysseus-run-id")}
            return await relay(path, str(envelope.get("method", "GET")).upper(), body, envelope.get("content_type", "application/json"), forwarded_headers)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(400, "Invalid copilot transport request") from exc

    return router
