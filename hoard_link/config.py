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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .types import CAPABILITIES, Capability


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
                raw = json.loads(p.read_text(encoding="utf-8"))

        only_resident = bool(raw.get("only_resident", True))

        faustus_raw = raw.get("faustus", {}) or {}
        faustus_urls: tuple[str, ...]
        if faustus_raw.get("url"):
            faustus_urls = (faustus_raw["url"],)
        else:
            faustus_urls = ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
        faustus_token = faustus_raw.get("token")

        comfy_url = (raw.get("comfy", {}) or {}).get("url")

        caps: dict[str, CapabilityConfig] = {}
        raw_caps = raw.get("capabilities", {}) or {}
        for cap in CAPABILITIES:
            c = raw_caps.get(cap, {}) or {}
            caps[cap] = CapabilityConfig(
                url=c.get("url"),
                model=c.get("model"),
                api=c.get("api"),
                provider=c.get("provider"),
                allow_load=bool(c.get("allow_load", False)),
                command=c.get("command"),
            )

        # --- environment overrides (highest priority) ---
        if env.get("HOARD_FAUSTUS_URL"):
            faustus_urls = (env["HOARD_FAUSTUS_URL"],)
        if env.get("HOARD_FAUSTUS_TOKEN"):
            faustus_token = env["HOARD_FAUSTUS_TOKEN"]
        if env.get("HOARD_COMFY_URL"):
            comfy_url = env["HOARD_COMFY_URL"]

        for cap in CAPABILITIES:
            url_key = f"HOARD_{cap.upper()}_URL"
            model_key = f"HOARD_{cap.upper()}_MODEL"
            if env.get(url_key) or env.get(model_key):
                current = caps[cap]
                caps[cap] = CapabilityConfig(
                    url=env.get(url_key, current.url),
                    model=env.get(model_key, current.model),
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
