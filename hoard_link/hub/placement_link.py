"""Faustus on a Spark still reaches the Hub on this PC.

The Hub answers on this PC's loopback only, so a Faustus that runs on a Spark has no Hub: no Agora, and no rooms or
council either, whose records live here. While Faustus is on the Spark, :class:`HubLink` keeps a reverse SSH tunnel open:
a port on the Spark's own loopback that is this Hub. Next to the stack it leaves a small file that says where the Hub is
and a copy of the Hub's token; Faustus reads it (``FAUSTUS_HUB_LINK`` in its unit) for the Agora and for coordination.
Services that hand over file paths do not go through it: a path on the Spark means nothing here.

Both ends are loopback and the traffic travels inside SSH; nothing is published on a network. The tunnel closes when
Faustus comes back to the PC and when the Hub stops (the ssh child reads our pipe and ends with it, even if the Hub is
killed). With the PC off there is no Hub to reach, and Faustus says so.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from typing import Any, Callable, Optional

LINK_PORT = 8811                 # on the Spark's loopback; 8810 stays free for a Hub of its own
CHECK_S = 20.0                   # how often the tunnel is looked at
RETRY_S = 60.0                   # how long to wait after a tunnel that would not open
SETTLE_S = 4.0                   # a tunnel that is still there after this opened
THREAD_NAME = "placement-hub-link"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes"]


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class HubLink:
    """The tunnel and what the Spark is told about it. ``tick`` is one look; ``start`` looks every ``CHECK_S``."""

    def __init__(self, placement: Any, *, popen: Optional[Callable[..., Any]] = None, clock: Callable[[], float] = time.time,
                 settle_s: float = SETTLE_S):
        self.placement = placement
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self._settle_s = settle_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Any = None
        self._told = ""              # what the Spark was last told: node, port and token, as one digest
        self._next_try = 0.0
        self._busy = False           # a look is talking to the Spark right now
        self._state: dict[str, Any] = {"wanted": False, "up": False, "since": None, "error": "", "port": LINK_PORT, "node": ""}

    # ----------------------------------------------------------------------------------------- life
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def poke(self) -> None:
        """Look now instead of at the next turn (after a move, so the tunnel does not wait for the clock)."""
        self._next_try = 0.0
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            threading.Thread(target=self._safe_tick, name=THREAD_NAME + "-poke", daemon=True).start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._safe_tick()
            self._stop.wait(CHECK_S)

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as exc:  # noqa: BLE001 - a link that cannot be kept is reported, never raised into the Hub
            with self._lock:
                self._busy = False
                self._state.update(up=False, error=str(exc)[-300:])

    # ----------------------------------------------------------------------------------------- one look
    def tick(self) -> dict[str, Any]:
        """Look once. The lock only guards the decision and the state: everything that talks to the Spark (telling it
        where the Hub is, opening the tunnel, waiting to see it hold) runs outside it, so ``status()`` never waits for
        SSH, however unreachable the Spark is. ``_busy`` keeps two looks from opening two tunnels."""
        with self._lock:
            data = self.placement.state()
            node = str((data.get("node") or {}).get("ssh") or "")
            wanted = data.get("location") == "sparks" and bool(node) and not self._stop.is_set()
            self._state.update(wanted=wanted, node=node if wanted else "")
            if not wanted:
                proc, self._proc = self._proc, None
                self._state.update(up=False, since=None, error="")
                state, action = dict(self._state), "close"
            elif self._proc is not None and self._proc.poll() is None:
                if not self._state["up"]:
                    self._state.update(up=True, since=self._clock(), error="")
                return dict(self._state)
            elif self._proc is not None:                     # it ended: say why and try again later
                self._state.update(up=False, since=None, error=self._why(self._proc) or "el túnel se cerró")
                self._proc, self._told = None, ""            # the Spark is told again before the next tunnel
                self._next_try = self._clock() + RETRY_S
                return dict(self._state)
            elif self._busy or self._clock() < self._next_try:
                return dict(self._state)
            else:
                self._busy = True
                state, action, proc = dict(self._state), "open", None
        if action == "close":
            self._end(proc)
            return state
        opened, error = None, ""
        try:
            self._tell(node)
            opened = self._open(node)
            deadline = time.time() + self._settle_s
            while time.time() < deadline and opened.poll() is None:
                time.sleep(0.2)
            if opened.poll() is not None:
                error, opened = self._why(opened) or "el túnel no se abrió", None
        except Exception as exc:  # noqa: BLE001 - the Spark may be off or unreachable: that is a state, not a crash
            self._end(opened)
            error, opened = str(exc)[-300:] or type(exc).__name__, None
        with self._lock:
            self._busy = False
            still_wanted = self.placement.state().get("location") == "sparks" and not self._stop.is_set()
            if opened is not None and still_wanted:
                self._proc = opened
                self._state.update(up=True, since=self._clock(), error="")
                opened = None
            elif opened is None:
                self._told = ""
                self._next_try = self._clock() + RETRY_S
                self._state.update(up=False, since=None, error=error)
            else:                                            # it came back to the PC, or the Hub is stopping, meanwhile
                self._state.update(wanted=False, node="", up=False, since=None, error="")
            state = dict(self._state)
        self._end(opened)                                    # a tunnel nobody wants any more
        return state

    # ----------------------------------------------------------------------------------------- the two halves
    def _hub_port(self) -> int:
        return int(self.placement.hub.config.port or 8810)

    def link_file(self) -> dict[str, Any]:
        from .placement import SPARK_STACK
        return {"url": f"http://127.0.0.1:{LINK_PORT}", "token_file": f"{SPARK_STACK}/hub-token", "hub": platform.node(),
                "port": self._hub_port()}

    def _tell(self, node: str) -> None:
        """Leave on the Spark where the Hub is and its token (only when either changed: it costs two SSH round trips)."""
        from .placement import STACK
        token = str(getattr(self.placement.hub, "token", "") or "")
        link = self.link_file()
        mark = hashlib.sha256(json.dumps([node, link, token], sort_keys=True).encode("utf-8")).hexdigest()
        if mark == self._told:
            return
        self.placement._ssh(f"umask 077; mkdir -p {STACK}; cat > {STACK}/hub-token.tmp && mv {STACK}/hub-token.tmp {STACK}/hub-token",
                            input=token, timeout=30)
        self.placement._ssh(f"cat > {STACK}/hub-link.json.tmp && mv {STACK}/hub-link.json.tmp {STACK}/hub-link.json",
                            input=json.dumps(link), timeout=30)
        self._told = mark

    def _open(self, node: str) -> Any:
        # The remote command only reads our pipe: when this process ends, however it ends, the pipe closes, the command
        # finishes and ssh goes with it. No tunnel is left behind holding the port.
        return self._popen(["ssh", "-T", *SSH_OPTS, "-R", f"127.0.0.1:{LINK_PORT}:127.0.0.1:{self._hub_port()}", node, "cat > /dev/null"],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_flags())

    @staticmethod
    def _why(proc: Any) -> str:
        try:
            err = proc.stderr.read() if proc.stderr is not None else b""
        except Exception:  # noqa: BLE001
            err = b""
        text = (err.decode("utf-8", errors="replace") if isinstance(err, bytes) else str(err or "")).strip()
        return text.splitlines()[-1][-300:] if text else ""

    def _close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        self._end(proc)

    @staticmethod
    def _end(proc: Any) -> None:
        """End a tunnel: close our pipe (the remote command reads its end and finishes), then insist if it lingers."""
        if proc is None:
            return
        for step in (lambda: proc.stdin.close(), lambda: proc.wait(timeout=3), proc.terminate, lambda: proc.wait(timeout=3), proc.kill):
            try:
                if proc.poll() is not None:
                    break
                step()
            except Exception:  # noqa: BLE001 - closing is best effort; the child ends with our pipe anyway
                continue
