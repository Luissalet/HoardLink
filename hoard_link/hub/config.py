"""Hub configuration: ``<data>/hub.json``, environment, and sensible defaults.

Defaults are chosen so a clone next to the apps works with no file at all:
the roots are the folder that contains this repository (where a family of
sibling app folders usually lives), the icon folder is ``<that>/Icons``,
and the data folder is ``<repo>/data`` (git-ignored). Everything can be
overridden by ``hub.json`` or by ``HOARD_HUB_*`` environment variables;
the environment wins, so a launcher script can point one instance at a
different set of folders without editing anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from . import DEFAULT_PORT

REPO_DIR = Path(__file__).resolve().parents[2]


def _split_paths(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()]


@dataclass
class HubConfig:
    port: int = DEFAULT_PORT
    data_dir: str = str(REPO_DIR / "data")
    roots: list[str] = field(default_factory=lambda: [str(REPO_DIR.parent)])
    icon_dirs: list[str] = field(default_factory=lambda: [str(REPO_DIR.parent / "Icons")])
    exclude_ids: list[str] = field(default_factory=list)
    faustus_dir: Optional[str] = None
    faustus_python: Optional[str] = None
    faustus_urls: list[str] = field(default_factory=lambda: ["http://127.0.0.1:7000", "http://127.0.0.1:7001"])
    browser: str = "auto"          # auto | edge | chrome | chromium | <path to a Chromium exe>
    window_size: list[int] = field(default_factory=lambda: [1280, 860])
    exit_with_window: bool = True
    language: str = "auto"         # auto | en | es
    lease_headroom_mb: int = 256   # VRAM kept free on every GPU when granting leases

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.data_dir, "logs")

    @property
    def profiles_dir(self) -> str:
        return os.path.join(self.data_dir, "profiles")

    @property
    def token_file(self) -> str:
        return os.path.join(self.data_dir, "mcp-token")

    @property
    def leases_file(self) -> str:
        return os.path.join(self.data_dir, "leases.json")

    @property
    def url_file(self) -> str:
        return os.path.join(self.data_dir, "url")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Optional[str | Path] = None, env: Optional[Mapping[str, str]] = None) -> "HubConfig":
        env = env if env is not None else os.environ
        cfg = cls()
        data_dir = env.get("HOARD_HUB_DATA_DIR")
        if data_dir:
            cfg.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        file = Path(path) if path else Path(cfg.data_dir) / "hub.json"
        if file.is_file():
            try:
                raw = json.loads(file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in cfg.__dataclass_fields__ and key != "data_dir":
                        setattr(cfg, key, value)
        if env.get("HOARD_HUB_PORT"):
            try:
                cfg.port = int(env["HOARD_HUB_PORT"])
            except ValueError:
                pass
        if env.get("HOARD_HUB_ROOTS"):
            cfg.roots = _split_paths(env["HOARD_HUB_ROOTS"])
        if env.get("HOARD_HUB_ICON_DIRS"):
            cfg.icon_dirs = _split_paths(env["HOARD_HUB_ICON_DIRS"])
        if env.get("HOARD_HUB_FAUSTUS_DIR") or env.get("FAUSTUS_DIR"):
            cfg.faustus_dir = env.get("HOARD_HUB_FAUSTUS_DIR") or env.get("FAUSTUS_DIR")
        if env.get("HOARD_HUB_FAUSTUS_PYTHON"):
            cfg.faustus_python = env["HOARD_HUB_FAUSTUS_PYTHON"]
        if env.get("HOARD_HUB_BROWSER"):
            cfg.browser = env["HOARD_HUB_BROWSER"]
        if env.get("HOARD_HUB_LANGUAGE"):
            cfg.language = env["HOARD_HUB_LANGUAGE"]
        cfg.roots = [os.path.abspath(os.path.expanduser(r)) for r in cfg.roots]
        cfg.icon_dirs = [os.path.abspath(os.path.expanduser(r)) for r in cfg.icon_dirs]
        if not isinstance(cfg.window_size, list) or len(cfg.window_size) != 2:
            cfg.window_size = [1280, 860]
        return cfg

    def save(self, path: Optional[str | Path] = None) -> str:
        file = Path(path) if path else Path(self.data_dir) / "hub.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload.pop("data_dir", None)
        file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return str(file)
