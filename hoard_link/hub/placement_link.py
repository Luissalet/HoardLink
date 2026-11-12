"""Faustus on a Spark still reaches the Hub on this PC.

The Hub answers on this PC's loopback only, so a Faustus that runs on a Spark has no Hub: no Agora, and no rooms or
council either, whose records live here. While Faustus is on the Spark, :class:`HubLink` keeps a reverse SSH tunnel open:
a port on the Spark's own loopback that is this Hub. Next to the stack it leaves a small file that says where the Hub is
and a copy of the Hub's token; Faustus reads it (``FAUSTUS_HUB_LINK`` in its unit) for the Agora and for coordination.
Services that hand over file paths do not go through it: a path on the Spark means nothing here.

Both ends are loopback and the traffic travels inside SSH; nothing is published on a network. The tunnel closes when
Faustus comes back to the PC and when the Hub stops (the ssh child reads our pipe and ends with it, even if the Hub is
killed). With the PC off there is no Hub to reach, and Faustus says so.

A PC that dies without closing its connections (a blue screen, a power cut) leaves on the Spark an sshd that still holds
the forwarded port, and then no new tunnel can open. Two things keep that from lasting: the remote command of a tunnel
ends by itself when nothing has been written to it for ``REMOTE_IDLE_S``, and when the port is still taken the link ends
that one session (only the sshd of this user with no command behind it) and opens the tunnel again at once.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional

LINK_PORT = 8811                 # on the Spark's loopback; 8810 stays free for a Hub of its own
CHECK_S = 20.0                   # how often the tunnel is looked at
RETRY_S = 60.0                   # how long to wait after a tunnel that would not open
SETTLE_S = 4.0                   # a tunnel that is still there after this opened
REMOTE_IDLE_S = 90               # the remote command ends by itself when no ssh has written to it for this long
THREAD_NAME = "placement-hub-link"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes", "-o", "ExitOnForwardFailure=yes"]
# The remote command of a tunnel: it reads our pipe (so it ends when this process ends, however it ends) and ends by
# itself when nothing has arrived for REMOTE_IDLE_S. A PC that dies without closing the connection (a blue screen, a
# power cut) leaves an sshd on the Spark that still holds the forwarded port; this is what makes it let go.
# bash by name: the Spark's login shell may be sh, where "read -t" does not exist.
REMOTE_CMD = f"bash -c 'while IFS= read -r -t {REMOTE_IDLE_S} _; do :; done'"
# What ssh says when the port on the Spark is already taken by a forwarding nobody is using any more.
PORT_BUSY = ("remote port forwarding failed", "address already in use", "cannot listen to port")


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
        self._state: dict[str, Any] = {"wanted": False, "up": False, "since": None, "error": "", "port": LINK_PORT, "node": "",
                                       "freed": ""}

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
                self._state.update(up=False, since=None, error="", freed="")
                state, action = dict(self._state), "close"
            elif self._proc is not None and self._proc.poll() is None:
                if not self._state["up"]:
                    self._state.update(up=True, since=self._clock(), error="")
                state, action = dict(self._state), "beat"
            elif self._proc is not None:                     # it ended: say why and try again later
                self._state.update(up=False, since=None, error=self._why(self._proc) or "el túnel se cerró", freed="")
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
        if action == "beat":
            self._beat()                                     # outside the lock: a write never holds up status()
            return state
        opened, error, freed = None, "", ""
        try:
            self._tell(node)
            opened, error = self._open_and_settle(node)
            if opened is None and any(mark in error.lower() for mark in PORT_BUSY):
                # The port on the Spark is still held by the tunnel of a Hub that is gone (the PC died without closing
                # it). Free that one session and try again now, instead of failing every RETRY_S for ever.
                released, freed = self._free_forward()
                if released:
                    for attempt in range(3):                 # the port takes a moment to come free after the kill
                        opened, error = self._open_and_settle(node)
                        if opened is not None:
                            break
                        time.sleep(0.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - the Spark may be off or unreachable: that is a state, not a crash
            self._end(opened)
            error, opened = str(exc)[-300:] or type(exc).__name__, None
        with self._lock:
            self._busy = False
            still_wanted = self.placement.state().get("location") == "sparks" and not self._stop.is_set()
            if opened is not None and still_wanted:
                self._proc = opened
                self._state.update(up=True, since=self._clock(), error="", freed=freed)
                opened = None
            elif opened is None:
                self._told = ""
                self._next_try = self._clock() + RETRY_S
                self._state.update(up=False, since=None, error=error, freed=freed)
            else:                                            # it came back to the PC, or the Hub is stopping, meanwhile
                self._state.update(wanted=False, node="", up=False, since=None, error="", freed="")
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
        # finishes and ssh goes with it. It also ends by itself when no ssh has written to it for REMOTE_IDLE_S, so a
        # session whose PC died lets go of the port instead of holding it for ever.
        return self._popen(["ssh", "-T", *SSH_OPTS, "-R", f"127.0.0.1:{LINK_PORT}:127.0.0.1:{self._hub_port()}", node, REMOTE_CMD],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_flags())

    def _open_and_settle(self, node: str) -> tuple[Any, str]:
        """Open the tunnel and wait to see it hold: the process (None when it did not hold) and why it did not."""
        opened = self._open(node)
        deadline = time.time() + self._settle_s
        while time.time() < deadline and opened.poll() is None:
            time.sleep(0.2)
        if opened.poll() is None:
            return opened, ""
        return None, self._why(opened) or "el túnel no se abrió"

    def _beat(self) -> None:
        """Write a line into the tunnel's pipe: the remote command reads it and keeps waiting, so a session whose PC is
        gone ends by itself after REMOTE_IDLE_S instead of holding the port for ever."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        except Exception:  # noqa: BLE001 - a pipe that is gone is the next look's business
            pass

    @staticmethod
    def _probe_command() -> str:
        """What listens on the link port on the Spark and which sshd session owns it. Read-only: it asks, never kills."""
        return (f"ss -ltnpH 'sport = :{LINK_PORT}' 2>/dev/null || true; echo --; "
                f"ps -eo pid=,ppid=,etimes=,args= | grep -F '[sshd: ' | grep -v grep || true")

    @staticmethod
    def _parse_probe(text: str) -> dict[str, Any]:
        """The listener on the link port and the sshd sessions of the Spark, from ``_probe_command``'s answer."""
        lines = (text or "").splitlines()
        cut = lines.index("--") if "--" in lines else len(lines)
        listen, sessions = lines[:cut], lines[cut + 1:]
        out: dict[str, Any] = {"listening": False, "pid": 0, "sessions": []}
        for line in listen:
            if f":{LINK_PORT}" not in line:
                continue
            out["listening"] = True
            found = re.search(r"pid=(\d+)", line)
            out["pid"] = int(found.group(1)) if found else 0
        for line in sessions:
            parts = line.split(None, 3)
            if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            out["sessions"].append({"pid": int(parts[0]), "ppid": int(parts[1]),
                                    "age": int(parts[2]) if parts[2].isdigit() else 0, "args": parts[3]})
        return out

    def _free_forward(self) -> tuple[bool, str]:
        """Let go of a forwarding on the link port that nobody is using any more: whether it was freed, and what was done.

        A PC that dies without closing its connections leaves on the Spark an sshd that still listens on the link port,
        so every new tunnel fails with ExitOnForwardFailure for ever. The only session ended is the one that owns that
        listener, and only when it is a session of this user with no command behind it (``sshd: <user>@notty``): the
        tunnel of a live Hub is never touched, and neither is anything else.
        """
        probe = self._parse_probe(self.placement._ssh(self._probe_command(), timeout=30))
        if not probe["listening"]:
            return False, ""
        pid = probe["pid"]
        owner = next((s for s in probe["sessions"] if pid and s["pid"] == pid), None)
        if owner is None:
            return False, f"el puerto {LINK_PORT} de la Spark lo ocupa un proceso que no es una sesión ssh; no lo toco"
        if not owner["args"].endswith("@notty"):
            return False, (f"el puerto {LINK_PORT} de la Spark lo ocupa «{owner['args']}», que no es una sesión sin "
                           f"orden; no la toco")
        self.placement._ssh(f"kill {pid}", timeout=30)
        return True, f"he liberado el puerto {LINK_PORT} de la Spark (sesión ssh {pid} sin orden, {owner['age']} s)"

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
