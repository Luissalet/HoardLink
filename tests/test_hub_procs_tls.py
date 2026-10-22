"""App health probes must not read the Windows certificate stores for HTTP."""
import io
import ssl
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest

from hoard_link.hub import procs


def reply(url, status=200, body=b'{"ok": true}', headers=None):
    response = urllib.response.addinfourl(io.BytesIO(body), headers or Message(), url, status)
    response.msg = "fixture"
    return response


@pytest.mark.parametrize("post", [False, True])
def test_http_probes_skip_tls(monkeypatch, post):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP must not initialize certificates")
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", forbidden)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", forbidden)
    seen = []
    def opened(self, req):
        seen.append((req.get_method(), req.data, req.timeout, self.parent))
        return reply(req.full_url)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", opened)
    result = (procs.fetch_json_post("http://127.0.0.1:9/api", {"mode": "browser"}, timeout=3)
              if post else procs.fetch_json("http://127.0.0.1:9/api", timeout=3))
    assert result == (200, {"ok": True})
    assert seen[0][:3] == (("POST", b'{"mode": "browser"}', 3) if post else ("GET", None, 3))
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in seen[0][3].handlers)


@pytest.mark.parametrize("redirect", [False, True])
def test_https_and_http_redirect_keep_default_tls(monkeypatch, redirect):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    contexts = []
    def default_certs(self, purpose=ssl.Purpose.SERVER_AUTH):
        contexts.append((self.verify_mode, self.check_hostname, purpose))
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", default_certs)
    requests = []
    def https_open(self, req):
        requests.append((req.full_url, self._context.verify_mode, self._context.check_hostname))
        return reply(req.full_url)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    def http_open(self, req):
        headers = Message()
        headers["Location"] = "https://example.test/api"
        return reply(req.full_url, 302, b"", headers)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    url = "http://example.test/api" if redirect else "https://example.test/api"
    assert procs.fetch_json(url) == (200, {"ok": True})
    assert contexts == [(ssl.CERT_REQUIRED, True, ssl.Purpose.SERVER_AUTH)]
    assert requests == [("https://example.test/api", context.verify_mode, context.check_hostname)]


def test_no_redirect_does_not_initialize_tls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Refused redirect must not initialize TLS")
    monkeypatch.setattr(urllib.request.HTTPSHandler, "__init__", forbidden)
    def opened(self, req):
        headers = Message()
        headers["Location"] = "https://example.test/api"
        return reply(req.full_url, 302, b"", headers)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", opened)
    assert procs.fetch_json("http://127.0.0.1:9/api", follow_redirects=False) == (302, None)


def test_probe_size_and_network_failures(monkeypatch):
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", lambda self, req: reply(req.full_url))
    assert procs.fetch_json("http://127.0.0.1:9/api", max_bytes=2) == (200, None)
    def failed(self, req):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", failed)
    assert procs.fetch_json("http://127.0.0.1:9/api") == (None, None)
    assert procs.fetch_json_post("http://127.0.0.1:9/api", {}) == (None, None)
