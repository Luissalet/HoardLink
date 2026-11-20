"""A browser session comes from the local launcher and stays separate from tools."""

from urllib.parse import parse_qs, urlsplit

from hoard_link.hub import ui_session

from ._hub_fakes import http, make_hub, serve


def test_launcher_tickets_require_local_credential_and_are_single_use(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    url = hub.config.url
    agent = {"Authorization": "Bearer " + hub.token}
    browser = {"Sec-Fetch-Site": "same-origin", "Origin": url}
    try:
        for headers in (browser, agent):
            assert http(url + ui_session.LAUNCH_PATH, {}, headers)[0] == 403
        link = ui_session.launch_url(url + "/?space_id=ecosystem#hub/orders", hub.config.data_dir)
        parts = urlsplit(link)
        fragment = parse_qs(parts.fragment)
        assert parts.query == "space_id=ecosystem" and fragment["view"] == ["hub/orders"]
        payload = {"ticket": fragment["hub-session"][0]}
        exchange = url + ui_session.SESSION_PATH
        assert http(exchange, payload, {**browser, "Origin": "https://example.test"})[0] == 403
        status, session = http(exchange, payload, browser)
        assert status == 200
        assert http(exchange, payload, browser)[0] == 403

        endpoint = url + "/api/events"
        event = {"type": "test.session.write", "data": {}}
        for headers in ({"Sec-Fetch-Site": "none"}, browser, {**browser, ui_session.SESSION_HEADER: "invalid"}):
            status, reply = http(endpoint, event, headers)
            assert status == 403 and reply["code"] == "hub_ui_session_required"
        assert http(endpoint, event, {**browser, ui_session.SESSION_HEADER: session["session"]})[1]["event"]["source"] == "ui"
        assert http(endpoint, event, agent)[1]["event"]["source"] == "hub"
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
