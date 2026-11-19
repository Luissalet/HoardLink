"""Named HTTP entry points for the Hub's existing agent tool contract."""

from . import tools

PREFIX = "/api/agent/tools/"


def describe(path):
    name = path.removeprefix(PREFIX)
    return next((tool for tool in tools.all_tools() if tool["name"] == name), None)


def invoke(hub, name, arguments):
    result = tools.call(hub, name, arguments)
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    return {"ok": ok, "tool": name, "result": result}, 200 if ok else 400
