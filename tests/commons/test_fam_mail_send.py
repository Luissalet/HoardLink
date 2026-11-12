"""Both language clients use the authenticated mail gateway and never retry a mutation."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hoard_link import family, fam_mail
from tests.commons.test_fam_services_js import run


@pytest.fixture
def gateway(tmp_path):
    requests = []
    response = {"ok": True, "state": "sent", "sent": True, "message_id": "<proof@example.com>"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"available":true,"accounts":[]}')

        def do_POST(self):
            requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])) )})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    saved = dict(family._state)
    token = tmp_path / "mcp-token"
    token.write_text("test-only-token", encoding="utf-8")
    url = f"http://127.0.0.1:{server.server_port}"
    family.configure("ledger", token_file=str(token), hub=url)
    yield url, token, requests, response
    family._state.clear()
    family._state.update(saved)
    server.shutdown()
    server.server_close()
    thread.join()


def test_python_sends_explicitly_and_defaults_to_draft(gateway, tmp_path):
    _, _, calls, response = gateway
    assert fam_mail.send_status()["available"]
    assert fam_mail.send(["me@example.com"], "Prueba", "Cuerpo")["ok"]
    assert calls[0]["body"]["mode"] == "draft"
    result = fam_mail.send(["me@example.com"], "Prueba", "Cuerpo", mode="send", attachments=[tmp_path / "file.txt"])
    assert result["sent"] and result["via"] == "hub"
    assert calls[-1]["auth"] == "Bearer test-only-token" and calls[-1]["path"] == "/api/mail/compose"
    assert calls[-1]["body"]["attachments"] == [str(tmp_path / "file.txt")]
    response.clear()
    response.update(ok=False, error="SMTP failed", state="unknown")
    assert not fam_mail.send(["me@example.com"], "Prueba", "Cuerpo", mode="send")["ok"]
    assert len(calls) == 3


def test_node_has_the_same_contract(gateway, tmp_path):
    url, token, calls, response = gateway
    result = run(url, token, """
      const ready = await fam.serviceAvailable('mail');
      const draft = await fam.mailSend(['me@example.com'], 'Prueba', 'Cuerpo');
      const sent = await fam.mailSend(['me@example.com'], 'Prueba', 'Cuerpo', {mode:'send', cc:['copy@example.com'], attachments:['/shared/file.txt']});
      return {ready, draft, sent};
    """)
    assert result["ready"] and result["sent"]["sent"]
    assert calls[0]["body"]["mode"] == "draft" and calls[1]["body"]["mode"] == "send"
    assert calls[1]["body"]["cc"] == ["copy@example.com"] and calls[1]["auth"] == "Bearer test-only-token"
    response.clear()
    response.update(ok=False, state="unknown", error="lost reply")
    result = run(url, token, "return await fam.mailSend(['me@example.com'], 'Prueba', 'Cuerpo', {mode:'send'});")
    assert not result["ok"] and result["state"] == "unknown" and len(calls) == 3
