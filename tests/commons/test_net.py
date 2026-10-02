"""hoard_link.net: ports, "already running" (port busy AND /api/health names this service), launcher helpers."""

from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from hoard_link import net


class Health(http.server.BaseHTTPRequestHandler):
    payload: object = {"service": "kafka-hoard"}
    status = 200

    def do_GET(self):  # noqa: N802
        if self.path != "/api/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode() if not isinstance(self.payload, bytes) else self.payload
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    started = []

    def make(payload=None, status=200):
        handler = type("H", (Health,), {"payload": {"service": "kafka-hoard"} if payload is None else payload, "status": status})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv.server_address[1]

    yield make
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_free_port_and_can_listen():
    port = net.free_port()
    assert 1024 <= port <= 65535 and net.can_listen(port)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert net.can_listen(taken) is False


def test_can_listen_rejects_junk():
    for bad in (0, -1, 70000, "x", None):
        assert net.can_listen(bad) is False


def test_find_available_port_skips_busy_ones():
    with socket.socket() as a:
        a.bind(("127.0.0.1", 0))
        a.listen()
        first = a.getsockname()[1]
        found = net.find_available_port(first, span=20)
        assert found != first and first < found <= first + 20
        with pytest.raises(RuntimeError):
            net.find_available_port(first, span=0)
        assert net.find_available_port(first) == found           # the same answer while nothing changes


def test_find_available_port_errors():
    with pytest.raises(ValueError):
        net.find_available_port(0)
    with pytest.raises(ValueError):
        net.find_available_port(70000)
    with pytest.raises(ValueError):
        net.find_available_port("abc")


def test_find_available_port_stops_at_65535(monkeypatch):
    monkeypatch.setattr(net, "can_listen", lambda port, host="127.0.0.1": False)
    with pytest.raises(RuntimeError, match="65535"):
        net.find_available_port(65530, span=100)


def test_exclusive_bind_on_windows_uses_the_right_option(monkeypatch):
    options = []

    class FakeSocket:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, level, opt, value):
            options.append(opt)

        def bind(self, addr):
            pass

    monkeypatch.setattr(net.socket, "socket", FakeSocket)
    monkeypatch.setattr(net.sys, "platform", "win32")
    monkeypatch.setattr(net.socket, "SO_EXCLUSIVEADDRUSE", 0xFFFB, raising=False)
    assert net.can_listen(5200)
    assert options == [0xFFFB]
    options.clear()
    monkeypatch.setattr(net.sys, "platform", "linux")
    assert net.can_listen(5200)
    assert options == [net.socket.SO_REUSEADDR]


def test_already_running_true_only_for_the_same_service(server):
    port = server({"service": "kafka-hoard"})
    assert net.already_running("kafka-hoard", port) is True
    assert net.already_running("phileas-hoard", port) is False


def test_already_running_false_when_port_is_free():
    assert net.already_running("kafka-hoard", net.free_port()) is False


@pytest.mark.parametrize("payload,status", [({"ok": True}, 200), ([1, 2], 200), (b"not json", 200), ({"service": "kafka-hoard"}, 500)])
def test_already_running_false_for_other_programs(server, payload, status):
    port = server(payload, status)
    assert net.already_running("kafka-hoard", port) is False


def test_already_running_false_for_a_port_that_is_not_http():
    with socket.socket() as raw:
        raw.bind(("127.0.0.1", 0))
        raw.listen()
        assert net.already_running("kafka-hoard", raw.getsockname()[1], timeout=0.3) is False


def test_already_running_ignores_proxy_environment(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    port = server({"service": "kafka-hoard"})
    assert net.already_running("kafka-hoard", port) is True


def test_health_url():
    assert net.health_url("http://127.0.0.1:5200") == "http://127.0.0.1:5200/api/health"
    assert net.health_url("http://127.0.0.1:5200/") == "http://127.0.0.1:5200/api/health"
    assert net.health_url("http://127.0.0.1:5200/api/health") == "http://127.0.0.1:5200/api/health"
    assert net.health_url("http://127.0.0.1:5200/health") == "http://127.0.0.1:5200/health"


def test_wait_healthy_comes_up_and_checks_service(server):
    port = server({"service": "kafka-hoard"})
    assert net.wait_healthy(f"http://127.0.0.1:{port}", "kafka-hoard", 2) is True
    assert net.wait_healthy(f"http://127.0.0.1:{port}", None, 2) is True
    assert net.wait_healthy(f"http://127.0.0.1:{port}/api/health", "kafka-hoard", 2) is True
    assert net.wait_healthy(f"http://127.0.0.1:{port}", "other", 0.3, poll=0.05) is False


def test_wait_healthy_times_out_and_polls_with_injected_clock():
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    assert net.wait_healthy(f"http://127.0.0.1:{net.free_port()}", "x", 1.0, poll=0.25, sleep=sleep, clock=lambda: t["now"]) is False
    assert sleeps == [0.25] * 4


def test_wait_healthy_when_the_server_starts_late(monkeypatch):
    answers = iter([None, None, {"service": "s"}])
    monkeypatch.setattr(net, "fetch_health", lambda url, timeout=1.0: next(answers))
    assert net.wait_healthy("http://x", "s", 5, poll=0, sleep=lambda s: None) is True


def test_open_in_browser(monkeypatch):
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(net.sys, "platform", "linux")
    assert net.open_in_browser("http://127.0.0.1:5200") is True and opened == ["http://127.0.0.1:5200"]
    assert net.open_in_browser("javascript:alert(1)") is False and net.open_in_browser("calc.exe") is False and net.open_in_browser(None) is False
    assert opened == ["http://127.0.0.1:5200"]
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no browser")))
    assert net.open_in_browser("http://x") is False


def test_open_in_browser_uses_startfile_on_windows(monkeypatch):
    started = []
    monkeypatch.setattr(net.sys, "platform", "win32")
    monkeypatch.setattr(net.os, "startfile", lambda url: started.append(url), raising=False)
    assert net.open_in_browser("http://127.0.0.1:5200") is True and started == ["http://127.0.0.1:5200"]
