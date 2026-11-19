"""Verify named HTTP routes for the entire Hub tool catalogue."""

import pytest

from hoard_link.hub import facets, tools

from .test_hub_and_server import _http, served  # noqa: F401


def test_all_catalogue_tools_have_named_routes(served, monkeypatch):
    hub, url = served
    headers = {"Authorization": f"Bearer {hub.token}"}
    status, response = _http(url + "/api/agent/tools", headers=headers)
    assert status == 200
    assert {spec["name"] for spec in response["tools"]} <= (
        tools.handlers(hub).keys() | facets.handlers(hub.facets).keys()
    )

    # Verify dispatch without running write tools, model requests or jobs.
    monkeypatch.setattr(tools, "call", lambda target, name, args: {"name": name, "args": args, "same": target is hub})
    for spec in response["tools"]:
        name = spec["name"]
        path = url + "/api/agent/tools/" + name
        assert _http(path, headers=headers) == (200, spec)
        assert _http(path, {"probe": name}, headers) == (
            200, {"ok": True, "tool": name, "result": {"name": name, "args": {"probe": name}, "same": True}},
        )
    path = url + "/api/agent/tools/hub_does_not_exist"
    assert _http(path, headers=headers)[0] == 404
    assert _http(path, {}, headers)[0] == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_named_agent_routes_require_the_hub_token(served, method):
    _, url = served
    status, _ = _http(url + "/api/agent/tools/hub_station_catalog", method=method,
                      body={} if method == "POST" else None)
    assert status == 401


def test_status_alias_keeps_legacy_app_status_shape(served):
    hub, url = served
    canonical, current = _http(url + "/api/apps/fake/status")
    legacy, original = _http(url + "/api/apps/fake")
    assert canonical == legacy == 200
    assert current.keys() == original.keys()
    assert current["id"] == original["id"] == "fake"
    headers = {"Authorization": f"Bearer {hub.token}"}
    status, via_agent = _http(url + "/api/agent/tools/hub_app_status", {"app": "fake"}, headers)
    assert status == 200 and via_agent["result"]["state"] == current["state"] == original["state"]
    assert _http(url + "/api/apps/missing/status")[0] == 404
