"""Exercise service-based gateway authorization without creating sockets."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from hoard_link.hub import stream_gateway

JOB = "a" * 32
VOICE_STREAM = f"voice/jobs/{JOB}/stream"
VOICE_CANCEL = f"voice/jobs/{JOB}/cancel"


@pytest.fixture
def dispatch(monkeypatch):
    owners = {
        "tts": "synthesis-fixture",
        "dictation": "dictation-fixture",
        "stt": "recognition-fixture",
    }
    apps = {
        key: SimpleNamespace(id=key, family={}, url="http://127.0.0.1:18817", token_file=None,
                             expect_service="different-runtime-name")
        for key in (*owners.values(), "pretender")
    }
    apps["pretender"].expect_service = "boltagon-hoard"
    hub = SimpleNamespace(get=apps.get, token_owner=lambda _token: "consumer",
                          config=SimpleNamespace(isolated=False), automation_blocked=lambda _id: False,
                          events=SimpleNamespace())
    handler = SimpleNamespace(hub=hub, _bearer=lambda: "caller-token", headers={},
                              server=SimpleNamespace(server_port=18816))
    monkeypatch.setattr(stream_gateway, "OWNERS", owners)
    monkeypatch.setattr(stream_gateway.contract, "read_token", lambda _: "provider-token")
    monkeypatch.setattr(stream_gateway, "_error", lambda _handler, status, code: (status, code))

    def relay(_handler, origin, selected, token, _trace):
        assert origin == ("127.0.0.1", 18817) and token == "provider-token"
        route, target, cancel = selected
        delivered.append((route.method, target, route.body, route.response, cancel))

    delivered = []
    monkeypatch.setattr(stream_gateway, "_relay", relay)

    def call(app_id, suffix):
        url = urlsplit(f"/api/apps/{app_id}/stream/{suffix}")
        result = stream_gateway.handle(handler, stream_gateway.match(url.path), url)
        return result if result is not None else delivered.pop()

    return call, apps


@pytest.mark.parametrize(("app", "suffix", "expected"), [
    ("synthesis-fixture", "voice/speak", ("POST", "/api/voice/speak", "json", "json", None)),
    ("synthesis-fixture", VOICE_STREAM,
     ("GET", f"/api/voice/jobs/{JOB}/stream", "", "ndjson", f"/api/voice/jobs/{JOB}")),
    ("synthesis-fixture", VOICE_CANCEL, ("DELETE", f"/api/voice/jobs/{JOB}", "", "json", None)),
    ("dictation-fixture", "dictation/understand", ("POST", "/api/dictation/understand", "audio", "json", None)),
    ("synthesis-fixture", "dictation/understand", (404, "route_not_allowed")),
    ("dictation-fixture", "voice/speak", (404, "route_not_allowed")),
    ("recognition-fixture", "dictation/understand", (404, "route_not_allowed")),
    ("pretender", "voice/speak", (404, "route_not_allowed")),
    ("synthesis-fixture", "voice/settings", (404, "route_not_allowed")),
    ("synthesis-fixture", "voice/jobs/not-an-id/stream", (404, "route_not_allowed")),
])
def test_compatibility_routes_follow_the_registered_owner(dispatch, app, suffix, expected):
    call, _ = dispatch
    assert call(app, suffix) == expected


def test_explicit_manifest_can_keep_compatibility_routes_disabled(dispatch):
    call, apps = dispatch
    apps["synthesis-fixture"].family["stream_routes"] = []
    assert call("synthesis-fixture", "voice/speak") == (404, "route_not_allowed")
