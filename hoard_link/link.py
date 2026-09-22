"""The one class every vendoring app imports: :class:`Link`.

See the package README for the resolution order and policies; this module
is the orchestration of :mod:`._probes` and :mod:`._faustus` into that
order, plus the four actions (`chat`, `embed`, `tts`, `comfy`) and the
`wait_idle` / `status` helpers.
"""

from __future__ import annotations

import asyncio
import base64
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from . import _faustus, _probes
from ._comfy import ComfyClient
from ._sync import SyncFacade
from .config import LinkConfig
from .errors import BackendError, Unavailable
from .gpu import gpu_free_mb
from .types import CAPABILITIES, ChatResult, Resolution, Usage

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_CACHE_TTL_S = 30.0


def _host(url: Optional[str]) -> str:
    if not url:
        return "?"
    parts = urlsplit(url)
    return parts.netloc or url


def _strip_think(text: str) -> tuple[str, Optional[str]]:
    if not text:
        return text, None
    blocks = _THINK_RE.findall(text)
    if not blocks:
        return text, None
    cleaned = _THINK_RE.sub("", text).strip()
    reasoning = "\n\n".join(b.strip() for b in blocks)
    return cleaned, reasoning


def _extract_usage(raw: Any, api: Optional[str]) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    if api == "ollama":
        prompt = raw.get("prompt_eval_count")
        completion = raw.get("eval_count")
        total = None
        if prompt is not None and completion is not None:
            total = prompt + completion
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    u = raw.get("usage") or {}
    return Usage(
        prompt_tokens=u.get("prompt_tokens"),
        completion_tokens=u.get("completion_tokens"),
        total_tokens=u.get("total_tokens"),
    )


def _b64_image(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Link:
    """Resolves and calls the shared model backend for one app."""

    def __init__(self, config: LinkConfig, client: Optional[httpx.AsyncClient] = None):
        self.config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._cache: dict[str, tuple[float, Any]] = {}
        # Overridable by tests: replace with a fake clock / no-op sleep to
        # test wait_idle() timing without real delays.
        self._now = time.monotonic
        self._sleep = asyncio.sleep
        self.sync = SyncFacade(self)

    async def aclose(self) -> None:
        self.sync.close()
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Link":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # caching of probes
    # ------------------------------------------------------------------

    async def _cached(self, key: str, factory: Any) -> Any:
        now = self._now()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
        value = await factory()
        self._cache[key] = (now, value)
        return value

    async def _probe_llamacpp(self) -> list[dict]:
        return await self._cached("llamacpp", lambda: _probes.probe_llamacpp(self._client))

    async def _probe_ollama(self) -> Optional[dict]:
        return await self._cached("ollama", lambda: _probes.probe_ollama(self._client))

    async def _probe_openai_compat(self) -> Optional[dict]:
        return await self._cached(
            "openai_compat", lambda: _probes.probe_openai_compat(self._client)
        )

    async def _probe_comfy(self, url: str) -> Optional[dict]:
        return await self._cached(f"comfy:{url}", lambda: _probes.probe_comfy(self._client, url))

    async def _faustus_url(self) -> Optional[str]:
        return await self._cached(
            "faustus_url",
            lambda: _faustus.find_reachable(self._client, self.config.faustus_urls),
        )

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    async def resolve(self, capability: str) -> Resolution:
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {capability!r}, expected one of {CAPABILITIES}")

        reasons: list[str] = []

        explicit = self._resolve_explicit(capability)
        if explicit is not None:
            return explicit

        faustus_url = await self._faustus_url()
        if faustus_url:
            res = await self._resolve_from_faustus(capability, faustus_url, reasons)
            if res is not None:
                return res
        else:
            reasons.append("Faustus not reachable on configured/default ports")

        res = await self._resolve_from_loopback(capability, reasons)
        if res is not None:
            return res

        return Resolution(
            capability=capability,
            provider=None,
            url=None,
            model=None,
            api=None,
            state="unavailable",
            reason="; ".join(reasons) if reasons else "no source available",
            details={"reasons": reasons},
        )

    def _resolve_explicit(self, capability: str) -> Optional[Resolution]:
        cc = self.config.capability(capability)
        if not cc.explicit:
            return None
        if cc.command:
            reason = f"{capability} -> configured command, explicit configuration"
            return Resolution(
                capability=capability,
                provider=cc.provider or "configured-command",
                url=None,
                model=cc.model,
                api=None,
                state="resolved",
                reason=reason,
                details={"source": "explicit", "command": cc.command},
            )
        api = cc.api or "openai"
        provider = cc.provider or "configured"
        model_part = f" ({cc.model})" if cc.model else ""
        reason = f"{capability} -> {provider} at {_host(cc.url)}{model_part}, explicit configuration"
        return Resolution(
            capability=capability,
            provider=provider,
            url=cc.url,
            model=cc.model,
            api=api,  # type: ignore[arg-type]
            state="resolved",
            reason=reason,
            details={"source": "explicit"},
        )

    async def _resolve_from_faustus(
        self, capability: str, faustus_url: str, reasons: list[str]
    ) -> Optional[Resolution]:
        token = self.config.faustus_token
        headers = _faustus.auth_headers(token) if token else None
        status, data = await _faustus.get(self._client, f"{faustus_url}/api/models", headers=headers)

        if status == 200 and isinstance(data, dict):
            items = data.get("items") or []
            item = _faustus.find_model_item(items, capability)
            if item:
                api = _faustus.api_for_backend(item.get("backend"))
                models_list = item.get("models") or []
                model = models_list[0] if models_list else None
                url = item.get("url")
                reason = (
                    f"{capability} -> {item.get('backend', '?')} at {_host(url)} "
                    f"({model}), from Faustus registry; resident"
                )
                return Resolution(
                    capability=capability,
                    provider=item.get("backend"),
                    url=url,
                    model=model,
                    api=api,  # type: ignore[arg-type]
                    state="resolved",
                    reason=reason,
                    details={
                        "source": "faustus_registry",
                        "endpoint_id": item.get("endpoint_id"),
                        "endpoint_name": item.get("endpoint_name"),
                        "category": item.get("category"),
                        "resident": True,
                    },
                )
            reasons.append(f"Faustus registry has no server for capability '{capability}'")
        elif status in (401, 403):
            reasons.append("Faustus reachable but /api/models rejected the token (401/403)")
        elif status is None:
            reasons.append("Faustus /api/models request failed to connect")
        else:
            reasons.append(f"Faustus /api/models returned HTTP {status}")

        if capability in ("tts", "stt"):
            svc_status, svc_data = await _faustus.get(
                self._client, f"{faustus_url}/api/{capability}/capabilities", headers=headers
            )
            if svc_status == 200:
                reason = f"{capability} -> Faustus {capability} service at {_host(faustus_url)}, native Faustus service"
                return Resolution(
                    capability=capability,
                    provider=f"faustus_{capability}",
                    url=faustus_url,
                    model=None,
                    api=None,
                    state="resolved",
                    reason=reason,
                    details={"source": f"faustus_{capability}", "capabilities": svc_data},
                )
            elif svc_status in (401, 403):
                reasons.append(
                    f"Faustus {capability.upper()} needs a browser session; token not allowed"
                )
            elif svc_status is None:
                reasons.append(f"Faustus {capability.upper()} service request failed to connect")
            else:
                reasons.append(f"Faustus {capability}/capabilities returned HTTP {svc_status}")

        return None

    async def _resolve_from_loopback(
        self, capability: str, reasons: list[str]
    ) -> Optional[Resolution]:
        if capability in ("llm", "vision"):
            res = await self._loopback_llamacpp(capability, reasons)
            if res is not None:
                return res
            res = await self._loopback_ollama(capability, reasons)
            if res is not None:
                return res
            if capability == "llm":
                res = await self._loopback_openai_compat(reasons)
                if res is not None:
                    return res
            return None

        if capability == "embeddings":
            return await self._loopback_ollama(capability, reasons)

        if capability in ("image", "video"):
            return await self._loopback_comfy(capability, reasons)

        reasons.append(
            f"no loopback provider implemented for '{capability}' outside explicit configuration/Faustus"
        )
        return None

    async def _loopback_llamacpp(self, capability: str, reasons: list[str]) -> Optional[Resolution]:
        servers = await self._probe_llamacpp()
        if not servers:
            reasons.append("no llama.cpp server found on ports 8080-8090")
            return None
        for s in servers:
            if capability == "vision" and not _probes.llamacpp_supports_vision(s):
                continue
            model = self._llamacpp_model_name(s)
            busy = _probes.llamacpp_busy(s)
            reason = (
                f"{capability} -> llama.cpp at {_host(s['url'])} ({model}), "
                f"shared loopback server; resident"
            )
            if busy:
                reason += "; busy"
            return Resolution(
                capability=capability,
                provider="llamacpp",
                url=f"{s['url']}/v1/chat/completions",
                model=model,
                api="openai",
                state="resolved",
                reason=reason,
                details={"source": "loopback", "busy": busy, "port": s["port"], "resident": True},
            )
        reasons.append(f"llama.cpp server found but does not support '{capability}' (no vision modality)")
        return None

    @staticmethod
    def _llamacpp_model_name(server: dict) -> Optional[str]:
        models_resp = server.get("models")
        if isinstance(models_resp, dict):
            data = models_resp.get("data") or []
            if data and data[0].get("id"):
                return data[0]["id"]
        props = server.get("props") or {}
        model_path = props.get("model_path")
        if model_path:
            return Path(model_path).name
        return None

    async def _loopback_ollama(self, capability: str, reasons: list[str]) -> Optional[Resolution]:
        ollama = await self._probe_ollama()
        if not ollama:
            reasons.append("Ollama not reachable on 11434")
            return None

        needs_cap = {"vision": "vision", "embeddings": "embedding"}.get(capability)
        candidates = [dict(m, would_load=False) for m in ollama["resident"]]
        if not candidates and not self.config.only_resident:
            tags = ollama.get("tags") or []
            candidates = [
                {"name": t.get("name"), "capabilities": [], "would_load": True} for t in tags
            ]

        allow_load = self.config.capability(capability).allow_load
        for m in candidates:
            if needs_cap and needs_cap not in (m.get("capabilities") or []):
                continue
            if m.get("would_load") and not allow_load:
                continue
            model = m["name"]
            resident = not m.get("would_load")
            reason = (
                f"{capability} -> Ollama at {_host(ollama['url'])} ({model}), "
                f"shared loopback server; {'resident' if resident else 'would load'}"
            )
            return Resolution(
                capability=capability,
                provider="ollama",
                url=ollama["url"],
                model=model,
                api="ollama",
                state="resolved",
                reason=reason,
                details={"source": "loopback", "resident": resident},
            )

        if ollama["resident"]:
            reasons.append(f"Ollama has resident models but none support '{capability}'")
        else:
            reasons.append("Ollama has no resident models (only_resident=True)")
        return None

    async def _loopback_openai_compat(self, reasons: list[str]) -> Optional[Resolution]:
        compat = await self._probe_openai_compat()
        if not compat:
            reasons.append("no OpenAI-compatible server found on 1234")
            return None
        model = compat["models"][0]
        reason = f"llm -> OpenAI-compatible server at {_host(compat['url'])} ({model}), shared loopback server"
        return Resolution(
            capability="llm",
            provider="openai_compat",
            url=f"{compat['url']}/v1/chat/completions",
            model=model,
            api="openai",
            state="resolved",
            reason=reason,
            details={"source": "loopback"},
        )

    async def _loopback_comfy(self, capability: str, reasons: list[str]) -> Optional[Resolution]:
        comfy_url = self.config.comfy_url or "http://127.0.0.1:8188"
        comfy = await self._probe_comfy(comfy_url)
        if not comfy:
            reasons.append(f"ComfyUI not reachable at {_host(comfy_url)}")
            return None
        gpus = gpu_free_mb()
        free_mb = gpus[0].free_mb if gpus else None
        reason = f"{capability} -> ComfyUI at {_host(comfy['url'])}, shared loopback server"
        if free_mb is not None:
            reason += f"; {free_mb} MB VRAM free"
        return Resolution(
            capability=capability,
            provider="comfyui",
            url=comfy["url"],
            model=None,
            api=None,
            state="resolved",
            reason=reason,
            details={
                "source": "loopback",
                "checkpoints": comfy["checkpoints"],
                "vram_free_mb": free_mb,
            },
        )

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        capability: str = "llm",
        response_format: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        res = await self.resolve(capability)
        if not res.resolved:
            raise Unavailable(capability, res.details.get("reasons", [res.reason]))

        start = self._now()
        if res.api == "ollama":
            text, raw = await self._chat_ollama(res, messages, images, max_tokens, temperature)
        else:
            text, raw = await self._chat_openai(
                res, messages, images, max_tokens, temperature, response_format
            )
        elapsed_ms = (self._now() - start) * 1000.0

        clean_text, reasoning = _strip_think(text)
        usage = _extract_usage(raw, res.api)
        return ChatResult(
            text=clean_text,
            model=res.model,
            provider=res.provider,
            usage=usage,
            elapsed_ms=elapsed_ms,
            reasoning=reasoning,
        )

    async def _chat_openai(
        self,
        res: Resolution,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[dict[str, Any]],
    ) -> tuple[str, dict]:
        msgs = [dict(m) for m in messages]
        if images:
            content: list[dict[str, Any]] = []
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m
                    break
            if last_user is None:
                last_user = {"role": "user", "content": ""}
                msgs.append(last_user)
            existing = last_user.get("content", "")
            if isinstance(existing, str) and existing:
                content.append({"type": "text", "text": existing})
            elif isinstance(existing, list):
                content.extend(existing)
            for img in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{_b64_image(img)}"},
                    }
                )
            last_user["content"] = content

        payload: dict[str, Any] = {"model": res.model, "messages": msgs, "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format

        resp = await self._client.post(res.url, json=payload, timeout=120.0)
        if resp.status_code >= 400:
            raise BackendError(res.provider, resp.status_code, resp.text[:200])
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return text, data

    @staticmethod
    def _ollama_endpoint(base_url: str, path: str) -> str:
        stripped = base_url.rstrip("/")
        if stripped.endswith(path):
            return stripped
        return f"{stripped}{path}"

    async def _chat_ollama(
        self,
        res: Resolution,
        messages: list[dict[str, Any]],
        images: Optional[list[bytes]],
        max_tokens: Optional[int],
        temperature: Optional[float],
    ) -> tuple[str, dict]:
        msgs = [dict(m) for m in messages]
        if images:
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m
                    break
            if last_user is None:
                last_user = {"role": "user", "content": ""}
                msgs.append(last_user)
            last_user["images"] = [_b64_image(img) for img in images]

        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: dict[str, Any] = {"model": res.model, "messages": msgs, "stream": False}
        if options:
            payload["options"] = options

        endpoint = self._ollama_endpoint(res.url, "/api/chat")
        resp = await self._client.post(endpoint, json=payload, timeout=120.0)
        if resp.status_code >= 400:
            raise BackendError(res.provider, resp.status_code, resp.text[:200])
        data = resp.json()
        text = (data.get("message") or {}).get("content", "")
        return text, data

    async def embed(self, texts: list[str]) -> list[list[float]]:
        res = await self.resolve("embeddings")
        if not res.resolved:
            raise Unavailable("embeddings", res.details.get("reasons", [res.reason]))

        if res.api == "ollama":
            endpoint = self._ollama_endpoint(res.url, "/api/embed")
            resp = await self._client.post(
                endpoint, json={"model": res.model, "input": texts}, timeout=60.0
            )
            if resp.status_code >= 400:
                raise BackendError(res.provider, resp.status_code, resp.text[:200])
            data = resp.json()
            vectors = data.get("embeddings")
            if vectors is None and "embedding" in data:
                vectors = [data["embedding"]]
            return vectors or []

        resp = await self._client.post(
            res.url, json={"model": res.model, "input": texts}, timeout=60.0
        )
        if resp.status_code >= 400:
            raise BackendError(res.provider, resp.status_code, resp.text[:200])
        data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]

    async def tts(self, text: str, voice: Optional[str] = None) -> bytes:
        res = await self.resolve("tts")
        if not res.resolved:
            raise Unavailable("tts", res.details.get("reasons", [res.reason]))

        if res.provider and res.provider.startswith("faustus_"):
            token = self.config.faustus_token
            headers = _faustus.auth_headers(token) if token else None
            payload: dict[str, Any] = {"text": text}
            if voice:
                payload["voice"] = voice
            resp = await self._client.post(
                f"{res.url}/api/tts/synthesize", json=payload, headers=headers, timeout=30.0
            )
            if resp.status_code >= 400:
                raise BackendError(res.provider, resp.status_code, resp.text[:200])
            content_type = resp.headers.get("content-type", "")
            if content_type.startswith("audio/") or content_type == "application/octet-stream":
                return resp.content
            data = resp.json()
            audio_b64 = data.get("audio")
            if audio_b64:
                return base64.b64decode(audio_b64)
            raise BackendError(res.provider, 200, "no audio field in TTS response")

        if res.provider == "configured-command":
            cc = self.config.capability("tts")
            return await self._tts_via_command(cc.command or [], text, voice)

        raise Unavailable("tts", [f"no TTS implementation for resolved provider '{res.provider}'"])

    @staticmethod
    async def _tts_via_command(command: list[str], text: str, voice: Optional[str]) -> bytes:
        if not command:
            raise Unavailable("tts", ["configured TTS command is empty"])
        uses_out_file = any("{out}" in part for part in command)
        out_path: Optional[Path] = None
        try:
            if uses_out_file:
                fd, name = tempfile.mkstemp(suffix=".wav")
                Path(name).write_bytes(b"")
                import os as _os

                _os.close(fd)
                out_path = Path(name)
                argv = [
                    part.format(text=text, voice=voice or "", out=str(out_path))
                    for part in command
                ]
            else:
                argv = [part.format(text=text, voice=voice or "") for part in command]

            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise BackendError(
                    "configured-command", proc.returncode or 1, stderr[:200].decode("utf-8", "replace")
                )
            if out_path is not None:
                return out_path.read_bytes()
            return stdout
        finally:
            if out_path is not None and out_path.exists():
                out_path.unlink(missing_ok=True)

    async def comfy(self) -> Optional[ComfyClient]:
        candidate = self.config.comfy_url
        if not candidate:
            res = await self.resolve("image")
            if res.resolved and res.provider == "comfyui":
                candidate = res.url
        if not candidate:
            return None
        return ComfyClient(candidate, client=self._client)

    async def wait_idle(self, capability: str, max_wait_s: float = 30.0) -> bool:
        res = await self.resolve(capability)
        if not res.resolved or res.provider != "llamacpp":
            return True

        port = res.details.get("port")
        deadline = self._now() + max_wait_s
        while True:
            server = await _probes.probe_llamacpp_port(self._client, port)
            if server is None or not _probes.llamacpp_busy(server):
                return True
            if self._now() >= deadline:
                return False
            await self._sleep(2.0)

    async def status(self) -> dict[str, Any]:
        results = await asyncio.gather(*(self.resolve(cap) for cap in CAPABILITIES))
        return {cap: res.to_dict() for cap, res in zip(CAPABILITIES, results)}
