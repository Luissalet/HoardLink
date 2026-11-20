"""An app reads the hub's own tools through /api/apps/hoardhub/call with its own token."""

from __future__ import annotations

from ._hub_fakes import FakeApp, http, make_hub, serve


def test_an_app_calls_a_hub_tool_with_its_own_token(tmp_path):
    fake = FakeApp("nightingale")
    hub = make_hub(tmp_path, [fake])
    server = serve(hub)
    url = f"http://127.0.0.1:{hub.config.port}"
    app_token = {"Authorization": "Bearer " + fake.token}
    try:
        for hub_id in ("hoardhub", "hub", "hoard-hub"):
            status, body = http(f"{url}/api/apps/{hub_id}/call", {"tool": "hub_station_catalog", "arguments": {}},
                                headers=app_token)
            assert status == 200, body
            assert body["ok"] is True and body["app"] == "hoardhub" and body["tool"] == "hub_station_catalog"
            assert isinstance(body["result"]["stations"], list)

        status, body = http(f"{url}/api/apps/hoardhub/call", {"tool": "hub_station_catalog"})
        assert status == 401 and body["ok"] is False

        status, body = http(f"{url}/api/apps/hoardhub/call", {"tool": "no_such_tool"}, headers=app_token)
        assert status == 400 and "unknown tool" in body["result"]["error"]

        # Any other unknown id still answers as before.
        status, body = http(f"{url}/api/apps/nobody/call", {"tool": "x"}, headers=app_token)
        assert status == 404 and body["error"] == "unknown app"
    finally:
        server.shutdown()
        server.server_close()
        fake.stop()
        hub.close()
