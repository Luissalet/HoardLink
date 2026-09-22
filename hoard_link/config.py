"""Explicit configuration: an app's ``backend.json`` plus environment overrides.

This is resolution-order source #1 in the README: whatever is set here wins
outright, no probing needed (a real HTTP call will surface a
:class:`~hoard_link.errors.BackendError` on its own if the address is
stale).

``backend.json`` schema (every key optional)::

    {
      "only_resident": true,
      "faustus": {"url": "http://127.0.0.1:7000", "token": "ody_..."},
      "comfy": {"url": "http://127.0.0.1:8188"},
      "capabilities": {
        "llm": {
          "url": "http://127.0.0.1:8081/v1/chat/completions",
          "model": "qwen3.8-27b-q8-llamacpp",
          "api": "openai",
          "provider": "llamacpp",
          "allow_load": false
        },
        "tts": {"command": ["piper", "--model", "es_ES.onnx", "--output_file", "{out}"]}
      }
    }

Environment overrides (highest priority, applied on top of the file):

- ``HOARD_<CAP>_URL`` / ``HOARD_<CAP>_MODEL`` for each capability, e.g.
  ``HOARD_LLM_URL``, ``HOARD_VISION_MODEL``.
- ``HOARD_FAUSTUS_URL``, ``HOARD_FAUSTUS_TOKEN``.
- ``HOARD_COMFY_URL``.

A ``model`` without a ``url`` (in the file or as ``HOARD_<CAP>_MODEL``)
does not pin a server: it is a *preference* used wherever resolution has a
choice (Ollama resident models, the Faustus registry's model list, an
OpenAI-compatible server's model list). Empty environment variables are
treated as unset.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .types import CAPABILITIES


def _section(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _clean_url(url: str) -> str:
    return str(url).strip().rstrip("/")


@dataclass(frozen=True)
class CapabilityConfig:
    url: Optional[str] = None
    model: Optional[str] = None
    api: Optional[str] = None
    provider: Optional[str] = None
    allow_load: bool = False
    command: Optional[list[str]] = None

    @property
    def explicit(self) -> bool:
        """True when this capability was pinned by config/env (not probed)."""
        return bool(self.url or self.command)


@dataclass(frozen=True)
class LinkConfig:
    app: str = "app"
    only_resident: bool = True
    faustus_urls: tuple[str, ...] = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
    faustus_token: Optional[str] = None
    comfy_url: Optional[str] = None
    capabilities: dict[str, CapabilityConfig] = field(default_factory=dict)

    def capability(self, capability: str) -> CapabilityConfig:
        return self.capabilities.get(capability, CapabilityConfig())

    @classmethod
    def load(
        cls,
        path: Optional[str | Path] = None,
        env: Optional[Mapping[str, str]] = None,
        app: str = "app",
    ) -> "LinkConfig":
        env = env if env is not None else os.environ
        raw: dict[str, Any] = {}
        if path is not None:
            p = Path(path)
            if p.is_file():
                # utf-8-sig: Windows Notepad saves UTF-8 with a BOM, which
                # plain json.loads rejects.
                try:
                    raw = json.loads(p.read_text(encoding="utf-8-sig"))
                except ValueError as exc:
                    raise ValueError(f"{p}: not valid JSON ({exc})") from exc
                if not isinstance(raw, dict):
                    raise ValueError(f"{p}: expected a JSON object at the top level")

        def env_value(key: str) -> Optional[str]:
            # An empty variable (``set HOARD_LLM_URL=``) means "not set".
            value = env.get(key)
            return value.strip() if value and value.strip() else None

        only_resident = bool(raw.get("only_resident", True))

        faustus_raw = _section(raw, "faustus")
        faustus_urls: tuple[str, ...]
        if faustus_raw.get("url"):
            faustus_urls = (_clean_url(faustus_raw["url"]),)
        else:
            faustus_urls = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
        faustus_token = faustus_raw.get("token") or None

        comfy_url = _section(raw, "comfy").get("url")

        caps: dict[str, CapabilityConfig] = {}
        raw_caps = _section(raw, "capabilities")
        for cap in CAPABILITIES:
            c = _section(raw_caps, cap)
            command = c.get("command")
            if command is not None and (
                not isinstance(command, list) or not all(isinstance(x, str) for x in command)
            ):
                raise ValueError(
                    f"capabilities.{cap}.command must be a list of strings, e.g. "
                    '["piper", "--model", "voice.onnx", "--output_file", "{out}"]'
                )
            caps[cap] = CapabilityConfig(
                url=c.get("url") or None,
                model=c.get("model") or None,
                api=c.get("api") or None,
                provider=c.get("provider") or None,
                allow_load=bool(c.get("allow_load", False)),
                command=command or None,
            )

        # --- environment overrides (highest priority) ---
        if env_value("HOARD_FAUSTUS_URL"):
            faustus_urls = (_clean_url(env_value("HOARD_FAUSTUS_URL")),)
        if env_value("HOARD_FAUSTUS_TOKEN"):
            faustus_token = env_value("HOARD_FAUSTUS_TOKEN")
        if env_value("HOARD_COMFY_URL"):
            comfy_url = env_value("HOARD_COMFY_URL")
        if comfy_url:
            comfy_url = _clean_url(comfy_url)

        for cap in CAPABILITIES:
            env_url = env_value(f"HOARD_{cap.upper()}_URL")
            env_model = env_value(f"HOARD_{cap.upper()}_MODEL")
            if env_url or env_model:
                current = caps[cap]
                caps[cap] = CapabilityConfig(
                    url=env_url or current.url,
                    model=env_model or current.model,
                    api=current.api,
                    provider=current.provider,
                    allow_load=current.allow_load,
                    command=current.command,
                )

        return cls(
            app=app,
            only_resident=only_resident,
            faustus_urls=faustus_urls,
            faustus_token=faustus_token,
            comfy_url=comfy_url,
            capabilities=caps,
        )
