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


ALLOWED_PREFIXES = ("/api/hoard-copilot/", "/api/chat/", "/api/session/", "/api/sessions/")
ALLOWED_EXACT = ("/api/chat_stream", "/api/session", "/api/sessions")


def allowed_route(path):
    """True when ``path`` (what the browser asks the relay to forward) is one of the copilot operations.

    Dot segments are refused, plain or percent-encoded (``/api/chat/../settings``, ``/api/chat/%2e%2e/settings``): an
    upstream that resolves them would otherwise be reached outside the list with the server's token."""
    from urllib.parse import unquote, urlsplit
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return False
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return False
    decoded = unquote(unquote(parts.path))
    if "\\" in decoded or any(segment in (".", "..") for segment in decoded.split("/")):
        return False
    return parts.path.startswith(ALLOWED_PREFIXES) or parts.path in ALLOWED_EXACT


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
    router.hoard_copilot = app_id  # lets install_spa see that this app already has its relay
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
            if not allowed_route(path):
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


def has_copilot(app):
    """True when ``app`` already serves ``/api/copilot/info`` (its own relay, or ``install_copilot`` ran), looking through
    included routers too (FastAPI keeps them as one route object)."""
    def walk(routes, prefix=""):
        for route in routes or []:
            path = getattr(route, "path", None)
            if path and prefix + path == "/api/copilot/info":
                return True
            inner = getattr(route, "original_router", None) or getattr(route, "router", None)
            if inner is not None and inner is not route:
                if getattr(inner, "hoard_copilot", None):
                    return True
                context = getattr(route, "include_context", None)
                extra = getattr(context, "prefix", "") or ""
                if walk(getattr(inner, "routes", None), prefix + extra):
                    return True
        return False
    return bool(getattr(getattr(app, "state", None), "hoard_copilot", None)) or walk(getattr(getattr(app, "router", None), "routes", None))


LAUNCHER_PATH = "/api/copilot/launcher.js"
# What an app's own session guard must let through for the copilot to start: the page loads the launcher before the
# session cookie exists. ``/info``, ``panel.js`` and ``/proxy`` stay behind the guard.
COPILOT_PUBLIC_PATHS = (LAUNCHER_PATH,)
# The anti-CSRF header the launcher sends with its own requests (``/info`` and ``/proxy``): an app that refuses
# state-changing requests without it needs no exception for the relay.
COPILOT_REQUEST_HEADER = ("X-Hoard-Request", "1")
_LAUNCHER_TAG = b'<script type="module" src="/api/copilot/launcher.js"></script>'


def inject_launcher(html):
    """``html`` (bytes) with the launcher script before ``</body>`` (or at the end); unchanged when already there."""
    if b"/api/copilot/launcher.js" in html:
        return html
    lower = html.lower()
    at = lower.rfind(b"</body>")
    if at < 0:
        return html + _LAUNCHER_TAG
    return html[:at] + _LAUNCHER_TAG + html[at:]


class CopilotInjectMiddleware:
    """Pure ASGI: add the launcher script to every HTML page the app serves (its SPA shell, server-rendered pages).

    Only GET/HEAD answers with ``Content-Type: text/html`` outside ``/api/`` are touched; the body is buffered (pages are
    small), ``Content-Length`` is fixed. Install it inside compression middleware
    so it receives HTML before encoding."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD") or str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        state = {"start": None, "html": False, "chunks": []}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                kind = headers.get(b"content-type", b"").lower()
                state["html"] = kind.startswith(b"text/html") and not headers.get(b"content-encoding")
                if not state["html"]:
                    await send(message)
                    return
                state["start"] = message
                return
            if message["type"] == "http.response.body" and state["html"]:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = inject_launcher(b"".join(state["chunks"]))
                start = state["start"]
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() not in (b"content-length", b"etag")]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body if scope.get("method") == "GET" else b""})
                return
            await send(message)

        await self.app(scope, receive, wrapped)


def install_copilot(app, app_id, data_dir, *, app_name=None, faustus_urls=None, token=None, inject=True, config_path=None):
    """Give a FastAPI Hoard the Faustus copilot: the relay (``/api/copilot/info``, ``panel.js``, ``proxy``), the launcher
    (``/api/copilot/launcher.js``) and, with ``inject``, the launcher added to every HTML page the app serves. Apps that
    already mount their own panel component pass ``inject=False``. Routes go before any SPA catch-all.

    The panel's conversation, model and tools live in Faustus; the app keeps no loop and no history. Faustus is found
    through ``faustus_urls`` (or ``HOARD_FAUSTUS_URL`` / the link config) and the token stays on the server."""
    import os
    from starlette.responses import Response
    from .config import LinkConfig
    from .copilot_launcher import LAUNCHER_JS

    if faustus_urls is None or token is None:
        link = LinkConfig.load(path=config_path, app=app_id)
        env_url = os.environ.get(app_id.upper().replace("-", "_") + "_FAUSTUS_URL") or os.environ.get("HOARD_FAUSTUS_URL")
        if faustus_urls is None:
            faustus_urls = [env_url] if env_url else list(link.faustus_urls)
        if token is None:
            token = link.faustus_token
    router = make_copilot_router(app_id, data_dir, faustus_urls=faustus_urls, token=token, app_name=app_name)

    async def launcher():
        return Response(LAUNCHER_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    router.add_api_route("/launcher.js", launcher, methods=["GET"], include_in_schema=False)
    before = len(getattr(app.router, "routes", []))
    app.include_router(router)
    routes = getattr(app.router, "routes", None)
    if isinstance(routes, list) and len(routes) > before:
        ours = routes[before:]
        del routes[before:]
        routes[0:0] = ours
    app.state.hoard_copilot = app_id
    if inject:
        app.add_middleware(CopilotInjectMiddleware)
        # ASGI responses travel from the innermost middleware outward. Inject
        # before a previously registered compressor sees and encodes the HTML.
        app.user_middleware.append(app.user_middleware.pop(0))
    return {"app_id": app_id, "launcher": LAUNCHER_PATH, "inject": bool(inject)}
