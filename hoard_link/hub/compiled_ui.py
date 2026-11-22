"""Read the compiled UI of a registered checkout and compare it with its source."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
from subprocess import TimeoutExpired
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import REPO_DIR
from .facets import Facet
from .integration_git import IntegrationError, ancestor, git, resolve_commit
from .repos import run_git


RECEIPT = "hoard-ui-build.json"
CONSOLE_INPUTS = (
    "apps/agora/src", "apps/agora/public", "apps/agora/index.html", "apps/agora/package.json",
    "apps/agora/package-lock.json", "apps/agora/vite.config.ts", "apps/agora/tsconfig.json",
    "hoard_link/hub/ui/production_decisions_card.js", "hoard_link/hub/ui/production_decisions_card.css",
    "hoard_link/hub/ui/production_decisions_card.d.ts", "hoard_link/hub/ui/production_decisions_data.js",
    "hoard_link/hub/ui/production_decisions_data.d.ts",
)
STUDIO_INPUTS = ("studio", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")
_VITE_NAMES = tuple(f"vite.config.{suffix}" for suffix in ("js", "mjs", "cjs", "ts", "mts", "cts"))
_SOURCE_INPUTS = ("src", "public", "app", "index.html", "package.json", "package-lock.json", "pnpm-lock.yaml",
                  "yarn.lock", "tsconfig.json", "postcss.config.js", "tailwind.config.js")
_REF = re.compile(r"[0-9a-fA-F]{7,64}")
_MAX_ASSET = 32 * 1024 * 1024

STATES = {
    "no_compiled_ui": {"es": "Sin interfaz compilada", "en": "No compiled interface"},
    "not_declared": {"es": "Interfaz compilada sin declarar", "en": "Compiled interface not declared"},
    "missing": {"es": "Falta la compilación", "en": "Build missing"},
    "behind": {"es": "Por detrás", "en": "Behind"},
    "current": {"es": "Al día", "en": "Up to date"},
    "unverified": {"es": "Sin verificar", "en": "Unverified"},
}
REASONS = {
    "dynamic_config": {"es": "La configuración no fija una salida que se pueda leer.", "en": "The configuration does not specify a readable output."},
    "no_checkout": {"es": "No se puede leer el historial de Git.", "en": "Git history is unavailable."},
    "no_source": {"es": "No hay commits de los archivos fuente declarados.", "en": "The declared sources have no commits."},
    "missing": {"es": "No se encuentra el archivo de entrada compilado.", "en": "The compiled entry file is missing."},
    "behind": {"es": "La compilación es anterior a cambios de interfaz.", "en": "Interface changes are newer than the build."},
    "dirty": {"es": "Hay cambios locales en el código fuente.", "en": "The interface source has local changes."},
    "unreachable": {"es": "La interfaz instalada no responde desde este Hub.", "en": "The installed interface is unreachable from this Hub."},
    "mismatch": {"es": "La respuesta de la instalación difiere de la compilación en disco.", "en": "The installed response differs from the build on disk."},
    "receipt": {"es": "El registro de compilación no coincide con esta salida o con el checkout.", "en": "The build receipt does not match this output or checkout."},
    "not_integrated": {"es": "La instalación no contiene los commits de interfaz de la entrega.", "en": "The installed checkout does not contain the delivery's interface commits."},
}


@dataclass(frozen=True)
class Build:
    output: str
    inputs: tuple[str, ...]
    entry: str = "index.html"
    route: str = "/"
    method: str = "manifest"
    asset_base: str = "/"


@dataclass(frozen=True)
class AppUI:
    id: str
    name: str
    root: Path | None
    urls: tuple[str, ...]
    declaration: object = None


def _within(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path outside the checkout")
    return path


def _vite_value(config: str, key: str, default: str) -> tuple[str, bool]:
    assignment = re.search(rf"(?:^|[{{,])\s*{key}\s*:\s*([^\r\n]+)", config, re.MULTILINE)
    if not assignment:
        return default, False
    expression = assignment[1]
    match = re.match(r"""(['"])([^'"]*)\1\s*(?=[,}]|$)""", expression)
    if match:
        return match[2], False
    match = re.match(r"""resolve\(\s*configRoot\s*,\s*(['"])([^'"]+)\1\s*\)\s*(?=[,}]|$)""",
                     expression)
    if match and "dirname(fileURLToPath(import.meta.url))" in config:
        return match[2], True
    raise ValueError("dynamic Vite setting")


def _detect(app: AppUI) -> Build | None:
    root = app.root
    if root is None or app.declaration is False:
        return None
    if isinstance(app.declaration, dict):
        data = app.declaration
        output, inputs = data["output"], data["sources"]
        entry, route = data.get("entry", "index.html"), data.get("route", "/")
        asset_base = data.get("asset_base", "/")
        if (not isinstance(inputs, list) or not inputs or
                not all(isinstance(p, str) and p for p in [output, entry, route, asset_base, *inputs]) or
                any(not path.startswith("/") or urlsplit(path).netloc for path in (route, asset_base)) or
                not asset_base.endswith("/")):
            raise ValueError("invalid declaration")
        _within(_within(root, output), entry)
        return Build(_within(root, output).relative_to(root).as_posix(),
                     tuple(_within(root, p).relative_to(root).as_posix() for p in inputs), entry, route,
                     asset_base=asset_base)
    if app.id == "hoardhub" and (root / "apps/agora/vite.config.ts").is_file():
        return Build("apps/agora/dist", CONSOLE_INPUTS, route="/agora/", method="hub_console", asset_base="/agora/")
    if app.id == "faustus" and (root / "vite.config.ts").is_file():
        config = (root / "vite.config.ts").read_text(encoding="utf-8")
        output, based_at_config = _vite_value(config, "outDir", "dist")
        if based_at_config and output == "static/studio" and "entryFileNames: 'studio.js'" in config:
            return Build(output, STUDIO_INPUTS, "studio.js", "/static/studio/studio.js", "faustus_studio")
    for folder in ("", "client", "frontend", "web", "ui", "apps/web"):
        for name in _VITE_NAMES:
            config_path = root / folder / name
            if not config_path.is_file():
                continue
            config = config_path.read_text(encoding="utf-8-sig")
            source, _ = _vite_value(config, "root", ".")
            source = _within(root, str(Path(folder) / source))
            output, based_at_config = _vite_value(config, "outDir", "dist")
            dest = _within(root, str((root / folder if based_at_config else source) / output))
            def relative(path):
                return path.relative_to(root).as_posix()
            inputs = [relative(config_path)]
            if source != root:
                inputs.append(relative(source))
            for base in {root, root / folder, source}:
                inputs.extend(relative(base / p) for p in _SOURCE_INPUTS if not (base / p).is_relative_to(dest))
            try:
                base, _ = _vite_value(config, "base", "/")
            except ValueError:
                base = "/"
            return Build(relative(dest), tuple(sorted(set(inputs))), method=relative(config_path),
                         asset_base=base if base.startswith("/") and base.endswith("/") and not urlsplit(base).netloc else "/")
    return None


class _EntryLinks(HTMLParser):
    def __init__(self, body: bytes):
        super().__init__()
        self.assets = set()
        self.feed(body.decode("utf-8", "replace"))

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "script" and attr.get("src"):
            self.assets.add(attr["src"])
        if tag == "link" and ("stylesheet" in (attr.get("rel") or "").split() or
                              "modulepreload" in (attr.get("rel") or "").split()) and attr.get("href"):
            self.assets.add(attr["href"])


def _entry_assets(build: Build, body: bytes) -> dict[str, str]:
    if not build.entry.lower().endswith(".html"):
        return {}
    paths = {}
    prefix = build.asset_base
    for ref in _EntryLinks(body).assets:
        parts = urlsplit(ref)
        if parts.netloc or parts.scheme or not parts.path or parts.path.startswith("/ui/browser-session.js"):
            continue
        path = unquote(parts.path)
        if path.startswith(prefix):
            relative, served_path = path[len(prefix):], parts.path
        elif build.method == "hub_console" and path.startswith("/assets/"):
            relative, served_path = path[1:], prefix + parts.path[1:]
        elif not path.startswith("/"):
            relative, served_path = path.removeprefix("./"), urljoin(build.route, parts.path)
        else:
            continue
        paths[relative] = served_path + (f"?{parts.query}" if parts.query else "")
    return paths


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read_url(base: str, route: str) -> bytes:
    parts = urlsplit(base)
    if urlsplit(route).netloc or urlsplit(route).scheme:
        raise OSError("asset outside the runtime")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
        raise OSError("not an HTTP runtime URL")
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    request = Request(urljoin(base, route), headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    with opener.open(request, timeout=1.2) as response:
        content = response.read(_MAX_ASSET + 1)
    if len(content) > _MAX_ASSET:
        raise OSError("asset exceeds the read limit")
    return content


def _runtime(app: AppUI, build: Build, body: bytes, assets: dict[str, str],
             contents: dict[str, bytes]) -> tuple[str, str | None]:
    state, served_hash = "unreachable", None
    for url in app.urls:
        try:
            served = _read_url(url, build.route)
            served_hash = sha256(served).hexdigest()
            if build.method == "hub_console":
                matched = bool(assets) and _entry_assets(build, served) == assets
            else:
                matched = served == body
            if matched and all(_read_url(url, route) == contents[path] for path, route in assets.items()):
                return "matched", served_hash
            state = "mismatch"
        except (OSError, ValueError):
            if served_hash is not None:
                state = "mismatch"
    return state, served_hash


def _row(app: AppUI, build: Build | None = None) -> dict:
    return {"app": app.id, "name": app.name, "output": build.output if build else None,
            "detection": build.method if build else None, "source_commit": None, "source_at": None,
            "built_at": None, "fingerprint": None, "behind_by": None, "behind_since": None,
            "served": "unverified", "served_entry_sha256": None, "build_commit": None, "basis": None}


def _state(row: dict, state: str, reason: str | None = None) -> dict:
    return {**row, "state": state, "label": STATES[state], "reason": reason,
            "detail": REASONS.get(reason)}


def _history(root: Path, build: Build, revision="HEAD", *, first=True) -> list[tuple[str, int]]:
    args = ("-1",) if first else ()
    result = git(root, "log", *args, "--format=%H %ct", revision, "--", *build.inputs)
    return [(sha, int(date)) for sha, date in (line.split() for line in result.stdout.decode("ascii").splitlines())]


def _ignored(root: Path, build: Build) -> bool:
    code = run_git(str(root), "check-ignore", "-q", "--", f"{build.output}/{build.entry}").rc
    if code not in (0, 1):
        raise IntegrationError("Cannot determine whether Git ignores the build")
    return code == 0


def _source_coverage(root: Path, build: Build, folder: Path, body: bytes, built_at: float, row: dict) -> list:
    receipt = folder / RECEIPT
    if receipt.is_file():
        record = json.loads(receipt.read_text(encoding="utf-8"))
        commit = record["source_commit"]
        if (not isinstance(commit, str) or not _REF.fullmatch(commit) or
                record["entry_sha256"] != sha256(body).hexdigest() or not ancestor(root, commit, "HEAD")):
            raise ValueError("build receipt does not match")
        receipt_date = record.get("built_at")
        if receipt_date is not None:
            if not isinstance(receipt_date, (int, float)) or not math.isfinite(receipt_date):
                raise ValueError("invalid build date")
            row["built_at"] = receipt_date
        row.update(build_commit=commit, basis="build_receipt")
        return _history(root, build, f"{commit}..HEAD", first=False)
    row["basis"] = "file_date"
    # Infer the installed branch at the build date. A later merge can introduce
    # source commits whose own timestamps are older than the files on disk.
    previous = git(root, "log", "--first-parent", "-1", f"--before=@{int(built_at)}", "--format=%H")
    commit = previous.stdout.decode("ascii").strip()
    return _history(root, build, f"{commit}..HEAD" if commit else "HEAD", first=False)


def _read_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(_MAX_ASSET + 1)
    if len(content) > _MAX_ASSET:
        raise OSError("asset exceeds the read limit")
    return content


def _inspect(app: AppUI, build: Build) -> dict:
    root = app.root
    row = _row(app, build)
    try:
        if not _ignored(root, build):
            return _state(row, "no_compiled_ui")
        history = _history(root, build)
        if not history:
            return _state(row, "unverified", "no_source")
        row["source_commit"], row["source_at"] = history[0]
    except (OSError, IntegrationError, TimeoutExpired):
        return _state(row, "unverified", "no_checkout")

    try:
        folder = _within(root, build.output)
        entry = _within(folder, build.entry)
        before = entry.stat()
        body = _read_file(entry)
        assets = _entry_assets(build, body)
        contents = {name: _read_file(_within(folder, name)) for name in sorted(assets)}
        after = entry.stat()
        if (after.st_ino, after.st_mtime_ns, after.st_size) != (before.st_ino, before.st_mtime_ns, before.st_size):
            return _state(row, "unverified", "mismatch")
    except (OSError, ValueError):
        return _state(row, "missing", "missing")

    # Include names and bytes so that a stable HTML entry with replaced JS is detectable.
    fingerprint = sha256()
    for name, content in [(build.entry, body), *contents.items()]:
        fingerprint.update(name.encode("utf-8") + b"\0" + sha256(content).digest())
    row.update(built_at=before.st_mtime, fingerprint=fingerprint.hexdigest())
    try:
        pending = _source_coverage(root, build, folder, body, before.st_mtime, row)
    except (OSError, IntegrationError, TypeError, ValueError, KeyError, TimeoutExpired):
        return _state(row, "unverified", "receipt")
    row.update(behind_by=len(pending), behind_since=min((date for _, date in pending), default=None))
    row["served"], row["served_entry_sha256"] = _runtime(app, build, body, assets, contents)
    if pending:
        return _state(row, "behind", "behind")
    if row["served"] != "matched":
        return _state(row, "unverified", row["served"])
    try:
        dirty = git(root, "diff", "--quiet", "--no-ext-diff", "HEAD", "--", *build.inputs, check=False)
    except (OSError, TimeoutExpired):
        return _state(row, "unverified", "no_checkout")
    if dirty.returncode:
        return _state(row, "unverified", "dirty" if dirty.returncode == 1 else "no_checkout")
    return _state(row, "current")


def _status(app: AppUI) -> dict:
    try:
        build = _detect(app)
    except (OSError, TypeError, KeyError, ValueError):
        return _state(_row(app), "unverified", "dynamic_config")
    if build:
        return _inspect(app, build)
    return _state(_row(app), "no_compiled_ui" if app.declaration is False else "not_declared")


def _changes(root: Path, ref: str, inputs: tuple[str, ...]) -> bool:
    if not _REF.fullmatch(ref):
        return False
    res = git(root, "diff-tree", "--root", "--first-parent", "-m", "-r", "--no-commit-id", "--name-only",
              f"{ref}^{{commit}}", "--", *inputs, check=False)
    return not res.returncode and bool(res.stdout)


class CompiledUIFacet(Facet):
    id = "compiled_ui"
    ui_scripts = ("compiled_ui.js",)

    def apps(self) -> list[AppUI]:
        apps = {
            app.id: AppUI(app.id, app.name, Path(app.folder).resolve(), (app.url,), app.family.get("compiled_ui"))
            for app in self.hub.apps
        }
        cfg = self.hub.config
        apps["hoardhub"] = AppUI("hoardhub", "Hoard Hub", REPO_DIR, (cfg.url,))
        old = apps.get("faustus")
        repos = cfg.repos if isinstance(cfg.repos, dict) else {}
        faustus_dir = cfg.faustus_dir or (old.root if old else None) or repos.get("faustus_dir")
        if not faustus_dir and not cfg.isolated:
            from ..fam_mail import faustus_dir as discover_faustus

            faustus_dir = discover_faustus(ask_hub=False)
        apps["faustus"] = AppUI("faustus", old.name if old else "Faustus",
                                Path(faustus_dir).resolve() if faustus_dir else None,
                                tuple(dict.fromkeys((*(old.urls if old else ()), *cfg.faustus_urls))),
                                old.declaration if old else None)
        if not cfg.isolated:
            # Use existing placement URLs without refreshing placement or running a remote command.
            placement = self.hub.placement.state()
            if placement["location"] == "sparks":
                urls = placement.get("urls") or {}
                for key in ("faustus", "hermes", "prometheus"):
                    if key in apps:
                        app = apps[key]
                        url = urls.get(key)
                        apps[key] = AppUI(app.id, app.name, app.root, (url,) if url else (), app.declaration)
        return sorted(apps.values(), key=lambda app: app.name.casefold())

    def status(self, app=None) -> dict:
        apps = [row for row in self.apps() if not app or row.id == app]
        if not apps:
            return {"ok": False, "status": 404, "error": "Aplicación desconocida. / Unknown application."}
        if len(apps) == 1:
            return {"ok": True, "apps": [_status(apps[0])]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            return {"ok": True, "apps": list(pool.map(_status, apps))}

    def get(self, req):
        return self.status(req.q("app")) if req.path == "/api/compiled-ui" else None

    @classmethod
    def tools(cls):
        return [{
            "name": "hub_compiled_ui_status",
            "description": "Read installed UI builds and source freshness. / Consulta la vigencia de las interfaces compiladas.",
            "inputSchema": {"type": "object", "properties": {
                "app": {"type": "string", "description": "Optional app id. / ID de aplicación opcional."},
            }},
        }]

    def handlers(self):
        return {"hub_compiled_ui_status": lambda args: self.status(args.get("app"))}

    def require_current(self, agora, task: dict, commits: list[str], *, verified: bool = False) -> None:
        from .agora import AgoraError

        submitted = task.get("commits") or commits
        names = task.get("repositories") or [task.get("repo")]
        if not submitted or not agora.repo_lookup or not any(names):
            return
        roots = {Path(root).resolve(): name for name in names if name and (root := agora.repo_lookup(name))}
        for app in self.apps():
            if app.root is None:
                continue
            repo = roots.get(app.root)
            if repo is None:
                aliases = {app.id.casefold(), app.name.casefold(), app.root.name.casefold()}
                repo = next((name for name in names if name and name.casefold() in aliases), None)
            if repo is None:
                continue
            refs = (task.get("commits_by_repo") or {}).get(repo, submitted)
            try:
                build = _detect(app)
            except (OSError, TypeError, KeyError, ValueError):
                continue  # An unresolved build layout cannot identify the delivery's UI inputs.
            if not build:
                continue
            try:
                touched = {ref for ref in dict.fromkeys([*refs, *commits]) if _changes(app.root, ref, build.inputs)}
                delivered = [ref for ref in refs if ref in touched]
                integrated = [ref for ref in commits if ref in touched]
                if not (delivered or integrated) or not _ignored(app.root, build):
                    continue
                required = integrated if verified and integrated else list(dict.fromkeys([*delivered, *integrated]))
                status = _inspect(app, build)
                if status["state"] == "current":
                    revision = status["build_commit"] or "HEAD"
                    if all(ancestor(app.root, resolve_commit(app.root, ref), revision) for ref in required):
                        continue
                    status = _state(status, "unverified", "not_integrated")
            except (OSError, IntegrationError, TimeoutExpired, ValueError):
                status = _state(_row(app), "unverified", "no_checkout")
            if status["detail"] is None:
                status = _state(status, "unverified", "no_checkout")
            es = f"{app.name}. {status['detail']['es']} Recompila y recarga la interfaz en una ventana de mantenimiento."
            en = f"{app.name}. {status['detail']['en']} Rebuild and reload the interface in a maintenance window."
            raise AgoraError(f"{es} / {en}", 409, error_code="compiled_ui_not_current",
                             message_es=es, message_en=en, compiled_ui=status)
