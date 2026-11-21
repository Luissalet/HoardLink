"""The local launcher grants the Hub page an origin-scoped write session."""

from __future__ import annotations

import hmac
from pathlib import Path
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..tokens import check_bearer, new_token, read_or_create_token
from . import procs

LAUNCH_FILE = "hub-ui-launch-token"
LAUNCH_PATH = "/api/ui/launch"
SESSION_PATH = "/api/ui/session"
SESSION_HEADER = "X-Hoard-UI-Session"
TICKET_LIFETIME = 60


class BrowserSessions:
    def __init__(self, data_dir: str, origin: str):
        self._launch_key = read_or_create_token(Path(data_dir) / LAUNCH_FILE)
        self._session_key = hmac.digest(self._launch_key.encode(), f"Hub UI session {origin}".encode(), "sha256")
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, authorization: str) -> str:
        if not check_bearer(authorization, self._launch_key):
            return ""
        ticket = new_token()
        now = time.monotonic()
        with self._lock:
            self._pending = {key: expiry for key, expiry in self._pending.items() if expiry > now}
            self._pending[ticket] = now + TICKET_LIFETIME
        return ticket

    def exchange(self, ticket: object) -> str:
        if not isinstance(ticket, str):
            return ""
        with self._lock:
            expires = self._pending.pop(ticket, 0)
        if expires <= time.monotonic():
            return ""
        nonce = new_token()
        return f"{nonce}.{self._sign(nonce)}"

    def accepts(self, proof: str) -> bool:
        nonce, _, signature = proof.partition(".")
        return (len(nonce) == 43 and len(signature) == 64
                and hmac.compare_digest(signature.encode(), self._sign(nonce).encode()))

    def _sign(self, nonce: str) -> str:
        return hmac.digest(self._session_key, nonce.encode(), "sha256").hex()


def launch_url(url: str, data_dir: str) -> str:
    """Open a local browser with a short-lived, single-use ticket in the fragment."""
    target = urlsplit(url)
    origin = f"{target.scheme}://{target.netloc}"
    key = read_or_create_token(Path(data_dir) / LAUNCH_FILE)
    status, response = procs.fetch_json_post(origin + LAUNCH_PATH, {}, timeout=3, follow_redirects=False,
                                             headers={"Authorization": "Bearer " + key})
    if status != 200 or not isinstance(response, dict) or not response.get("ticket"):
        return url
    fragment = urlencode({"hub-session": response["ticket"], "view": target.fragment})
    return urlunsplit(target._replace(fragment=fragment))
