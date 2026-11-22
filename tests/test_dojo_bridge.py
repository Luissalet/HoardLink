import json
from types import SimpleNamespace

from hoard_link.hub import dojo, tools


def test_bridge_picks_active_placement_and_forwards_verified_transport_identity(monkeypatch):
    remote = "https://spark.example.test"
    state = {"location": "sparks", "urls": {"faustus": remote}}
    hub = SimpleNamespace(
        token="test-service-token", placement=SimpleNamespace(state=lambda: state),
        config=SimpleNamespace(person_profile={"id": "operator"}, faustus_urls=["http://127.0.0.1:7000"]),
    )
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self):
            return '{"record":{"teacher_id":"teacher-one"},"undo":"token"}'

    class Opener:
        def open(self, request, timeout):
            requests.append((request.full_url, request.get_header("X-agent-id"),
                             request.get_header("Authorization"), request.get_method(), request.data))
            return Response()

    monkeypatch.setattr(dojo, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(dojo.where, "reachable", lambda urls: urls[0])
    tool = next(tool for tool in tools.all_tools() if tool["name"] == "hub_dojo")
    assert {"compare", "exam_key"} <= set(tool["inputSchema"]["properties"]["operation"]["enum"])
    arguments = {"operation": "mark", "arguments": {"teacher_id": "forged"}}
    assert dojo.call(hub, arguments, "teacher-one")["undo"] == "token"
    auth = ("teacher-one", "Bearer test-service-token")
    assert requests[0][:4] == (remote + "/api/dojo/marks", *auth, "POST")
    exam_id = "e" * 32
    pair = {"exam_id": exam_id, "attempt_ids": ["a" * 32, "b" * 32]}
    dojo.call(hub, {"operation": "compare", "arguments": pair}, "teacher-one")
    assert requests[1][:4] == (remote + "/api/dojo/compare", *auth, "POST")
    assert json.loads(requests[1][4]) == pair
    dojo.call(hub, {"operation": "exam_key", "arguments": {"exam_id": exam_id}}, "teacher-one")
    assert requests[2] == (f"{remote}/api/dojo/exams/{exam_id}/key", *auth, "GET", None)
    for invalid in ({}, {"exam_id": "../key"}, {"exam_id": 1}):
        assert dojo.call(hub, {"operation": "exam_key", "arguments": invalid}, "teacher-one")["ok"] is False
    assert dojo.call(hub, {"operation": "apply"}, "teacher-one")["ok"] is False
    assert dojo.call(hub, {"operation": "list"}, "operator")["ok"] is False
    assert dojo.call(hub, {"operation": "attempt", "arguments": {"attempt_id": "../marks"}}, "teacher-one")["ok"] is False
    state["location"] = "pc"
    assert dojo.call(hub, {"operation": "list"}, "teacher-one")["undo"] == "token"
    assert requests[-1][0] == "http://127.0.0.1:7000/api/dojo"
    state["job"] = {"running": True}
    assert dojo.call(hub, {"operation": "list"}, "teacher-one")["ok"] is False
    assert len(requests) == 4
