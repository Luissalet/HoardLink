"""Choose temporary IPv4 loopback ports without probing installed listeners."""

import errno
import os
import random
import re
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from hoard_link._process import run_hidden


TEMPORARY_PORTS = range(49152, 65536)
RESERVED_PORTS = frozenset({5191, 5220, 5232, 5253, 8810, 8815, 59018})


def _windows_tcp_exclusions():
    if sys.platform != "win32":
        return ()
    netsh = Path(os.environ["SystemRoot"]) / "System32" / "netsh.exe"
    result = run_hidden(
        [str(netsh), "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
        capture_output=True, check=True, encoding="utf-8", errors="replace", timeout=10,
        creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    )
    rows = (re.fullmatch(r"\s*(\d+)\s+(\d+)\s*\*?\s*", line) for line in result.stdout.splitlines())
    return tuple(range(int(row[1]), int(row[2]) + 1) for row in rows if row)


def _windows_tcp_in_use():
    if sys.platform != "win32":
        return frozenset()
    netstat = Path(os.environ["SystemRoot"]) / "System32" / "netstat.exe"
    result = run_hidden(
        [str(netstat), "-anoq", "-p", "tcp"],
        capture_output=True, check=True, encoding="utf-8", errors="replace", timeout=10,
        creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    )
    rows = (re.match(r"\s*TCP\s+\S+:(\d+)\s", line) for line in result.stdout.splitlines())
    return frozenset(int(row[1]) for row in rows if row)


def free_loopback_ports(count, *, exclude=()):
    """Hold distinct sockets until the full set of candidate ports is known."""
    excluded = RESERVED_PORTS.union(exclude, _windows_tcp_in_use())
    system_exclusions = _windows_tcp_exclusions()
    start = random.randrange(len(TEMPORARY_PORTS))
    with ExitStack() as stack:
        ports = []
        for offset in range(len(TEMPORARY_PORTS)):
            if len(ports) == count:
                return tuple(ports)
            port = TEMPORARY_PORTS[(start + offset) % len(TEMPORARY_PORTS)]
            if port in excluded or any(port in interval for interval in system_exclusions):
                continue
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                probe.close()
                if isinstance(exc, PermissionError):
                    raise PermissionError(exc.errno, f"127.0.0.1:{port}: {exc.strerror}",
                                          None, getattr(exc, "winerror", None)) from exc
                if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
                    raise
                continue
            stack.callback(probe.close)
            ports.append(port)
        if len(ports) != count:
            raise OSError("Unable to reserve enough permitted loopback ports")
        return tuple(ports)
