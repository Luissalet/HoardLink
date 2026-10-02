"""Models for every app: the hub serves chat and vision to the family.

Python apps reach the local models through the vendored
:class:`hoard_link.Link`. Node apps cannot (Link is Python), and a small
stdlib-only app may not want to carry ``httpx``. The hub is Python, always
running, and already has Link, so it serves the same thing over HTTP:

* ``POST /api/link/chat`` — one chat or vision call, resolved by the hub's
  own Link (same model resolution, GPU lease, reasoning effort and
  idle-wait as an app's own call);
* ``GET /api/link/status`` — which model serves ``llm``, ``vision``,
  ``embed`` and ``tts`` right now, and why not when none does.

The contract (request, answers and status codes) is in ``docs/FAMILY.md``,
section "Models for every app". This module is the whole implementation:
validation, a FIFO gate that keeps at most ``link_chat_concurrency`` model
calls in flight (the others wait in the queue, inside the caller's
timeout), the call itself on a private event loop, lenient JSON parsing of
what the model answered, and the mapping of every failure to a status code.

Nothing here stores or logs message contents: the ``hub.link.chat`` event
carries the app, capability, outcome, duration and model only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import json
import os
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .config import HubConfig

#: Largest request body (messages plus base64 images) the hub reads on this route.
LINK_MAX_BODY = 12 * 1024 * 1024
DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 1800.0
STATUS_CACHE_S = 5.0
STATUS_TIMEOUT_S = 25.0
CHAT_CAPABILITIES = ("llm", "vision")
#: status key -> the capability name Hoard Link knows it by
STATUS_CAPABILITIES = (("llm", "llm"), ("vision", "vision"), ("embed", "embeddings"), ("tts", "tts"))
ROLES = ("system", "user", "assistant")
EFFORTS = ("off", "low", "medium", "high", "max", "auto")
MAX_LINKS = 64

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# JSON the model produced
# ---------------------------------------------------------------------------

def _balanced_end(text: str, start: int) -> int:
    """Index just past the object/array that opens at ``text[start]``, or -1."""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def parse_json_loose(text: Any) -> tuple[Any, bool]:
    """``(value, found)``: the JSON a model put in ``text``. Accepts the bare
    document, a fenced block, or the first balanced object/array inside prose.
    ``found`` is False when nothing parses (the value is then None)."""
    if not isinstance(text, str):
        return None, False
    raw = text.strip()
    if not raw:
        return None, False
    candidates = [raw]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(raw)]
    for cand in candidates:
        try:
            return json.loads(cand), True
        except ValueError:
            pass
    # First balanced {...} or [...] anywhere (fenced blocks included); a start that does not parse is skipped.
    pos = 0
    while pos < len(raw):
        starts = [i for i in (raw.find("{", pos), raw.find("[", pos)) if i >= 0]
        if not starts:
            break
        start = min(starts)
        end = _balanced_end(raw, start)
        if end > 0:
            try:
                return json.loads(raw[start:end]), True
            except ValueError:
                pass
        pos = start + 1
    return None, False


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

class BadRequest(ValueError):
    pass


@dataclasses.dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    capability: str = "llm"
    images: list[bytes] = dataclasses.field(default_factory=list)
    json_mode: bool = False
    schema: Optional[dict[str, Any]] = None
    effort: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout_s: float = DEFAULT_TIMEOUT_S


def parse_request(body: Any) -> ChatRequest:
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    capability = str(body.get("capability") or "llm").strip().lower()
    if capability not in CHAT_CAPABILITIES:
        raise BadRequest(f"capability must be one of {', '.join(CHAT_CAPABILITIES)}")
    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list) or not raw_msgs:
        raise BadRequest("messages must be a non-empty list of {role, content}")
    messages: list[dict[str, Any]] = []
    for i, m in enumerate(raw_msgs):
        if not isinstance(m, dict):
            raise BadRequest(f"messages[{i}] must be an object")
        role = str(m.get("role") or "").strip().lower()
        content = m.get("content")
        if role not in ROLES:
            raise BadRequest(f"messages[{i}].role must be one of {', '.join(ROLES)}")
        if not isinstance(content, str):
            raise BadRequest(f"messages[{i}].content must be a string")
        messages.append({"role": role, "content": content})
    if not any(m["role"] == "user" for m in messages):
        raise BadRequest("messages needs at least one user message")

    images: list[bytes] = []
    raw_images = body.get("images")
    if raw_images not in (None, []):
        if capability != "vision":
            raise BadRequest("images are only accepted with capability 'vision'")
        if not isinstance(raw_images, list):
            raise BadRequest("images must be a list of base64 strings")
        for i, img in enumerate(raw_images):
            if not isinstance(img, str) or not img.strip():
                raise BadRequest(f"images[{i}] must be a base64 string")
            data = img.strip()
            if data.startswith("data:") and "," in data:   # tolerate a data URL
                data = data.split(",", 1)[1]
            data = "".join(data.split())
            try:
                images.append(base64.b64decode(data + "=" * (-len(data) % 4), validate=True))
            except (binascii.Error, ValueError):
                raise BadRequest(f"images[{i}] is not valid base64") from None
            if not images[-1]:
                raise BadRequest(f"images[{i}] is empty")
    elif capability == "vision":
        raise BadRequest("capability 'vision' needs at least one image in images")

    want_json = body.get("json")
    json_mode, schema = False, None
    if isinstance(want_json, dict):
        json_mode, schema = True, want_json
    elif want_json in (True, 1):
        json_mode = True
    elif want_json not in (None, False, 0):
        raise BadRequest("json must be true or a JSON Schema object")

    effort = body.get("effort")
    if effort is not None:
        effort = str(effort).strip().lower()
        if effort not in EFFORTS:
            raise BadRequest(f"effort must be one of {', '.join(EFFORTS)}")
        if effort == "auto":
            effort = None

    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise BadRequest("max_tokens must be a positive integer")
    temperature = body.get("temperature")
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 2:
            raise BadRequest("temperature must be a number between 0 and 2")
        temperature = float(temperature)
    timeout_s = body.get("timeout_s")
    if timeout_s is None:
        timeout_s = DEFAULT_TIMEOUT_S
    elif isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise BadRequest("timeout_s must be a positive number")
    timeout_s = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))
    return ChatRequest(messages=messages, capability=capability, images=images, json_mode=json_mode, schema=schema,
                       effort=effort, max_tokens=max_tokens, temperature=temperature, timeout_s=timeout_s)


def with_json_instruction(messages: list[dict[str, Any]], schema: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with a system instruction to answer in JSON, added to the
    first system message (or as a new one). Always sent, even when the
    backend also gets a response format: not every server honours that field."""
    text = "Answer with a single valid JSON value and nothing else: no prose before or after, no code fences."
    if schema:
        text += " It must conform to this JSON Schema: " + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    out = [dict(m) for m in messages]
    for m in out:
        if m["role"] == "system":
            m["content"] = (m["content"].rstrip() + "\n\n" + text) if m["content"].strip() else text
            return out
    return [{"role": "system", "content": text}] + out


# ---------------------------------------------------------------------------
# The FIFO gate
# ---------------------------------------------------------------------------

class Gate:
    """At most ``limit`` holders at once; the rest wait in arrival order."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._cv = threading.Condition()
        self._active = 0
        self._queue: deque[object] = deque()

    @property
    def active(self) -> int:
        with self._cv:
            return self._active

    @property
    def waiting(self) -> int:
        with self._cv:
            return len(self._queue)

    def acquire(self, timeout: float) -> bool:
        ticket = object()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._active < self.limit):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cv.wait(left)
                self._queue.popleft()
                self._active += 1
                self._cv.notify_all()
                return True
            finally:
                if ticket in self._queue:      # gave up (timeout): leave the line
                    self._queue.remove(ticket)
                    self._cv.notify_all()

    def release(self) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class LinkService:
    """What ``hub.link`` is: chat/vision and status for the family.

    ``link_factory(app)`` returns an object with ``async chat(...)`` and
    ``async resolve(capability)`` like :class:`hoard_link.Link` (tests pass a
    fake). The real factory builds one Link per calling app so a GPU lease
    taken for a load is owned by the app that asked for it.
    """

    def __init__(self, config: HubConfig, *, emit: Optional[Callable[[str, dict[str, Any]], Any]] = None,
                 link_factory: Optional[Callable[[str], Any]] = None, now: Callable[[], float] = time.time):
        self.config = config
        self._emit = emit
        self._factory = link_factory or self._default_link
        self._now = now
        self.gate = Gate(int(config.link_chat_concurrency or 2))
        self._links: dict[str, Any] = {}
        self._links_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()
        self._status_cache: tuple[float, Optional[dict[str, Any]]] = (0.0, None)
        self._status_lock = threading.Lock()

    # -- plumbing -------------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()
            box: dict[str, asyncio.AbstractEventLoop] = {}

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                box["loop"] = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=runner, name="hoard-hub-link", daemon=True)
            self._thread.start()
            ready.wait()
            self._loop = box["loop"]
            return self._loop

    def close(self) -> None:
        with self._loop_lock:
            loop, self._loop = self._loop, None
        if loop is not None:
            with self._links_lock:
                links, self._links = list(self._links.values()), {}
            for link in links:
                aclose = getattr(link, "aclose", None)
                if aclose is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(aclose(), loop).result(2.0)
                    except Exception:  # noqa: BLE001
                        pass
            loop.call_soon_threadsafe(loop.stop)

    def link_config(self, app: str):
        """The Link configuration the hub serves with: ``<data>/backend.json`` (the same schema as an
        app's) plus ``HOARD_*`` variables, the hub's own Faustus URLs, and the hub itself as lease arbiter."""
        from ..config import LinkConfig

        path = self.config.backend_file
        cfg = LinkConfig.load(path if os.path.isfile(path) else None, app=app)
        changes: dict[str, Any] = {"hub_url": self.config.url}
        if not (os.environ.get("HOARD_FAUSTUS_URL") or "").strip():
            changes["faustus_urls"] = tuple(self.config.faustus_urls)
        return dataclasses.replace(cfg, **changes)

    def _default_link(self, app: str) -> Any:
        from ..link import Link

        return Link(self.link_config(app))

    def _link(self, app: str) -> Any:
        app = (app or "hub")[:80]
        with self._links_lock:
            link = self._links.get(app)
            if link is None:
                if len(self._links) >= MAX_LINKS:
                    app = "hub"
                    link = self._links.get(app)
                if link is None:
                    link = self._links[app] = self._factory(app)
            return link

    def _record(self, app: str, req_cap: str, ok: bool, started: float, model: Optional[str], error: Optional[str]) -> None:
        if self._emit is None:
            return
        data: dict[str, Any] = {"app": app, "capability": req_cap, "ok": ok, "ms": int((time.monotonic() - started) * 1000),
                                "model": model}
        if error:
            data["error"] = error
        try:
            self._emit("hub.link.chat", data)
        except Exception:  # noqa: BLE001 - the bus is best effort
            pass

    # -- status ---------------------------------------------------------------
    def status(self, force: bool = False) -> dict[str, Any]:
        with self._status_lock:
            ts, cached = self._status_cache
            if not force and cached is not None and self._now() - ts < STATUS_CACHE_S:
                return self._with_gate(cached)
            result = self._status_now()
            self._status_cache = (self._now(), result)
            return self._with_gate(result)

    def _with_gate(self, base: dict[str, Any]) -> dict[str, Any]:
        return {**base, "chat": {"concurrency": self.gate.limit, "active": self.gate.active, "queued": self.gate.waiting}}

    def _status_now(self) -> dict[str, Any]:
        async def run() -> list[Any]:
            link = self._link("hoard-hub")
            return list(await asyncio.gather(*(link.resolve(cap) for _, cap in STATUS_CAPABILITIES), return_exceptions=True))

        out: dict[str, Any] = {"ok": True}
        try:
            fut = asyncio.run_coroutine_threadsafe(asyncio.wait_for(run(), STATUS_TIMEOUT_S), self._ensure_loop())
            results = fut.result(STATUS_TIMEOUT_S + 5)
        except Exception as exc:  # noqa: BLE001
            results = [exc] * len(STATUS_CAPABILITIES)
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        for (key, _cap), res in zip(STATUS_CAPABILITIES, results):
            if isinstance(res, BaseException):
                out[key] = {"available": False, "model": None, "provider": None, "reason": f"{type(res).__name__}: {res}"[:200]}
                continue
            out[key] = {"available": bool(getattr(res, "resolved", False)), "model": getattr(res, "model", None),
                        "provider": getattr(res, "provider", None), "reason": getattr(res, "reason", "") or ""}
        out["checked_at"] = self._now()
        return out

    # -- chat -----------------------------------------------------------------
    def chat(self, app: str, body: Any) -> tuple[int, dict[str, Any]]:
        """Run one call for ``app``. Returns ``(http status, payload)``; never raises."""
        started = time.monotonic()
        cap = str((body or {}).get("capability") or "llm") if isinstance(body, dict) else "llm"
        try:
            req = parse_request(body)
        except BadRequest as exc:
            self._record(app, cap, False, started, None, "bad_request")
            return 400, {"ok": False, "error": "bad_request", "detail": str(exc)}
        deadline = started + req.timeout_s
        if not self.gate.acquire(req.timeout_s):
            self._record(app, req.capability, False, started, None, "timeout")
            return 504, {"ok": False, "error": "timeout",
                         "detail": f"waited {req.timeout_s:.0f}s in the queue behind other model calls "
                                   f"(link_chat_concurrency={self.gate.limit})"}
        try:
            status, payload = self._run(app, req, started, deadline)
        except Exception as exc:  # noqa: BLE001
            status, payload = 500, {"ok": False, "error": "internal", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            self.gate.release()
        self._record(app, req.capability, bool(payload.get("ok")), started, payload.get("model"),
                     None if payload.get("ok") else str(payload.get("error")))
        return status, payload

    def _run(self, app: str, req: ChatRequest, started: float, deadline: float) -> tuple[int, dict[str, Any]]:
        from ..errors import BackendError, Unavailable

        left = deadline - time.monotonic()
        if left <= 0:
            return 504, {"ok": False, "error": "timeout", "detail": "the time ran out while queued"}
        queued_ms = int((time.monotonic() - started) * 1000)
        link = self._link(app)
        messages = with_json_instruction(req.messages, req.schema) if req.json_mode else req.messages
        formats: list[Optional[dict[str, Any]]] = [None]
        if req.json_mode:
            fmt = ({"type": "json_schema", "json_schema": {"name": "response", "schema": req.schema}} if req.schema
                   else {"type": "json_object"})
            formats = [fmt, None]     # then the same call without it, for a server that rejects the field

        async def go() -> Any:
            last: Optional[BaseException] = None
            for fmt in formats:
                try:
                    return await link.chat(messages, images=req.images or None, max_tokens=req.max_tokens,
                                           temperature=req.temperature, capability=req.capability,
                                           response_format=fmt, effort=req.effort)
                except BackendError as exc:
                    last = exc
                    if fmt is not None and exc.status in (400, 422):
                        continue
                    raise
            assert last is not None
            raise last

        try:
            fut = asyncio.run_coroutine_threadsafe(asyncio.wait_for(go(), left), self._ensure_loop())
            res = fut.result(left + 5)
        except Unavailable as exc:
            detail = "; ".join(exc.reasons) or str(exc)
            if any("GPU busy" in r for r in exc.reasons):
                return 503, {"ok": False, "error": "gpu_busy", "detail": detail}
            return 503, {"ok": False, "error": "no_model", "detail": detail}
        except (asyncio.TimeoutError, TimeoutError):
            return 504, {"ok": False, "error": "timeout", "detail": f"no answer within {req.timeout_s:.0f}s"}
        except BackendError as exc:
            if exc.status == 0 and "timeout" in str(exc).lower():
                return 504, {"ok": False, "error": "timeout", "detail": str(exc)[:300], "provider": exc.provider}
            return 502, {"ok": False, "error": "backend_error", "detail": str(exc)[:300], "provider": exc.provider,
                         "backend_status": exc.status}
        payload: dict[str, Any] = {
            "ok": True, "text": res.text, "json": None, "model": res.model, "provider": res.provider,
            "ms": int(res.elapsed_ms), "queued_ms": queued_ms, "effort": res.effort,
            "usage": res.usage.to_dict() if hasattr(res.usage, "to_dict") else None,
        }
        if req.json_mode:
            value, found = parse_json_loose(res.text)
            payload["json"] = value
            if not found:
                payload["json_error"] = "the model's answer contains no parseable JSON; read text"
        return 200, payload
