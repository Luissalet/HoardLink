"""A browser session comes from the local launcher and stays separate from tools."""

from http.client import HTTPConnection
import json
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
        for locale, message in (
            ("es-ES,es;q=0.9,en;q=0.8", "Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir el Hub."),
            ("en-GB,en;q=0.9,es;q=0.8", "This window is from an earlier version. Close it and reopen the Hub."),
        ):
            status, reply = http(endpoint, event, {**browser, "Accept-Language": locale})
            assert status == 403
            assert reply == {"ok": False, "code": "hub_ui_session_required", "error": message}
        assert http(endpoint, event, {**browser, ui_session.SESSION_HEADER: session["session"]})[1]["event"]["source"] == "ui"
        assert http(endpoint, event, agent)[1]["event"]["source"] == "hub"
    finally:
        server.shutdown()
        server.server_close()
        hub.close()


def test_outdated_native_console_gets_a_visible_error_without_a_write(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    path = "/api/agora/console/color"
    headers = {"Origin": hub.config.url, "Sec-Fetch-Site": "same-origin", "Accept": "*/*",
               "Content-Type": "application/json"}
    body = {"leader_id": "codex", "color": "#112233"}
    connection = HTTPConnection("127.0.0.1", hub.config.port, timeout=5)
    try:
        before = http(hub.config.url + "/api/agora/console/overview")[1]["leaders"]
        for language, notice in (
            (None, "Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir la consola del Ágora."),
            ("en-GB", "This window is from an earlier version. Close it and reopen the Agora console."),
        ):
            request_headers = {**headers, **({"Accept-Language": language} if language else {})}
            connection.request("POST", path, json.dumps(body), request_headers)
            result = connection.getresponse()
            assert result.status == 200
            assert json.loads(result.read()) == {"ok": False, "code": "hub_ui_session_required", "error": notice}
        for additional in ({"Sec-Fetch-Mode": "cors"}, {"User-Agent": "HoardAgora"},
                           {"X-Hoard-Person": "faustus-studio"}, {ui_session.SESSION_HEADER: "invalid"}):
            connection.request("POST", path, json.dumps(body), {**headers, **additional})
            result = connection.getresponse()
            assert result.status == 403 and json.loads(result.read())["code"] == "hub_ui_session_required"
        after = http(hub.config.url + "/api/agora/console/overview")[1]["leaders"]
        assert [leader.get("color") for leader in before] == [leader.get("color") for leader in after]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        hub.close()
