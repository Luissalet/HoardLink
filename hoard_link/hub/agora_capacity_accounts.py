"""Read Claude and Cursor account quotas for the Agora, the way unified usage trackers do.

Unified trackers (CodexBar and its forks, ccusage-style monitors) read each provider's quota from the
same place the provider's own client does:

* **Claude** -- ``GET https://api.anthropic.com/api/oauth/usage`` with the OAuth access token Claude
  Code keeps in ``~/.claude/.credentials.json`` (``CLAUDE_CONFIG_DIR`` when set). It answers the
  session (5 h) and weekly windows as ``utilization`` percentages with ``resets_at``. The token
  needs the ``user:profile`` scope. This module only *reads* the file: it never refreshes or rewrites
  Claude Code's credentials. An expired token is reported as an unknown window that says why.
* **Cursor** -- ``GET https://cursor.com/api/usage-summary`` authenticated with the Cursor app's
  own session (``cursorAuth/accessToken`` in its ``state.vscdb``, opened read-only), sent as the
  ``WorkosCursorSessionToken`` cookie. The plan has two included quotas, the same two the Cursor
  dashboard shows: "Cursor models" (Auto, Composer, Grok) and "Other models" (named API models),
  plus on-demand spend when it is enabled. They reset at the end of the billing cycle.

Nothing here logs or returns a token, email or account id: identities are hashed in memory into the
opaque pool key the Agora requires, and the published DTO carries percentages, labels and resets only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .agora_capacity import _report_via_agora_cli

LOGGER = logging.getLogger(__name__)

CLAUDE_SOURCE = "claude-oauth:api/oauth/usage"
CURSOR_SOURCE = "cursor-app:api/usage-summary"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CURSOR_SUMMARY_URL = "https://cursor.com/api/usage-summary"
_HTTP_TIMEOUT_S = 15.0
_EXPIRY_MARGIN_S = 60.0

_REPORTER_LOCK = threading.Lock()
_REPORTER_THREAD: Optional[threading.Thread] = None
_REPORTER_STOP: Optional[threading.Event] = None


class AccountCapacityError(RuntimeError):
    """A sanitized failure code; ``label`` is the Spanish line shown in the quota bar."""

    def __init__(self, code: str, label: str = ""):
        self.code = code
        self.label = label or code
        super().__init__(code)


# --------------------------------------------------------------------------------------------- helpers
def _pool_id(provider: str, identity: Any) -> str:
    text = str(identity or "").strip().casefold()
    if not text:
        raise AccountCapacityError("account_identity_unavailable", "No se pudo identificar la cuenta")
    material = f"hoardlink:{provider}-account-pool:v1:{text}".encode("utf-8")
    return f"{provider}-" + hashlib.sha256(material).hexdigest()[:40]


def _iso(value: Any) -> Optional[str]:
    """ISO-8601 UTC from an ISO string or a unix timestamp (s or ms); None when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / (1000.0 if float(value) > 1e11 else 1.0)
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _future(reset_iso: Optional[str], now: float) -> bool:
    if not reset_iso:
        return True
    try:
        return datetime.fromisoformat(reset_iso).timestamp() > now
    except ValueError:
        return True


def _measured(window_id: str, label: str, used: Any, reset: Any, source: str, now: float) -> Optional[dict]:
    """A measured window from a used percentage, or None when the number is unusable."""
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    used = max(0.0, min(100.0, float(used)))
    reset_at = _iso(reset)
    if not _future(reset_at, now):
        reset_at = None
    return {"window_id": window_id, "label": label[:100], "measurement": "measured",
            "used_percent": round(used, 2), "remaining_percent": round(100.0 - used, 2),
            "reset_at": reset_at, "source": source}


def _unknown(window_id: str, label: str, source: str) -> dict:
    return {"window_id": window_id, "label": label[:100], "measurement": "unknown",
            "used_percent": None, "remaining_percent": None, "reset_at": None, "source": source}


def _http_json(url: str, headers: Mapping[str, str], *, opener: Callable[..., Any] = urllib.request.urlopen,
               timeout: float = _HTTP_TIMEOUT_S) -> Any:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AccountCapacityError("unauthorized") from None
        if exc.code == 429:
            raise AccountCapacityError("rate_limited") from None
        raise AccountCapacityError(f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AccountCapacityError("network_unavailable") from None
    except ValueError:
        raise AccountCapacityError("invalid_response") from None


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (only to read ``sub``/``exp`` of our own session)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------------------------- Claude
def claude_config_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    configured = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def read_claude_session(*, config_dir: Optional[Path] = None, home: Optional[Path] = None,
                        now: Optional[float] = None) -> dict[str, Any]:
    """Claude Code's OAuth token and account id, read-only. Raises when missing or expired."""
    now = time.time() if now is None else float(now)
    directory = config_dir or claude_config_dir()
    try:
        data = json.loads((directory / ".credentials.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AccountCapacityError("claude_not_logged_in",
                                   "Claude Code no ha iniciado sesión en este PC") from None
    except (OSError, ValueError):
        raise AccountCapacityError("claude_credentials_unreadable",
                                   "No se pudieron leer las credenciales de Claude Code") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise AccountCapacityError("claude_not_logged_in", "Claude Code no ha iniciado sesión en este PC")
    scopes = oauth.get("scopes") if isinstance(oauth.get("scopes"), list) else None
    if scopes is not None and "user:profile" not in scopes:
        raise AccountCapacityError("claude_scope_missing",
                                   "La sesión de Claude Code no permite leer la cuota (falta user:profile)")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        if float(expires) / 1000.0 - _EXPIRY_MARGIN_S <= now:
            raise AccountCapacityError("claude_token_expired",
                                       "Sesión de Claude Code caducada: ejecuta «claude /login» en el PC")
    identity = ""
    profile = (home or Path.home()) / ".claude.json"
    try:
        account = json.loads(profile.read_text(encoding="utf-8")).get("oauthAccount") or {}
        identity = account.get("accountUuid") or account.get("organizationUuid") or ""
    except (OSError, ValueError, AttributeError):
        identity = ""
    if not identity:
        # Stable enough for one machine and still opaque: the subscription tier plus the config dir.
        identity = f"{oauth.get('subscriptionType', '')}|{oauth.get('rateLimitTier', '')}|{directory}"
    return {"token": token, "identity": identity,
            "plan": str(oauth.get("subscriptionType") or ""), "tier": str(oauth.get("rateLimitTier") or "")}


_CLAUDE_FIELDS = (
    ("five_hour", "claude.session", "Sesión (5 h)"),
    ("seven_day", "claude.week", "Semana · todos los modelos"),
    ("seven_day_opus", "claude.week-opus", "Semana · Opus"),
    ("seven_day_sonnet", "claude.week-sonnet", "Semana · Sonnet"),
)


def _plan_label(plan: str, tier: str) -> str:
    tier = tier.lower()
    if "max_20x" in tier:
        return "Claude Max 20x"
    if "max_5x" in tier:
        return "Claude Max 5x"
    return f"Claude {plan.capitalize()}" if plan else "Cuenta de Claude"


def build_claude_report(usage: Mapping[str, Any], session: Mapping[str, Any], *,
                        now: Optional[float] = None) -> dict[str, Any]:
    """Agora DTO from ``/api/oauth/usage``: the flat windows and, when present, the ``limits`` array."""
    if not isinstance(usage, Mapping):
        raise AccountCapacityError("invalid_response", "Respuesta de cuota de Claude no válida")
    now = time.time() if now is None else float(now)
    windows: list[dict[str, Any]] = []
    for key, window_id, label in _CLAUDE_FIELDS:
        entry = usage.get(key)
        if isinstance(entry, Mapping):
            window = _measured(window_id, label, entry.get("utilization"), entry.get("resets_at"), CLAUDE_SOURCE, now)
            if window:
                windows.append(window)
    seen = {w["window_id"] for w in windows}
    limits = usage.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "")
            percent = item.get("percent", item.get("utilization"))
            if kind == "session":
                window_id, label = "claude.session", "Sesión (5 h)"
            elif kind == "weekly_all":
                window_id, label = "claude.week", "Semana · todos los modelos"
            elif kind == "weekly_scoped":
                scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
                model = scope.get("model") if isinstance(scope.get("model"), Mapping) else {}
                name = str(model.get("display_name") or model.get("id") or "modelo").strip()
                slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:24] or "model"
                window_id, label = f"claude.week-{slug}", f"Semana · {name}"
            else:
                continue
            if window_id in seen:
                continue
            window = _measured(window_id, label, percent, item.get("resets_at"), CLAUDE_SOURCE, now)
            if window:
                windows.append(window)
                seen.add(window_id)
    extra = usage.get("extra_usage")
    if isinstance(extra, Mapping) and extra.get("is_enabled"):
        window = _measured("claude.extra", "Uso extra del mes", extra.get("utilization"), None, CLAUDE_SOURCE, now)
        if window:
            windows.append(window)
    if not windows:
        raise AccountCapacityError("claude_usage_empty", "Claude no devolvió ventanas de cuota")
    return {"provider_id": "claude", "pool_id": _pool_id("claude", session.get("identity")),
            "pool_label": _plan_label(str(session.get("plan") or ""), str(session.get("tier") or "")),
            "source": CLAUDE_SOURCE, "windows": windows}


def collect_claude_usage_report(*, config_dir: Optional[Path] = None, home: Optional[Path] = None,
                                now: Optional[float] = None,
                                opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    session = read_claude_session(config_dir=config_dir, home=home, now=now)
    try:
        usage = _http_json(CLAUDE_USAGE_URL, {
            "Authorization": f"Bearer {session['token']}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "hoardlink-agora-capacity/0.1",
        }, opener=opener)
    except AccountCapacityError as exc:
        if exc.code == "unauthorized":
            raise AccountCapacityError("claude_token_expired",
                                       "Sesión de Claude Code caducada: ejecuta «claude /login» en el PC") from None
        raise AccountCapacityError(exc.code, "No se pudo leer la cuota de Claude ahora") from None
    return build_claude_report(usage, session, now=now)


def claude_unavailable_report(error: AccountCapacityError, *, config_dir: Optional[Path] = None,
                              home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """An honest 'unknown' window saying why, when the account can still be told apart."""
    identity = ""
    try:
        account = json.loads(((home or Path.home()) / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
        identity = account.get("accountUuid") or account.get("organizationUuid") or ""
    except (OSError, ValueError, AttributeError):
        identity = ""
    if not identity:
        return None
    return {"provider_id": "claude", "pool_id": _pool_id("claude", identity), "pool_label": "Cuenta de Claude",
            "source": CLAUDE_SOURCE, "windows": [_unknown("claude.session", error.label, CLAUDE_SOURCE)]}


# ---------------------------------------------------------------------------------------------- Cursor
def cursor_state_db(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    configured = str(env.get("CURSOR_STATE_DB") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if platform.startswith("win"):
        base = Path(env.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def read_cursor_session(*, db_path: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    """The Cursor app's own session token, from its database opened read-only."""
    now = time.time() if now is None else float(now)
    path = db_path or cursor_state_db()
    if not path.is_file():
        raise AccountCapacityError("cursor_app_missing", "La app de Cursor no ha iniciado sesión en este PC")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        raise AccountCapacityError("cursor_state_unreadable", "No se pudo leer la sesión de la app de Cursor") from None
    token = row[0] if row else None
    if isinstance(token, bytes):
        token = token.decode("utf-8", "replace")
    if not isinstance(token, str) or token.count(".") != 2:
        raise AccountCapacityError("cursor_not_logged_in", "La app de Cursor no ha iniciado sesión en este PC")
    claims = _jwt_claims(token)
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and float(expires) - _EXPIRY_MARGIN_S <= now:
        raise AccountCapacityError("cursor_token_expired",
                                   "Sesión de la app de Cursor caducada: ábrela para renovarla")
    user_id = str(claims.get("sub") or "").split("|")[-1]
    if not user_id:
        raise AccountCapacityError("account_identity_unavailable", "No se pudo identificar la cuenta de Cursor")
    return {"token": token, "user_id": user_id}


def build_cursor_report(summary: Mapping[str, Any], session: Mapping[str, Any], *,
                        now: Optional[float] = None) -> dict[str, Any]:
    """Agora DTO from ``usage-summary``: the two included quotas the dashboard shows, plus on-demand."""
    if not isinstance(summary, Mapping):
        raise AccountCapacityError("invalid_response", "Respuesta de uso de Cursor no válida")
    now = time.time() if now is None else float(now)
    reset = summary.get("billingCycleEnd")
    individual = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), Mapping) else {}
    plan = individual.get("plan") if isinstance(individual.get("plan"), Mapping) else {}
    windows: list[dict[str, Any]] = []
    if summary.get("isUnlimited"):
        windows.append({"window_id": "cursor.plan", "label": "Plan sin límite", "measurement": "declared",
                        "used_percent": 0.0, "remaining_percent": 100.0, "reset_at": _iso(reset),
                        "source": CURSOR_SOURCE})
    for key, window_id, label in (("autoPercentUsed", "cursor.models", "Modelos de Cursor (Auto, Composer, Grok)"),
                                  ("apiPercentUsed", "cursor.other", "Otros modelos")):
        window = _measured(window_id, label, plan.get(key), reset, CURSOR_SOURCE, now)
        if window:
            windows.append(window)
    if not any(w["window_id"] in ("cursor.models", "cursor.other") for w in windows):
        used, limit = plan.get("used"), plan.get("limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            window = _measured("cursor.plan", "Incluido en el plan", 100.0 * float(used) / float(limit),
                               reset, CURSOR_SOURCE, now)
            if window:
                windows.append(window)
    on_demand = individual.get("onDemand") if isinstance(individual.get("onDemand"), Mapping) else {}
    if on_demand.get("enabled"):
        used, limit = on_demand.get("used"), on_demand.get("limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            window = _measured("cursor.on-demand", "Bajo demanda", 100.0 * float(used) / float(limit),
                               reset, CURSOR_SOURCE, now)
            if window:
                windows.append(window)
    if not windows:
        raise AccountCapacityError("cursor_usage_empty", "Cursor no devolvió cuotas del plan")
    membership = str(summary.get("membershipType") or "").strip()
    return {"provider_id": "cursor", "pool_id": _pool_id("cursor", session.get("user_id")),
            "pool_label": f"Cursor {membership.capitalize()}" if membership else "Cuenta de Cursor",
            "source": CURSOR_SOURCE, "windows": windows}


def collect_cursor_usage_report(*, db_path: Optional[Path] = None, now: Optional[float] = None,
                                opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    session = read_cursor_session(db_path=db_path, now=now)
    try:
        summary = _http_json(CURSOR_SUMMARY_URL, {
            "Cookie": f"WorkosCursorSessionToken={session['user_id']}%3A%3A{session['token']}",
            "Origin": "https://cursor.com",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 hoardlink-agora-capacity/0.1",
        }, opener=opener)
    except AccountCapacityError as exc:
        if exc.code == "unauthorized":
            raise AccountCapacityError("cursor_token_expired",
                                       "Sesión de la app de Cursor caducada: ábrela para renovarla") from None
        raise AccountCapacityError(exc.code, "No se pudo leer el uso de Cursor ahora") from None
    return build_cursor_report(summary, session, now=now)


def cursor_unavailable_report(error: AccountCapacityError, *, db_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    try:
        session = read_cursor_session(db_path=db_path, now=0.0)
    except AccountCapacityError:
        return None
    return {"provider_id": "cursor", "pool_id": _pool_id("cursor", session["user_id"]), "pool_label": "Cuenta de Cursor",
            "source": CURSOR_SOURCE, "windows": [_unknown("cursor.models", error.label, CURSOR_SOURCE)]}


# ------------------------------------------------------------------------------------------ publishing
_COLLECTORS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[..., Optional[dict[str, Any]]]]] = {
    "claude": (collect_claude_usage_report, claude_unavailable_report),
    "cursor": (collect_cursor_usage_report, cursor_unavailable_report),
}


def collect_and_report_account_usage(provider: str, *,
                                     reporter: Callable[..., dict[str, Any]] = _report_via_agora_cli,
                                     reporter_kwargs: Optional[dict[str, Any]] = None,
                                     collector_kwargs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Collect one provider and publish it as ``<provider>-capacity``. A known failure is published as an
    unknown window with its reason, so the bar says why instead of keeping an old number."""
    collect, unavailable = _COLLECTORS[provider]
    kwargs = dict(collector_kwargs or {})
    try:
        report = collect(**kwargs)
        reason = None
    except AccountCapacityError as exc:
        reason = exc.code
        report = None
        if exc.code not in ("network_unavailable", "rate_limited"):
            try:
                fallback_kwargs = {k: v for k, v in kwargs.items() if k in ("config_dir", "home", "db_path")}
                report = unavailable(exc, **fallback_kwargs)
            except Exception:
                report = None
    except Exception:
        LOGGER.debug("%s quota collection failed", provider, exc_info=False)
        return {"ok": False, "provider_id": provider, "reason": "collection_failed"}
    if report is None:
        return {"ok": False, "provider_id": provider, "reason": reason or "collection_failed"}
    publish = dict(reporter_kwargs or {})
    publish.setdefault("agent_id", f"{provider}-capacity")
    try:
        result = reporter(report, **publish)
    except Exception:
        LOGGER.debug("%s quota report failed", provider, exc_info=False)
        return {"ok": False, "provider_id": provider, "reason": "report_failed"}
    result = dict(result or {})
    result["provider_id"] = provider
    if result.get("ok"):
        result["window_count"] = len(report["windows"])
    if reason:
        result["reason"] = reason
        result["measured"] = False
    return result


def start_account_capacity_reporter(*, interval_seconds: int = 120,
                                    reporter_kwargs: Optional[Mapping[str, Any]] = None,
                                    providers: tuple[str, ...] = ("claude", "cursor")) -> threading.Thread:
    """One daemon poller for the Claude and Cursor accounts bound to this Hub."""
    global _REPORTER_THREAD, _REPORTER_STOP
    if isinstance(interval_seconds, bool) or not 60 <= int(interval_seconds) <= 600:
        raise ValueError("interval_seconds must be between 60 and 600")
    publication = dict(reporter_kwargs or {})
    with _REPORTER_LOCK:
        if _REPORTER_THREAD is not None and _REPORTER_THREAD.is_alive():
            return _REPORTER_THREAD
        stop = threading.Event()

        def run() -> None:
            while not stop.is_set():
                for provider in providers:
                    result = collect_and_report_account_usage(provider, reporter_kwargs=publication)
                    if not result.get("ok") or result.get("reason"):
                        LOGGER.info("%s quota observation: %s", provider, result.get("reason", "unknown"))
                stop.wait(interval_seconds)

        _REPORTER_STOP = stop
        _REPORTER_THREAD = threading.Thread(target=run, name="agora-account-capacity", daemon=True)
        _REPORTER_THREAD.start()
        return _REPORTER_THREAD


def stop_account_capacity_reporter(timeout: float = 1.0) -> None:
    global _REPORTER_THREAD, _REPORTER_STOP
    with _REPORTER_LOCK:
        thread, stop = _REPORTER_THREAD, _REPORTER_STOP
        if stop is not None:
            stop.set()
    if thread is not None:
        thread.join(timeout=max(0.0, float(timeout)))
    with _REPORTER_LOCK:
        if _REPORTER_THREAD is thread and (thread is None or not thread.is_alive()):
            _REPORTER_THREAD = None
            _REPORTER_STOP = None
