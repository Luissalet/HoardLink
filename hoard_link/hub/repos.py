"""The Repos facet: the state of every git repository of the family, at a glance.

Luis keeps a few dozen repositories next to each other (one per Hoard app,
this one, the portfolio, Faustus...). What goes wrong with them is quiet:
commits never pushed, a vendored copy of ``hoard_link`` that drifted, a
theme copy out of sync, a plugin manifest that differs from the one Faustus
carries, stray branches, a missing README or LICENSE. This module scans
them and says which, with the exact command to fix each (text only).

**The hub never changes a repository.** It does not push, commit, reset,
delete or check out anything; the single network call is :meth:`fetch`
(``git fetch --prune``), only when asked for by name, and it only updates
remote-tracking refs. Every other git call is a read, run with
``--no-optional-locks`` and ``GIT_OPTIONAL_LOCKS=0`` so a scan never takes
(or leaves behind) ``index.lock`` — a known trap when an editor or another
agent is working in the same repository.

Discovery: every direct child of a registry root (the parents of the apps
the hub lists, plus the configured roots) that has a ``.git``, plus the
configured extras and Faustus. ``hub.json`` ``repos`` tunes it::

    "repos": {"roots": [], "extra": [], "exclude": [], "faustus_dir": "D:/LocalAI/faustus",
              "portfolio_dir": "…/portfolio-react", "stray_prefixes": ["claude/"],
              "default_branches": ["main", "master"], "ci": true,
              "expected_emails": ["luissalet@users.noreply.github.com"]}

The snapshot lives in memory and in ``<data>/repos.json`` (first paint after
a restart). Reading it (:meth:`snapshot`) never waits for git: it returns
the cache at once and starts a background refresh when it is older than five
minutes; one refresh runs at a time, repositories are scanned in parallel,
and the CI status (``gh run list``, cached ten minutes, optional) is filled
in afterwards without holding the snapshot back.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from . import drift

GIT_TIMEOUT_S = 10.0
FETCH_TIMEOUT_S = 60.0
CI_TIMEOUT_S = 15.0
CI_CACHE_S = 600.0
STALE_AFTER_S = 300.0
STALE_LOCK_S = 600.0
SCAN_WORKERS = 6
CI_WORKERS = 4
MAX_PATHS = 50
MAX_COMMITS = 10
DEFAULT_EMAILS = ["luissalet@users.noreply.github.com"]
CREATE_NO_WINDOW = 0x08000000

#: (severity, label) — severities are ``error`` > ``warn`` > ``info``.
SEVERITY_ORDER = {"error": 3, "warn": 2, "info": 1}

_SECRET_EXEMPT_ENV = {".env.example", ".env.sample", ".env.template", ".env.dist"}


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

class GitResult(NamedTuple):
    rc: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GCM_INTERACTIVE": "never"})
    return env


def run_git(repo: str, *args: str, timeout: float = GIT_TIMEOUT_S, git: Optional[str] = None) -> GitResult:
    """One git call, never raising. Never prompts, never waits more than ``timeout`` seconds."""
    argv = [git or shutil.which("git") or "git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", repo, *args]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=git_env(), stdin=subprocess.DEVNULL, **kwargs)
    except subprocess.TimeoutExpired:
        return GitResult(-1, "", f"timed out after {timeout:.0f}s", True)
    except OSError as exc:
        return GitResult(127, "", str(exc))
    return GitResult(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))


def sanitize_url(url: str) -> str:
    """Remove credentials from a remote URL (``https://user:token@host/…`` → ``https://host/…``)."""
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@", r"\1", str(url or "").strip())


_SCP_RE = re.compile(r"^(?:[^@\s/]+@)?([^:/\s]+):(?!//)(.+)$")
_SLUG_RE = re.compile(r"^/?([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def parse_github(url: str) -> Optional[str]:
    """``owner/repo`` of a GitHub remote, in every spelling Luis uses: https, ``ssh://git@github.com/…`` and the
    scp style with an ssh alias for the host (``git@Luissalet:Luissalet/X.git``). A host with a dot that is not
    github.com (gitlab.com, a company server) is not GitHub; an alias has no dot."""
    url = sanitize_url(url)
    if not url:
        return None
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@]*@)?([^/:]+)(?::\d+)?/(.+)$", url)
    if m:
        host, path = m.group(1).lower(), m.group(2)
    else:
        m = _SCP_RE.match(url)
        if not m:
            return None
        host, path = m.group(1).lower(), m.group(2)
        if "." in host and host not in ("github.com", "www.github.com"):
            return None
        if len(host) == 1:  # a Windows drive letter (D:/repos/x.git), not an ssh alias
            return None
    if "." in host and host not in ("github.com", "www.github.com"):
        return None
    s = _SLUG_RE.match(path)
    return f"{s.group(1)}/{s.group(2)}" if s else None


def parse_status(text: str) -> dict[str, Any]:
    """``git status --porcelain=v1 -z`` → counts and the first :data:`MAX_PATHS` paths."""
    parts = text.split("\0")
    staged = modified = untracked = conflicted = total = 0
    paths: list[dict[str, str]] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x in "RC" or y in "RC":
            i += 1  # the original path of a rename/copy follows as its own field
        total += 1
        if x == "?" and y == "?":
            untracked += 1
        elif x == "!":
            total -= 1
            continue
        else:
            if x not in " ?!":
                staged += 1
            if y not in " ?!":
                modified += 1
            if x + y in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
                conflicted += 1
        if len(paths) < MAX_PATHS:
            paths.append({"xy": x + y, "path": path})
    return {"staged": staged, "modified": modified, "untracked": untracked, "conflicted": conflicted,
            "total": total, "paths": paths, "truncated": total > len(paths)}


def _secret_like(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    low = base.lower()
    if low == ".env" or (low.startswith(".env.") and low not in _SECRET_EXEMPT_ENV):
        return True
    if low.endswith((".pem", ".key")) or low.startswith("id_rsa") or low == "mcp-token":
        return True
    if p.startswith("data/") and low not in (".gitkeep", ".gitignore"):
        return True
    return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _paths(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [os.path.abspath(os.path.expanduser(str(v))) for v in (value or []) if str(v or "").strip()]


@dataclass
class RepoSettings:
    roots: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    faustus_dir: Optional[str] = None
    portfolio_dir: Optional[str] = None
    stray_prefixes: list[str] = field(default_factory=lambda: ["claude/"])
    default_branches: list[str] = field(default_factory=lambda: ["main", "master"])
    ci: bool = True
    expected_emails: list[str] = field(default_factory=lambda: list(DEFAULT_EMAILS))

    @classmethod
    def from_config(cls, repos_cfg: Any, hub_faustus_dir: Optional[str] = None) -> "RepoSettings":
        raw = repos_cfg if isinstance(repos_cfg, dict) else {}
        faustus = raw.get("faustus_dir") or hub_faustus_dir
        portfolio = raw.get("portfolio_dir")
        return cls(
            roots=_paths(raw.get("roots")), extra=_paths(raw.get("extra")),
            exclude=[str(e).strip() for e in (raw.get("exclude") or []) if str(e or "").strip()],
            faustus_dir=_paths(faustus)[0] if faustus else None,
            portfolio_dir=_paths(portfolio)[0] if portfolio else None,
            stray_prefixes=[str(p) for p in (raw.get("stray_prefixes") if "stray_prefixes" in raw else ["claude/"]) or []],
            default_branches=[str(b) for b in (raw.get("default_branches") or ["main", "master"])],
            ci=bool(raw.get("ci", True)),
            expected_emails=[str(e).lower() for e in (raw.get("expected_emails") or DEFAULT_EMAILS)],
        )


# ---------------------------------------------------------------------------
# issues
# ---------------------------------------------------------------------------

def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def build_issues(r: dict[str, Any]) -> list[dict[str, Any]]:
    """The things worth a look in one scanned repository, most severe first. Each carries a ``text`` in
    Spanish and English and the ``kind`` rules and tools match on."""
    if r.get("error"):
        return [{"kind": "scan_error", "severity": "warn",
                 "text": {"es": f"no se pudo leer el repositorio: {r['error']}", "en": f"could not read the repository: {r['error']}"}}]
    out: list[dict[str, Any]] = []

    def add(kind: str, severity: str, es: str, en: str, **detail: Any) -> None:
        item: dict[str, Any] = {"kind": kind, "severity": severity, "text": {"es": es, "en": en}}
        if detail:
            item["detail"] = detail
        out.append(item)

    if r.get("operation"):
        op = r["operation"]
        add("in_progress", "error", f"operación a medias: {op}", f"operation in progress: {op}", operation=op)
    lock = r.get("lock")
    if lock and lock.get("stale"):
        mins = int(lock["age_s"] // 60)
        add("stale_lock", "error", f"index.lock huérfano desde hace {mins} min", f"stale index.lock, {mins} min old", age_s=lock["age_s"])
    if r.get("secrets"):
        names = ", ".join(r["secrets"][:5])
        add("tracked_secret", "error", f"ficheros que parecen secretos bajo git: {names}",
            f"tracked files that look like secrets: {names}", paths=r["secrets"])
    ci = r.get("ci") or {}
    if ci.get("state") == "failing":
        wf = ci.get("workflow") or "CI"
        add("ci_failing", "error", f"el último CI ({wf}) falla", f"the latest CI run ({wf}) is failing", url=ci.get("url"))
    n = int(r.get("unpushed") or 0)
    if r.get("never_pushed"):
        if r.get("no_remote"):
            add("never_pushed", "warn", "sin remoto configurado: nunca se ha hecho push", "no remote configured: never pushed")
        else:
            add("never_pushed", "warn", "nunca se ha hecho push (ninguna rama remota)", "never pushed (no remote branch)")
    elif n:
        add("unpushed", "warn", f"{n} {_plural(n, 'commit', 'commits')} sin push", f"{n} unpushed {_plural(n, 'commit', 'commits')}", count=n)
    if r.get("detached"):
        add("detached_head", "warn", "HEAD desacoplado (sin rama)", "detached HEAD")
    if r.get("stray_branches"):
        names = ", ".join(r["stray_branches"][:6])
        k = len(r["stray_branches"])
        add("stray_branch", "warn", f"{k} {_plural(k, 'rama suelta', 'ramas sueltas')}: {names}",
            f"{k} stray {_plural(k, 'branch', 'branches')}: {names}", branches=r["stray_branches"])
    drift_ = r.get("drift") or {}
    vend = drift_.get("vendored") or {}
    if vend.get("stale"):
        n_files = sum(c.get("count", 0) for c in vend.get("copies", []) if c.get("count"))
        add("vendored_drift", "warn", f"hoard_link vendorizado desfasado ({n_files} ficheros)",
            f"vendored hoard_link is out of date ({n_files} files)", copies=[c["path"] for c in vend["copies"] if c.get("count")])
    man = drift_.get("manifest") or {}
    if man.get("state") == "differs":
        add("manifest_drift", "warn", "faustus-plugin.json distinto del manifiesto que lleva Faustus",
            "faustus-plugin.json differs from the manifest Faustus carries", keys=man.get("keys"))
    elif man.get("state") == "invalid":
        add("manifest_drift", "warn", "faustus-plugin.json o su copia en Faustus no se puede leer",
            "faustus-plugin.json or its Faustus copy cannot be read")
    elif man.get("state") == "missing_in_faustus":
        add("manifest_missing", "info", "faustus-plugin.json sin copia en Faustus", "faustus-plugin.json has no copy in Faustus")
    if r.get("unexpected_authors"):
        who = ", ".join(r["unexpected_authors"][:3])
        add("unexpected_author", "info", f"commits sin push con autor inesperado: {who}",
            f"unpushed commits by an unexpected author: {who}", emails=r["unexpected_authors"])
    th = drift_.get("theme") or {}
    if th.get("stale"):
        add("theme_drift", "info", f"hoard-theme.css desfasado ({len(th['stale'])})", f"hoard-theme.css out of date ({len(th['stale'])})",
            copies=th["stale"])
    d = r.get("dirty") or {}
    if d.get("total"):
        add("dirty", "info",
            f"{d['total']} cambios sin commitear ({d['staged']} en stage, {d['modified']} modificados, {d['untracked']} sin seguimiento)",
            f"{d['total']} uncommitted changes ({d['staged']} staged, {d['modified']} modified, {d['untracked']} untracked)")
    if (r.get("behind") or 0) > 0:
        b = r["behind"]
        add("behind", "info", f"{b} {_plural(b, 'commit', 'commits')} por detrás del upstream", f"{b} {_plural(b, 'commit', 'commits')} behind upstream", count=b)
    files = r.get("files") or {}
    if not files.get("readme"):
        add("no_readme", "info", "sin README.md", "no README.md")
    if not files.get("readme_es"):
        add("no_readme_es", "info", "sin README.es.md", "no README.es.md")
    if not files.get("license"):
        add("no_license", "info", "sin LICENSE", "no LICENSE")
    if r.get("in_portfolio") is False:
        add("not_in_portfolio", "info", "no aparece en el portfolio", "not mentioned in the portfolio")
    out.sort(key=lambda i: -SEVERITY_ORDER[i["severity"]])
    return out


def worst_severity(issues: list[dict[str, Any]]) -> Optional[str]:
    return max((i["severity"] for i in issues), key=lambda s: SEVERITY_ORDER[s], default=None)


# ---------------------------------------------------------------------------
# the monitor
# ---------------------------------------------------------------------------

CiRunner = Callable[[list[str], float], "tuple[int, str, str]"]


def _default_ci_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)
    env = dict(os.environ)
    env.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL, **kwargs)
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def parse_ci(stdout: str, local_sha: str = "") -> dict[str, Any]:
    """``gh run list --json …`` (one run) → ``{state, …}`` with state failing|passing|running|none|unknown."""
    try:
        runs = json.loads(stdout or "[]")
    except ValueError:
        return {"state": "unknown", "error": "unreadable gh output"}
    if not isinstance(runs, list):
        return {"state": "unknown", "error": "unexpected gh output"}
    if not runs:
        return {"state": "none"}
    run = runs[0] if isinstance(runs[0], dict) else {}
    status, conclusion = str(run.get("status") or "").lower(), str(run.get("conclusion") or "").lower()
    if status == "completed":
        state = "passing" if conclusion in ("success", "neutral", "skipped") else (
            "failing" if conclusion in ("failure", "timed_out", "startup_failure", "action_required") else "unknown")
    elif status:
        state = "running"
    else:
        state = "unknown"
    sha = str(run.get("headSha") or "")
    return {"state": state, "status": status, "conclusion": conclusion, "sha": sha[:12], "url": run.get("url"),
            "created_at": run.get("createdAt"), "workflow": run.get("workflowName"),
            "head_matches": bool(local_sha and sha and sha.startswith(local_sha[:12]))}


class RepoMonitor:
    """Scans, caches and serves the state of the family's repositories. Built by the hub; touches no
    repository except through read-only git calls and the explicit :meth:`fetch`."""

    def __init__(self, data_dir: str, settings_fn: Callable[[], RepoSettings], apps_fn: Callable[[], list[Any]],
                 emit: Optional[Callable[[str, dict[str, Any]], Any]] = None, *, lang_fn: Optional[Callable[[], str]] = None,
                 src_pkg: Optional[str] = None, now: Callable[[], float] = time.time,
                 ci_runner: Optional[CiRunner] = None, which: Callable[[str], Optional[str]] = shutil.which,
                 git: Optional[str] = None):
        self.path = os.path.join(data_dir, "repos.json")
        self._settings_fn = settings_fn
        self._apps_fn = apps_fn
        self._emit_fn = emit
        self._lang_fn = lang_fn or (lambda: "es")
        self.src_pkg = Path(src_pkg) if src_pkg else Path(__file__).resolve().parents[1]
        self.src_js = self.src_pkg.parent / "js" / "hoard-link.js"
        self.src_js_commons = self.src_pkg.parent / "js" / drift.JS_COMMONS
        self.canon_theme = self.src_pkg / "ui" / drift.THEME_NAME
        self._now = now
        self._ci_runner = ci_runner or _default_ci_runner
        self._which = which
        self._git = git
        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self._ci_idle = threading.Event()
        self._ci_idle.set()
        self._snapshot: Optional[dict[str, Any]] = None
        self._ci_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.last_error: Optional[str] = None
        self._load()

    # -- persistence ------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        repos = raw.get("repos") if isinstance(raw, dict) else None
        if not isinstance(repos, list):
            return
        self._snapshot = {"generated_ts": float(raw.get("generated_ts") or 0), "scan_s": raw.get("scan_s"),
                          "repos": [r for r in repos if isinstance(r, dict) and r.get("name")], "roots": raw.get("roots") or []}
        for r in self._snapshot["repos"]:
            ci = r.get("ci") or {}
            slug = r.get("github")
            if slug and ci.get("checked_ts") and ci.get("state") != "unknown":
                self._ci_cache[slug] = (float(ci["checked_ts"]), ci)

    def _save(self) -> None:
        snap = self._snapshot
        if snap is None:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "generated_ts": snap["generated_ts"], "scan_s": snap.get("scan_s"),
                           "roots": snap.get("roots"), "repos": snap["repos"]}, fh, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- events -----------------------------------------------------------------------------
    def _emit(self, type: str, data: dict[str, Any]) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(type, data)
        except Exception:  # noqa: BLE001 - the bus is best effort
            pass

    def _issue_events(self, before: Optional[dict[str, set[str]]], repos: list[dict[str, Any]]) -> None:
        """``hub.repos.issue`` for every error-level issue a repo did not have before. With no previous
        snapshot (the very first scan) nothing is emitted: everything would be news."""
        if before is None:
            return
        lang = "en" if self._lang_fn() == "en" else "es"
        for r in repos:
            had = before.get(r["name"], set())
            for issue in r.get("issues") or []:
                if issue["severity"] == "error" and issue["kind"] not in had:
                    self._emit("hub.repos.issue", {"repo": r["name"], "kind": issue["kind"], "severity": "error",
                                                   "text": issue["text"][lang], "url": self._github_url(r) or ""})

    @staticmethod
    def _github_url(r: dict[str, Any]) -> Optional[str]:
        return f"https://github.com/{r['github']}" if r.get("github") else None

    @staticmethod
    def _error_kinds(snapshot: Optional[dict[str, Any]]) -> Optional[dict[str, set[str]]]:
        if snapshot is None:
            return None
        return {r["name"]: {i["kind"] for i in r.get("issues", []) if i["severity"] == "error"} for r in snapshot["repos"]}

    # -- discovery --------------------------------------------------------------------------
    def discover(self, cfg: Optional[RepoSettings] = None) -> list[dict[str, Any]]:
        """``[{name, path, app}]`` for every repository to scan, sorted by name."""
        cfg = cfg or self._settings_fn()
        norm = lambda p: os.path.normcase(os.path.realpath(p))  # noqa: E731
        apps = list(self._apps_fn() or [])
        app_by_path = {norm(a.folder): a.id for a in apps}
        roots: list[str] = list(cfg.roots)
        for a in apps:
            roots.append(os.path.dirname(os.path.abspath(a.folder)))
        found: dict[str, str] = {}   # normalised path -> path

        def consider(path: str) -> None:
            if os.path.exists(os.path.join(path, ".git")):
                found.setdefault(norm(path), os.path.abspath(path))

        seen_roots: set[str] = set()
        for root in roots:
            key = norm(root)
            if key in seen_roots or not os.path.isdir(root):
                continue
            seen_roots.add(key)
            consider(root)
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue
            for entry in entries:
                child = os.path.join(root, entry)
                if not entry.startswith(".") and os.path.isdir(child):
                    consider(child)
        for a in apps:
            consider(a.folder)
        for p in cfg.extra:
            consider(p)
        if cfg.faustus_dir:
            consider(cfg.faustus_dir)
        excluded_names = {e.casefold() for e in cfg.exclude}
        excluded_paths = {norm(os.path.abspath(os.path.expanduser(e))) for e in cfg.exclude if os.sep in e or "/" in e or ":" in e}
        out: list[dict[str, Any]] = []
        for key, path in sorted(found.items(), key=lambda kv: os.path.basename(kv[1]).casefold()):
            name = os.path.basename(path.rstrip("/\\")) or path
            if name.casefold() in excluded_names or key in excluded_paths:
                continue
            out.append({"name": name, "path": path, "app": app_by_path.get(key)})
        counts: dict[str, int] = {}
        for item in out:
            counts[item["name"].casefold()] = counts.get(item["name"].casefold(), 0) + 1
        for item in out:
            if counts[item["name"].casefold()] > 1:
                item["name"] = f"{item['name']} ({os.path.basename(os.path.dirname(item['path']))})"
        return out

    # -- scanning one repository --------------------------------------------------------------
    def _g(self, repo: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> GitResult:
        return run_git(repo, *args, timeout=timeout, git=self._git)

    def scan_repo(self, path: str, name: Optional[str] = None, app: Optional[str] = None,
                  cfg: Optional[RepoSettings] = None) -> dict[str, Any]:
        cfg = cfg or self._settings_fn()
        t0 = time.monotonic()
        rec: dict[str, Any] = {"name": name or os.path.basename(path.rstrip("/\\")), "path": path, "app": app,
                               "error": None, "scanned_ts": self._now()}
        g = lambda *a, **k: self._g(path, *a, **k)  # noqa: E731
        top = g("rev-parse", "--absolute-git-dir")
        if not top.ok:
            rec["error"] = (top.err.strip() or "git failed").splitlines()[0][:200]
            rec["issues"] = build_issues(rec)
            rec["scan_ms"] = int((time.monotonic() - t0) * 1000)
            return rec
        gitdir = top.out.strip()
        rec["git_dir"] = gitdir

        # HEAD
        sym = g("symbolic-ref", "-q", "--short", "HEAD")
        rec["branch"] = sym.out.strip() if sym.ok and sym.out.strip() else None
        rec["detached"] = rec["branch"] is None
        head = g("log", "-1", "--format=%H%x1f%cI%x1f%s%x1f%an%x1f%ae")
        if head.ok and head.out.strip():
            sha, date, subject, author, email = (head.out.rstrip("\n").split("\x1f") + [""] * 5)[:5]
            rec["head"] = {"sha": sha, "short": sha[:8], "date": date, "subject": subject, "author": author, "email": email}
            rec["empty"] = False
        else:
            rec["head"] = None
            rec["empty"] = True
            rec["detached"] = False  # an unborn branch is not a detached HEAD

        # remotes
        remotes: list[dict[str, str]] = []
        rv = g("remote", "-v")
        if rv.ok:
            seen: set[str] = set()
            for line in rv.out.splitlines():
                m = re.match(r"^(\S+)\t(.+?) \((fetch|push)\)$", line.strip("\r"))
                if m and m.group(1) not in seen and m.group(3) == "fetch":
                    seen.add(m.group(1))
                    remotes.append({"name": m.group(1), "url": sanitize_url(m.group(2))})
        rec["remotes"] = remotes
        rec["no_remote"] = not remotes
        gh = None
        for rm in sorted(remotes, key=lambda r: r["name"] != "origin"):
            gh = parse_github(rm["url"])
            if gh:
                break
        rec["github"] = gh

        # upstream, ahead/behind, unpushed
        rec["upstream"], rec["ahead"], rec["behind"] = None, None, None
        rec["unpushed"], rec["never_pushed"], rec["unpushed_authors"], rec["unexpected_authors"] = 0, False, [], []
        unpushed_shas: set[str] = set()
        if not rec["empty"]:
            up = g("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if up.ok and up.out.strip():
                rec["upstream"] = up.out.strip()
                ab = g("rev-list", "--left-right", "--count", "HEAD...@{u}")
                if ab.ok:
                    try:
                        a_, b_ = ab.out.split()
                        rec["ahead"], rec["behind"] = int(a_), int(b_)
                    except ValueError:
                        pass
            refs = g("for-each-ref", "--format=%(refname)", "refs/remotes")
            remote_branches = [l for l in refs.out.splitlines() if l and not l.endswith("/HEAD")] if refs.ok else None
            cnt = g("rev-list", "--count", "HEAD", "--not", "--remotes")
            if cnt.ok:
                try:
                    rec["unpushed"] = int(cnt.out.strip() or 0)
                except ValueError:
                    pass
            rec["never_pushed"] = remote_branches is not None and not remote_branches
            if rec["unpushed"]:
                lg = g("log", "--max-count=2000", "--format=%H%x1f%ae", "HEAD", "--not", "--remotes")
                emails: dict[str, int] = {}
                for line in lg.out.splitlines() if lg.ok else []:
                    sha, _, em = line.partition("\x1f")
                    unpushed_shas.add(sha)
                    emails[em.lower()] = emails.get(em.lower(), 0) + 1
                rec["unpushed_authors"] = [{"email": e, "count": c} for e, c in sorted(emails.items(), key=lambda kv: -kv[1])]
                expected = set(cfg.expected_emails)
                rec["unexpected_authors"] = [e for e in emails if e not in expected]
        rec["no_upstream"] = rec["upstream"] is None and not rec["detached"] and not rec["empty"]

        # working tree
        st = g("status", "--porcelain=v1", "-z", "--untracked-files=normal")
        rec["dirty"] = parse_status(st.out) if st.ok else {"staged": 0, "modified": 0, "untracked": 0, "conflicted": 0, "total": 0,
                                                           "paths": [], "truncated": False, "error": st.err.strip()[:120]}
        sl = g("stash", "list")
        rec["stash"] = len([l for l in sl.out.splitlines() if l.strip()]) if sl.ok else 0

        # in-progress operation, stale lock
        op = None
        for marker, label in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
                              ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
            if os.path.exists(os.path.join(gitdir, marker)):
                op = label
                break
        rec["operation"] = op
        lock = None
        lock_path = os.path.join(gitdir, "index.lock")
        try:
            age = max(0.0, self._now() - os.path.getmtime(lock_path))
            lock = {"path": lock_path, "age_s": int(age), "stale": age > STALE_LOCK_S}
        except OSError:
            pass
        rec["lock"] = lock

        # branches
        branches: list[dict[str, Any]] = []
        default = None
        bl = g("for-each-ref", "--format=%(refname:short)%09%(HEAD)%09%(upstream:short)%09%(committerdate:iso-strict)", "refs/heads")
        names: list[str] = []
        if bl.ok:
            for line in bl.out.splitlines():
                f = (line.split("\t") + ["", "", "", ""])[:4]
                if f[0]:
                    names.append(f[0])
                    branches.append({"name": f[0], "current": f[1] == "*", "upstream": f[2] or None, "date": f[3]})
        for cand in cfg.default_branches:
            if cand in names:
                default = cand
                break
        merged: set[str] = set()
        if default:
            mg = g("for-each-ref", f"--merged={default}", "--format=%(refname:short)", "refs/heads")
            if mg.ok:
                merged = {l.strip() for l in mg.out.splitlines() if l.strip()}
        stray: list[str] = []
        for b in branches:
            b["default"] = b["name"] == default
            b["stray"] = None
            if b["name"] == default:
                continue
            if any(b["name"].startswith(p) for p in cfg.stray_prefixes if p):
                b["stray"] = "prefix"
            elif default and b["name"] in merged:
                b["stray"] = "merged"
            if b["stray"]:
                stray.append(b["name"])
        rec["default_branch"], rec["branches"], rec["stray_branches"] = default, branches, stray

        # last commits
        commits: list[dict[str, Any]] = []
        if not rec["empty"]:
            cl = g("log", f"-{MAX_COMMITS}", "--format=%H%x1f%cI%x1f%an%x1f%ae%x1f%s")
            for line in cl.out.splitlines() if cl.ok else []:
                sha, date, an, ae, subj = (line.split("\x1f") + [""] * 5)[:5]
                commits.append({"sha": sha, "short": sha[:8], "date": date, "author": an, "email": ae, "subject": subj,
                                "pushed": sha not in unpushed_shas})
        rec["commits"] = commits

        # files, secrets
        def has(*rel: str) -> bool:
            return any(os.path.isfile(os.path.join(path, r)) for r in rel)

        wf_dir = os.path.join(path, ".github", "workflows")
        try:
            workflows = len([n for n in os.listdir(wf_dir) if fnmatch.fnmatch(n.lower(), "*.y*ml")])
        except OSError:
            workflows = 0
        rec["files"] = {"readme": has("README.md"), "readme_es": has("README.es.md"),
                        "license": has("LICENSE", "LICENSE.md", "LICENSE.txt"), "workflows": workflows,
                        "manifest": has("faustus-plugin.json"), "icon": has("app-icon.png")}
        ls = g("ls-files", "-z")
        rec["secrets"] = [p for p in ls.out.split("\0") if p and _secret_like(p)][:20] if ls.ok else []

        # drift
        rec["drift"] = self._drift(path, cfg)
        rec["ci"] = rec.get("ci") or {"state": "unknown"}
        rec["in_portfolio"] = None
        rec["issues"] = build_issues(rec)       # without CI and portfolio, which the refresh adds and then recomputes
        rec["scan_ms"] = int((time.monotonic() - t0) * 1000)
        return rec

    def _drift(self, path: str, cfg: RepoSettings) -> dict[str, Any]:
        copies = []
        for c in drift.find_vendored(path, self.src_pkg):
            changed, removed = drift.plan_tree(self.src_pkg, c)
            stale = changed + removed
            copies.append({"path": Path(os.path.relpath(c, path)).as_posix(), "kind": "python", "count": len(stale),
                           "stale": stale[:20]})
        js = drift.vendored_js_stale(path, self.src_js)
        if js is not None:
            copies.append({"path": "server/hoard-link.js", "kind": "js", "count": 1 if js else 0, "stale": ["hoard-link.js"] if js else []})
        commons = drift.vendored_js_commons_plan(path, self.src_js_commons)
        if commons is not None:
            stale = commons[0] + commons[1]
            copies.append({"path": "server/" + drift.JS_COMMONS, "kind": "js", "count": len(stale), "stale": stale[:20]})
        theme_copies = list(drift.theme_copies_in(path))
        theme = [Path(os.path.relpath(p, path)).as_posix() for p in drift.theme_stale(theme_copies, self.canon_theme)]
        return {"vendored": {"copies": copies, "stale": any(c["count"] for c in copies)},
                "theme": {"copies": len(theme_copies), "stale": theme},
                "manifest": self._manifest_drift(path, cfg)}

    @staticmethod
    def _manifest_drift(path: str, cfg: RepoSettings) -> dict[str, Any]:
        mine = os.path.join(path, "faustus-plugin.json")
        if not os.path.isfile(mine):
            return {"state": "n/a"}
        if not cfg.faustus_dir or not os.path.isdir(os.path.join(cfg.faustus_dir, "plugins")):
            return {"state": "unchecked"}
        try:
            with open(mine, "r", encoding="utf-8-sig") as fh:
                mine_json = json.load(fh)
        except (OSError, ValueError):
            return {"state": "invalid"}
        app_id = str(mine_json.get("id") or "").strip() if isinstance(mine_json, dict) else ""
        if not app_id:
            return {"state": "invalid"}
        theirs = os.path.join(cfg.faustus_dir, "plugins", app_id, "plugin.json")
        if not os.path.isfile(theirs):
            return {"state": "missing_in_faustus", "id": app_id, "path": theirs}
        try:
            with open(theirs, "r", encoding="utf-8-sig") as fh:
                theirs_json = json.load(fh)
        except (OSError, ValueError):
            return {"state": "invalid", "id": app_id, "path": theirs}
        if theirs_json == mine_json:
            return {"state": "ok", "id": app_id, "path": theirs}
        keys: list[str] = []
        if isinstance(mine_json, dict) and isinstance(theirs_json, dict):
            keys = sorted(k for k in set(mine_json) | set(theirs_json) if mine_json.get(k) != theirs_json.get(k))
        return {"state": "differs", "id": app_id, "path": theirs, "keys": keys[:20]}

    # -- portfolio ----------------------------------------------------------------------------
    @staticmethod
    def _portfolio_text(portfolio_dir: str) -> Optional[str]:
        src = os.path.join(portfolio_dir, "src")
        if not os.path.isdir(src):
            return None
        exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".md", ".mdx", ".html", ".yml", ".yaml", ".txt", ".css"}
        chunks: list[str] = []
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", "dist", "build")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in exts:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > 2_000_000:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        chunks.append(fh.read().lower())
                except OSError:
                    continue
        return "\n".join(chunks)

    @staticmethod
    def _in_portfolio(rec: dict[str, Any], text: str) -> bool:
        needles = {rec["name"].lower(), rec["name"].lower().replace(" ", "-")}
        if rec.get("github"):
            needles.add(rec["github"].lower())
            needles.add(rec["github"].split("/", 1)[1].lower())
        return any(n and (len(n) >= 3 or "/" in n) and n in text for n in needles)

    # -- CI -----------------------------------------------------------------------------------
    def _ci_wanted(self, cfg: RepoSettings) -> bool:
        return cfg.ci and bool(self._which("gh"))

    def _apply_ci_cache(self, rec: dict[str, Any], wanted: bool) -> bool:
        """Fill ``rec['ci']`` from the cache; True when a fresh lookup is still needed (``wanted``: CI is on and
        ``gh`` is available)."""
        slug = rec.get("github")
        hit = self._ci_cache.get(slug) if slug else None
        if hit:
            rec["ci"] = dict(hit[1])
            return wanted and self._now() - hit[0] > CI_CACHE_S
        rec["ci"] = {"state": "unknown", "pending": True} if (slug and wanted) else {"state": "unknown"}
        return bool(slug and wanted)

    def _ci_lookup(self, slug: str, local_sha: str) -> dict[str, Any]:
        rc, out, err = self._ci_runner(["gh", "run", "list", "-R", slug, "--limit", "1", "--json",
                                        "status,conclusion,headSha,url,createdAt,workflowName"], CI_TIMEOUT_S)
        if rc != 0:
            return {"state": "unknown", "error": (err or "gh failed").strip().splitlines()[0][:160] if (err or "").strip() else "gh failed"}
        return parse_ci(out, local_sha)

    def _ci_fill(self, todo: list[tuple[str, str]]) -> None:
        """Look up CI for ``[(repo name, github slug)]`` and update the live snapshot as results arrive."""
        try:
            def one(item: tuple[str, str]) -> tuple[str, str, dict[str, Any]]:
                name, slug = item
                with self._lock:
                    rec = next((r for r in (self._snapshot or {}).get("repos", []) if r["name"] == name), None)
                    sha = ((rec or {}).get("head") or {}).get("sha", "")
                ci = self._ci_lookup(slug, sha)
                ci["checked_ts"] = self._now()
                return name, slug, ci

            with ThreadPoolExecutor(max_workers=CI_WORKERS) as pool:
                for name, slug, ci in pool.map(one, todo):
                    with self._lock:
                        if ci.get("state") != "unknown":
                            self._ci_cache[slug] = (ci["checked_ts"], ci)
                        snap = self._snapshot
                        rec = next((r for r in (snap or {}).get("repos", []) if r["name"] == name), None)
                        if rec is None:
                            continue
                        before = {i["kind"] for i in rec.get("issues", []) if i["severity"] == "error"}
                        rec["ci"] = ci
                        rec["issues"] = build_issues(rec)
                        snap["ci_updated_ts"] = self._now()
                        self._save()
                    self._issue_events({name: before}, [rec])
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"ci: {type(exc).__name__}: {exc}"
        finally:
            self._ci_idle.set()

    def join_ci(self, timeout: float = 30.0) -> bool:
        """Wait for the background CI lookups (tests; nothing else needs to)."""
        return self._ci_idle.wait(timeout)

    # -- refresh --------------------------------------------------------------------------------
    @property
    def refreshing(self) -> bool:
        return not self._idle.is_set()

    def refresh(self, *, wait: bool = True, timeout: float = 300.0) -> dict[str, Any]:
        """Rescan every repository in the background (one refresh at a time). With ``wait`` it returns the new
        snapshot once the git phase is done; the CI status keeps arriving afterwards."""
        with self._lock:
            started = self._idle.is_set()
            if started:
                self._idle.clear()
                threading.Thread(target=self._run_refresh, name="hoard-hub-repos", daemon=True).start()
        if wait:
            self._idle.wait(timeout)
        return {"ok": True, "started": started, "refreshing": self.refreshing}

    def _run_refresh(self) -> None:
        try:
            self._do_refresh()
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._idle.set()

    def _do_refresh(self) -> None:
        cfg = self._settings_fn()
        t0 = time.monotonic()
        items = self.discover(cfg)
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            records = list(pool.map(lambda it: self.scan_repo(it["path"], it["name"], it["app"], cfg), items))
        text = self._portfolio_text(cfg.portfolio_dir) if cfg.portfolio_dir else None
        ci_wanted = self._ci_wanted(cfg)
        portfolio_real = os.path.normcase(os.path.realpath(cfg.portfolio_dir)) if cfg.portfolio_dir else None
        need_ci: list[tuple[str, str]] = []
        with self._lock:
            for rec in records:
                if text is not None and not rec.get("error") and os.path.normcase(os.path.realpath(rec["path"])) != portfolio_real:
                    rec["in_portfolio"] = self._in_portfolio(rec, text)
                if not rec.get("error") and self._apply_ci_cache(rec, ci_wanted):
                    need_ci.append((rec["name"], rec["github"]))
                rec["issues"] = build_issues(rec)
            before = self._error_kinds(self._snapshot)
            now = self._now()
            self._snapshot = {"generated_ts": now, "scan_s": round(time.monotonic() - t0, 2), "repos": records,
                              "roots": sorted({os.path.dirname(it["path"]) for it in items})}
            self._save()
        summary = self._summary(records)
        self._emit("hub.repos.scan", {"repos": summary["repos"], "with_issues": summary["with_issues"],
                                      "unpushed_total": summary["unpushed_total"], "errors": summary["errors"],
                                      "scan_errors": summary["scan_errors"]})
        self._issue_events(before, records)
        if need_ci and self._ci_idle.is_set():
            self._ci_idle.clear()
            threading.Thread(target=self._ci_fill, args=(need_ci,), name="hoard-hub-repos-ci", daemon=True).start()

    # -- reading -------------------------------------------------------------------------------
    @staticmethod
    def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        ok = [r for r in records if not r.get("error")]
        return {
            "repos": len(records),
            "with_issues": sum(1 for r in records if r.get("issues")),
            "with_unpushed": sum(1 for r in ok if r.get("unpushed")),
            "unpushed_total": sum(int(r.get("unpushed") or 0) for r in ok),
            "never_pushed": sum(1 for r in ok if r.get("never_pushed")),
            "dirty": sum(1 for r in ok if (r.get("dirty") or {}).get("total")),
            "drift": sum(1 for r in ok if _has_drift(r)),
            "ci_failing": sum(1 for r in ok if (r.get("ci") or {}).get("state") == "failing"),
            "stray": sum(1 for r in ok if r.get("stray_branches")),
            "errors": sum(1 for r in records for i in r.get("issues", []) if i["severity"] == "error"),
            "scan_errors": sum(1 for r in records if r.get("error")),
        }

    @staticmethod
    def row(r: dict[str, Any]) -> dict[str, Any]:
        """The compact view of a record: everything a table needs, none of the lists."""
        d = r.get("dirty") or {}
        files = r.get("files") or {}
        dr = r.get("drift") or {}
        return {
            "name": r["name"], "path": r["path"], "app": r.get("app"), "error": r.get("error"),
            "branch": r.get("branch"), "detached": bool(r.get("detached")), "default_branch": r.get("default_branch"),
            "head": {k: (r.get("head") or {}).get(k) for k in ("short", "date", "subject")} if r.get("head") else None,
            "github": r.get("github"), "upstream": r.get("upstream"), "ahead": r.get("ahead"), "behind": r.get("behind"),
            "unpushed": r.get("unpushed", 0), "never_pushed": bool(r.get("never_pushed")), "no_remote": bool(r.get("no_remote")),
            "dirty": {k: d.get(k, 0) for k in ("staged", "modified", "untracked", "total")}, "stash": r.get("stash", 0),
            "operation": r.get("operation"), "stale_lock": bool((r.get("lock") or {}).get("stale")),
            "stray_branches": list(r.get("stray_branches") or []),
            "extra_branches": sum(1 for b in r.get("branches") or [] if not b.get("default")),
            "ci": {k: (r.get("ci") or {}).get(k) for k in ("state", "url", "workflow")},
            "drift": {"vendored": bool((dr.get("vendored") or {}).get("stale")), "theme": bool((dr.get("theme") or {}).get("stale")),
                      "manifest": (dr.get("manifest") or {}).get("state")},
            "docs": {"readme": bool(files.get("readme")), "readme_es": bool(files.get("readme_es")),
                     "license": bool(files.get("license")), "workflows": files.get("workflows", 0)},
            "in_portfolio": r.get("in_portfolio"),
            "issues": [{"kind": i["kind"], "severity": i["severity"], "text": i["text"]} for i in r.get("issues", [])],
            "severity": worst_severity(r.get("issues", [])),
            "scanned_ts": r.get("scanned_ts"),
        }

    def snapshot(self, *, block_first: bool = False, wait_s: float = 80.0) -> dict[str, Any]:
        """The cached snapshot, at once. Starts a background refresh when there is none or it is older than
        five minutes; ``block_first`` waits (up to ``wait_s``) for the very first scan: the agent tools do, a
        page does not."""
        with self._lock:
            snap = self._snapshot
        age = None if snap is None else max(0.0, self._now() - snap["generated_ts"])
        if snap is None:
            self.refresh(wait=block_first, timeout=wait_s)
            with self._lock:
                snap = self._snapshot
            age = None if snap is None else max(0.0, self._now() - snap["generated_ts"])
        elif age is not None and age > STALE_AFTER_S:
            self.refresh(wait=False)
        records = list((snap or {}).get("repos", []))
        return {"ok": True, "generated_ts": (snap or {}).get("generated_ts"), "age_s": None if age is None else int(age),
                "refreshing": self.refreshing, "ci_pending": not self._ci_idle.is_set(), "scan_s": (snap or {}).get("scan_s"),
                "roots": (snap or {}).get("roots", []), "summary": self._summary(records),
                "repos": [self.row(r) for r in records], "last_error": self.last_error}

    def find(self, name: str) -> tuple[Optional[dict[str, Any]], list[str]]:
        """A record by name: exact, then case-insensitive, then ignoring punctuation (``phileas`` finds
        ``Phileas's Hoard``), then the GitHub repo name, then a unique substring. Returns ``(record, [])`` or
        ``(None, candidates)`` when nothing or several match."""
        with self._lock:
            records = list((self._snapshot or {}).get("repos", []))
        want = str(name or "").strip()
        if not want:
            return None, []
        low = want.casefold()
        for test in (lambda r: r["name"] == want, lambda r: r["name"].casefold() == low, lambda r: r["path"] == want,
                     lambda r: _slug(r["name"]) == _slug(want),
                     lambda r: (r.get("github") or "").casefold() == low or (r.get("github") or "").split("/")[-1].casefold() == low):
            hit = [r for r in records if test(r)]
            if len(hit) == 1:
                return hit[0], []
            if len(hit) > 1:
                return None, [r["name"] for r in hit]
        w = _slug(want)
        hit = [r for r in records if w and w in _slug(r["name"])]
        if len(hit) == 1:
            return hit[0], []
        return None, [r["name"] for r in hit]

    def detail(self, name: str) -> dict[str, Any]:
        rec, candidates = self.find(name)
        if rec is None:
            return {"ok": False, "error": f"unknown repo: {name}" if not candidates else f"ambiguous repo: {name}",
                    "candidates": candidates or [r["name"] for r in (self._snapshot or {}).get("repos", [])][:60]}
        return {"ok": True, "repo": dict(rec), "row": self.row(rec), "github_url": self._github_url(rec),
                "age_s": int(max(0.0, self._now() - (self._snapshot or {}).get("generated_ts", 0)))}

    # -- actions --------------------------------------------------------------------------------
    def fetch(self, name: str) -> dict[str, Any]:
        """``git fetch --quiet --prune`` in one repository (network; explicit action only), then rescan it."""
        rec, candidates = self.find(name)
        if rec is None:
            return {"ok": False, "error": f"unknown repo: {name}", "candidates": candidates}
        if rec.get("no_remote"):
            return {"ok": False, "repo": rec["name"], "error": "no remote configured: nothing to fetch from", "row": self.row(rec)}
        t0 = time.monotonic()
        res = self._g(rec["path"], "fetch", "--quiet", "--prune", timeout=FETCH_TIMEOUT_S)
        err = (res.err.strip() or ("timed out" if res.timed_out else f"git exited {res.rc}"))[:300] if not res.ok else None
        cfg = self._settings_fn()
        fresh = self.scan_repo(rec["path"], rec["name"], rec.get("app"), cfg)
        with self._lock:
            snap = self._snapshot
            old = next((r for r in (snap or {}).get("repos", []) if r["name"] == rec["name"]), None)
            if old is not None:
                fresh["in_portfolio"] = old.get("in_portfolio")
                if not fresh.get("error"):
                    self._apply_ci_cache(fresh, False)
                fresh["issues"] = build_issues(fresh)
                before = {rec["name"]: {i["kind"] for i in old.get("issues", []) if i["severity"] == "error"}}
                snap["repos"] = [fresh if r["name"] == rec["name"] else r for r in snap["repos"]]
                self._save()
            else:
                before = None
                fresh["issues"] = build_issues(fresh)
        self._emit("hub.repos.fetch", {"repo": rec["name"], "ok": res.ok, "error": err, "ms": int((time.monotonic() - t0) * 1000)})
        self._issue_events(before, [fresh])
        out = {"ok": res.ok, "repo": rec["name"], "row": self.row(fresh)}
        if err:
            out["error"] = err
        return out

    def push_command(self, name: str) -> dict[str, Any]:
        """The exact command Luis would run to push this repository — text only, never executed."""
        rec, candidates = self.find(name)
        if rec is None:
            return {"ok": False, "error": f"unknown repo: {name}", "candidates": candidates}
        if rec.get("error"):
            return {"ok": False, "error": rec["error"]}
        path = rec["path"]
        remotes = rec.get("remotes") or []
        base = f'git -C "{path}"'
        res: dict[str, Any] = {"ok": True, "repo": rec["name"], "path": path, "branch": rec.get("branch"),
                               "unpushed": rec.get("unpushed", 0), "upstream": rec.get("upstream")}
        if rec.get("detached"):
            return {**res, "ok": False, "error": "HEAD is detached: check out a branch first (nothing was run)"}
        if not remotes:
            return {**res, "ok": False, "error": "no remote configured: git remote add origin <url> first (nothing was run)"}
        if rec.get("upstream"):
            res["command"] = f"{base} push"
        else:
            remote = next((r["name"] for r in remotes if r["name"] == "origin"), remotes[0]["name"])
            res["command"] = f'{base} push -u {remote} {rec.get("branch")}'
        res["note"] = "Not executed: the hub never pushes."
        self._emit("hub.repos.push_command", {"repo": rec["name"], "command": res["command"]})
        return res


def _has_drift(r: dict[str, Any]) -> bool:
    dr = r.get("drift") or {}
    return bool((dr.get("vendored") or {}).get("stale") or (dr.get("theme") or {}).get("stale")
                or (dr.get("manifest") or {}).get("state") in ("differs", "invalid", "missing_in_faustus"))


def filter_rows(rows: list[dict[str, Any]], mode: str = "all", text: str = "") -> list[dict[str, Any]]:
    """The agent tool's filters, also used by the tests: all|issues|unpushed|dirty|drift|ci_failing, plus text."""
    mode = (mode or "all").lower()
    tests: dict[str, Callable[[dict[str, Any]], bool]] = {
        "all": lambda r: True,
        "issues": lambda r: bool(r["issues"]),
        "unpushed": lambda r: bool(r["unpushed"]) or r["never_pushed"],
        "dirty": lambda r: bool(r["dirty"]["total"]),
        "drift": lambda r: bool(r["drift"]["vendored"] or r["drift"]["theme"]
                                or r["drift"]["manifest"] in ("differs", "invalid", "missing_in_faustus")),
        "ci_failing": lambda r: r["ci"]["state"] == "failing",
    }
    fn = tests.get(mode, tests["all"])
    needle = (text or "").strip().casefold()
    out = []
    for r in rows:
        if not fn(r):
            continue
        if needle:
            hay = " ".join([r["name"], r["path"], str(r.get("branch") or ""), str(r.get("github") or ""),
                            " ".join(i["kind"] for i in r["issues"])]).casefold()
            if needle not in hay:
                continue
        out.append(r)
    return out
