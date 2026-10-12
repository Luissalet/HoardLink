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
import copy
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from . import DEFAULT_PORT
from ..atomic import write_json_atomic

REPO_DIR = Path(__file__).resolve().parents[2]

_config_lock = threading.RLock()


@contextmanager
def _locked_config(path):
    """Coordinate writers of the same configuration across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock, path.with_suffix(path.suffix + '.lock').open('a+b') as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b'\0'); lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _changed_fields(current, before, after):
    """Apply only this writer's changes, preserving unrelated concurrent fields."""
    result = copy.deepcopy(current) if isinstance(current, dict) else {}
    for key in before.keys() | after.keys():
        if key not in after:
            result.pop(key, None)
        elif key not in before or before[key] != after[key]:
            if isinstance(before.get(key), dict) and isinstance(after[key], dict):
                result[key] = _changed_fields(result.get(key, {}), before[key], after[key])
            else:
                result[key] = copy.deepcopy(after[key])
    return result


def _read_config(path, fallback):
    if not path.exists():
        return copy.deepcopy(fallback)
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict):
        raise ValueError('Hub configuration must be an object')
    return raw


def _split_paths(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()]


@dataclass
class HubConfig:
    port: int = DEFAULT_PORT
    data_dir: str = str(REPO_DIR / "data")
    roots: list[str] = field(default_factory=lambda: [str(REPO_DIR.parent)])
    icon_dirs: list[str] = field(default_factory=lambda: [str(REPO_DIR.parent / "Icons")])
    exclude_ids: list[str] = field(default_factory=lambda: ["scribe"])
    faustus_dir: Optional[str] = None
    faustus_python: Optional[str] = None
    faustus_urls: list[str] = field(default_factory=lambda: ["http://127.0.0.1:7000", "http://127.0.0.1:7001"])
    browser: str = "auto"          # auto | edge | chrome | chromium | <path to a Chromium exe>
    window_engine: str = "auto"    # auto (Hoard Window shell when installed) | shell | chromium
    window_size: list[int] = field(default_factory=lambda: [1280, 860])
    exit_with_window: bool = True
    language: str = "auto"         # auto | en | es
    lease_headroom_mb: int = 256   # VRAM kept free on every GPU when granting leases
    # GPU lease settings: {"protected_gpus": [0, 1]} keeps those GPUs out of every "any" request (only a
    # request that names them gets them). Empty by default: nothing is protected until it is set.
    lease: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)  # cooperative CPU/RAM/IO budgets; GPU uses lease
    fleet: dict[str, Any] = field(default_factory=dict)  # expected_apps, autostart, notifications, enabled
    profiles: dict[str, Any] = field(default_factory=dict)   # name -> {apps, commands, desktop}
    backup: dict[str, Any] = field(default_factory=dict)     # {dir, exclude: [...], max_file_mb, keep, include_hub}
    events_keep: int = 20000       # rows kept in events.db after a prune
    jobs_enabled: bool = True      # the scheduler thread (rules always run)
    link_chat_concurrency: int = 2  # model calls served at once through /api/link/chat; the rest wait in a queue
    # The Repos facet (repos.py): {roots, extra, exclude, faustus_dir, portfolio_dir, stray_prefixes,
    # default_branches, ci, expected_emails}; every key optional.
    repos: dict[str, Any] = field(default_factory=dict)
    # The Web facet (web.py), the family's one web service: {enabled, user_agent, default_min_interval_s,
    # respect_robots, cache_ttl_s, max_bytes, searxng_url, brave_api_key, browser: "auto" | "off"}; every key optional
    # (defaults and validation live in web.py; restart the hub after editing).
    web: dict[str, Any] = field(default_factory=dict)
    # Per-machine launch commands that replace a manifest's launch hint, keyed by app id:
    # {"writer": {"executable": "node", "argv": ["scripts/dev-desktop.mjs"], "cwd": "{APP_DIR}"}}.
    # For an app this machine runs differently from how it is shipped (a developer build, a
    # different install folder) without editing the app's own manifest.
    launch_overrides: dict[str, Any] = field(default_factory=dict)
    # The Ágora's passive observer of the coding agents' transcripts (agent_watch.py): {enabled, hours, max_files,
    # roots: {"codex": [...], "cursor": [...], "claude": [...]}}; every key optional (defaults: the user's home).
    agent_watch: dict[str, Any] = field(default_factory=dict)
    # The Ágora: {"freshness": "warn" | "off"} — whether an answer says that the agent acted on a task without
    # having read what others wrote there since (never a block; docs/AGORA.md). Default warn.
    agora: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self._saved_payload = self._payload()
        self._has_saved_state = False

    def _payload(self):
        value = asdict(self)
        value.pop('data_dir', None)
        return value

    def read_profiles(self):
        with _config_lock:
            raw = _read_config(Path(self.data_dir) / 'hub.json', self._payload())
            self.profiles = copy.deepcopy(raw.get('profiles', self.profiles))
            self._saved_payload['profiles'] = copy.deepcopy(self.profiles)
            return self.profiles

    def update_profiles(self, change):
        path = Path(self.data_dir) / 'hub.json'
        with _locked_config(path):
            raw = _read_config(path, self._payload())
            profiles = copy.deepcopy(raw.get('profiles', self.profiles))
            updated = change(profiles)
            before = self.profiles, copy.deepcopy(self._saved_payload), self._has_saved_state
            self.profiles = copy.deepcopy(updated)
            self._saved_payload['profiles'] = profiles
            self._has_saved_state = True
            self._store_owner = (threading.get_ident(), path.resolve())
            self._profile_write = copy.deepcopy(updated)
            try:
                self.save()
            except Exception:
                self.profiles, self._saved_payload, self._has_saved_state = before
                raise
            finally:
                self._store_owner = None
                self._profile_write = None
        return self.profiles

    @property
    def protected_gpus(self) -> list[int]:
        """``lease.protected_gpus`` of hub.json as a list of GPU indices (junk entries are ignored)."""
        from .lease import _as_index_list
        return _as_index_list((self.lease or {}).get("protected_gpus"))

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
    def lanes_file(self) -> str:
        return os.path.join(self.data_dir, "browser-lanes.json")

    @property
    def events_file(self) -> str:
        return os.path.join(self.data_dir, "events.db")

    @property
    def rules_file(self) -> str:
        return os.path.join(self.data_dir, "rules.json")

    @property
    def jobs_file(self) -> str:
        return os.path.join(self.data_dir, "jobs.json")

    @property
    def backup_dir(self) -> str:
        raw = str((self.backup or {}).get("dir") or "").strip()
        return os.path.abspath(os.path.expanduser(raw)) if raw else os.path.join(self.data_dir, "backups")

    @property
    def repos_file(self) -> str:
        return os.path.join(self.data_dir, "repos.json")

    @property
    def backend_file(self) -> str:
        """The hub's own ``backend.json`` (same schema as an app's): what /api/link/chat resolves with."""
        return os.path.join(self.data_dir, "backend.json")

    @property
    def url_file(self) -> str:
        return os.path.join(self.data_dir, "url")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        """The effective configuration as ``/api/config`` shows it: secrets masked (``save`` writes the real ones)."""
        out = asdict(self)
        web = out.get("web")
        if isinstance(web, dict) and web.get("brave_api_key"):
            out["web"] = {**web, "brave_api_key": "\u2022\u2022\u2022\u2022" + str(web["brave_api_key"])[-4:]}
        return out

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
        if env.get("HOARD_HUB_WINDOW_ENGINE"):
            cfg.window_engine = env["HOARD_HUB_WINDOW_ENGINE"]
        if env.get("HOARD_HUB_LANGUAGE"):
            cfg.language = env["HOARD_HUB_LANGUAGE"]
        if env.get("HOARD_HUB_BACKUP_DIR"):
            cfg.backup = {**(cfg.backup or {}), "dir": env["HOARD_HUB_BACKUP_DIR"]}
        if env.get("HOARD_HUB_JOBS") in ("0", "false", "no", "off"):
            cfg.jobs_enabled = False
        if env.get("HOARD_HUB_LINK_CHAT_CONCURRENCY"):
            try:
                cfg.link_chat_concurrency = int(env["HOARD_HUB_LINK_CHAT_CONCURRENCY"])
            except ValueError:
                pass
        try:
            cfg.link_chat_concurrency = max(1, min(int(cfg.link_chat_concurrency), 16))
        except (TypeError, ValueError):
            cfg.link_chat_concurrency = 2
        if not isinstance(cfg.backup, dict):
            cfg.backup = {}
        if not isinstance(cfg.lease, dict):
            cfg.lease = {}
        if not isinstance(cfg.repos, dict):
            cfg.repos = {}
        if not isinstance(cfg.web, dict):
            cfg.web = {}
        if not isinstance(cfg.agent_watch, dict):
            cfg.agent_watch = {}
        if not isinstance(cfg.agora, dict):
            cfg.agora = {}
        cfg.roots = [os.path.abspath(os.path.expanduser(r)) for r in cfg.roots]
        cfg.icon_dirs = [os.path.abspath(os.path.expanduser(r)) for r in cfg.icon_dirs]
        if not isinstance(cfg.window_size, list) or len(cfg.window_size) != 2:
            cfg.window_size = [1280, 860]
        cfg._saved_payload = cfg._payload()
        cfg._has_saved_state = True
        return cfg

    def save(self, path: Optional[str | Path] = None) -> str:
        file = Path(path) if path else Path(self.data_dir) / "hub.json"
        if getattr(self, '_store_owner', None) == (threading.get_ident(), file.resolve()):
            return self._save_locked(file)
        with _locked_config(file):
            return self._save_locked(file)

    def _save_locked(self, file):
        payload = self._payload()
        current = _read_config(file, payload)
        if getattr(self, '_profile_write', None) is not None:
            current['profiles'] = copy.deepcopy(self._profile_write)
            write_json_atomic(file, current)
            self._saved_payload['profiles'] = copy.deepcopy(self.profiles)
            return str(file)
        before = self._saved_payload if self._has_saved_state else {}
        write_json_atomic(file, _changed_fields(current, before, payload))
        self._saved_payload = copy.deepcopy(payload)
        self._has_saved_state = True
        return str(file)
