"""Review follow-ups: Atlas failures never come back with a success status, and the tool handlers accept no arguments."""
from hoard_link.hub.workspace import WorkspaceFacet


class _Hub:
    def __init__(self, result):
        self.result = result

    def get(self, app_id):
        return object() if app_id == "atlas" else None

    def call_app(self, app_id, tool, args, caller=None):
        return self.result


def _facet(result):
    facet = WorkspaceFacet.__new__(WorkspaceFacet)
    facet.hub = _Hub(result)
    return facet


def test_atlas_ok_false_with_status_200_is_not_a_success():
    reply = _facet({"ok": False, "status": 200, "error": "atlas said no"})._call("hub", "projects", {})
    assert reply.status == 502 and reply.payload["ok"] is False and reply.payload["error"] == "atlas said no"


def test_non_numeric_or_missing_status_and_unreachable():
    assert _facet({"ok": False, "status": "abc", "error": "x"})._call("hub", "projects", {}).status == 502
    assert _facet({"ok": False, "error": "not reachable"})._call("hub", "projects", {}).status == 503
    assert _facet({"ok": False, "status": 404, "error": "no project"})._call("hub", "project", {}).status == 404


def test_handlers_accept_none_arguments():
    handlers = _facet({"ok": True, "result": {"projects": []}}).handlers()
    assert handlers["hub_workspace"](None)["ok"] is False          # no tool named: a clean refusal, not a crash
    assert handlers["hub_atlas_projects"](None) == {"projects": []}
