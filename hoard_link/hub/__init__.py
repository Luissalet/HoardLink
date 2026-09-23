"""Hoard Hub: the desktop launcher for a family of local, agent-controlled apps.

Every app in the family ships a ``faustus-plugin.json`` in its repository
root that says what it is, where it listens, how to check it is healthy and
how to start it. The hub reads those manifests straight from the folders
next to it (or from any root you configure), shows one card per app with
its icon, tells you which ones are running (port, pid, memory, uptime),
and starts, stops, opens and closes them — in the browser or as their own
desktop window — without any AI workspace in the loop.

It is the "open the apps without Faustus" half of Hoard Link: the library
answers "which model server do I use", the hub answers "which of my apps
are up, and open that one". Both are loopback-only and both are meant to be
driven by a person *or* by an agent: the hub exposes the same
``/api/agent/tools`` + ``/api/agent/call`` contract as the apps it manages,
plus a stdio MCP bridge (``python -m hoard_link.hub.mcp``).

Optional extras: ``psutil`` for pid/memory/uptime and process-tree stops
(without it the hub still shows health and can open apps, but cannot stop
them), ``pywebview`` for a native window instead of a Chromium ``--app``
window.
"""

from __future__ import annotations

HUB_VERSION = "0.1.0"
DEFAULT_PORT = 8810
SERVICE = "hoard-hub"

__all__ = ["HUB_VERSION", "DEFAULT_PORT", "SERVICE"]
