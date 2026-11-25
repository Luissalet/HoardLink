"""Agent list queries over synthetic stores, without starting a Hub server."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from hoard_link.hub import agent_routes, mcp
from hoard_link.hub.agora import Agora, AgoraError, AgoraFacet
from hoard_link.hub.agora_list_query import MAX_LIMIT, TASK_FIELDS
from hoard_link.hub.config import HubConfig
from hoard_link.hub.facets import Request

@pytest.fixture
def agora(tmp_path):
    store = Agora(tmp_path / "agora.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def offline_hub(tmp_path):
    config = HubConfig(data_dir=str(tmp_path), roots=[], agent_watch={"enabled": False})
    hub = SimpleNamespace(config=config, facets=[])
    facet = AgoraFacet(hub)
    hub.facets.append(facet)
    try:
        yield hub, facet
    finally:
        facet.close()


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def seed_task(store, **fields):
    when = epoch("2026-11-23T12:00:00Z")
    return store.db.insert("tasks", {"space_id": store.space_id, "title": "Map draft", "status": "approved",
                                    "priority": 2, "repo": "Mapping", "kind": "feature", "owner": "fixture-worker",
                                    "created": when, "updated": when, "body": "Full task text", **fields})


def test_projected_daily_list_through_agent_mcp(offline_hub, monkeypatch):
    hub, facet = offline_hub
    root = facet.agora
    root.space_create({"agent": "fixture-worker", "id": "studio-test", "title": "Synthetic studio"})
    space = root.for_space("studio-test")
    ids = [seed_task(space, title=f"Task {index:02}", body="history " * 450) for index in range(16)]
    for index in range(330):
        seed_task(space, body="history " * 450, status="done" if index % 2 else "approved",
                  updated=epoch("2026-11-24T00:00:00Z"))
    seed_task(root)
    legacy = root.tasks({"space_id": "studio-test", "limit": 500})
    assert len(legacy["tasks"]) == 346
    assert len(json.dumps(legacy).encode()) > 1_198_000

    def unexpected_full_task(*args, **kwargs):
        pytest.fail("projecting metadata must not build complete task records")

    def request(method, path, body, timeout):
        assert (method, path) == ("POST", "/api/agent/call")
        response, status = agent_routes.invoke(hub, body["tool"], body["arguments"])
        return status, response

    monkeypatch.setattr(Agora, "_task_dict", unexpected_full_task)
    monkeypatch.setattr(mcp, "_ensure_hub", lambda: True)
    monkeypatch.setattr(mcp, "_request", request)
    monkeypatch.setattr(mcp, "request_context", lambda: None)
    monkeypatch.delenv("AGORA_SPACE", raising=False)
    schema = next(tool for tool in facet.tools() if tool["name"] == "hub_agora_tasks")
    assert schema["annotations"]["readOnlyHint"]
    args = {"space_id": "studio-test", "status": "approved", "since": "2026-11-22T23:00:00Z",
            "until": "2026-11-23T23:00:00Z", "fields": ["id", "title", "status", "repo", "updated"]}
    assert set(args) <= set(schema["inputSchema"]["properties"])
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "hub_agora_tasks", "arguments": args}})["result"]
    assert not reply["isError"]
    payload = reply["content"][0]["text"]
    assert len(payload.encode()) < 4_000
    page = json.loads(payload)
    assert [item["id"] for item in page["tasks"]] == ids[::-1]
    assert all(set(item) == set(args["fields"]) for item in page["tasks"])
    assert page["space_id"] == "studio-test"
    assert page["next_cursor"] is None


def test_date_field_bounds_with_offset_nulls_and_filters(agora):
    start = epoch("2026-11-22T23:00:00Z")
    end = epoch("2026-11-23T23:00:00Z")
    first = seed_task(agora, status="done", closed_at=start, created=start - 1, updated=end)
    second = seed_task(agora, closed_at=None, created=start, updated=start)
    seed_task(agora, status="done", closed_at=end, created=end, updated=end, repo="Other")
    seed_task(agora, status="open", closed_at=None, created=start - 1, updated=start - 0.001)
    query = {"since": "2026-11-23T00:00:00+01:00", "until": "2026-11-23T18:00:00-05:00"}

    assert [row["id"] for row in agora.tasks(query)["tasks"]] == [second]
    assert tuple(agora.tasks(query)["tasks"][0]) == TASK_FIELDS
    for field, expected in (("created", second), ("closed_at", first)):
        rows = agora.tasks({**query, "date_field": field})["tasks"]
        assert [row["id"] for row in rows] == [expected]

    count = agora.tasks({**query, "status": "active", "owner": "FIXTURE-WORKER", "repo": "mapping",
                         "kind": "FEATURE", "count_only": True, "cursor": "id:1", "limit": 1})
    assert count == {"ok": True, "count": 1, "space_id": agora.space_id}
    assert agora.tasks({"since": "2026-11-23", "until": "2026-11-24", "count_only": True})["count"] == 2


def test_projected_id_cursor_survives_inserts_and_sort_key_changes(agora):
    oldest = seed_task(agora, priority=0, status="open")
    middle = seed_task(agora, priority=1, status="open")
    latest = seed_task(agora, priority=0, status="open")
    legacy = agora.tasks({"limit": 1})
    assert legacy["tasks"][0]["id"] == latest
    assert legacy["tasks"][0]["body"] == "Full task text"
    assert legacy["next_cursor"] == "1"
    assert agora.tasks({"limit": 1, "cursor": "1"})["tasks"][0]["id"] == oldest

    query = {"compact": True, "limit": 1}
    first = agora.tasks(query)
    assert first["next_cursor"] == f"id:{latest}"
    seed_task(agora, priority=0)
    agora.db.execute("UPDATE tasks SET updated=updated+1, priority=0 WHERE id=?", (middle,))
    second = agora.tasks({**query, "cursor": first["next_cursor"]})
    third = agora.tasks({**query, "cursor": second["next_cursor"]})
    assert [entry["tasks"][0]["id"] for entry in (first, second, third)] == [latest, middle, oldest]
    assert third["next_cursor"] is None


def test_row_and_byte_limits_keep_the_cursor_inside_the_agent_reply(agora):
    with agora.db.tx():
        ids = [seed_task(agora, body="á" * 2300) for _ in range(MAX_LIMIT + 7)]
    page = agora.tasks({"fields": ["id"], "limit": 999})
    assert len(page["tasks"]) == MAX_LIMIT
    assert page["next_cursor"] == f"id:{ids[-MAX_LIMIT]}"
    page = agora.tasks({"fields": ["id", "body"]})
    assert len(json.dumps(page).encode()) < 24_000
    assert page["tasks"] == [{"id": ids[-1], "body": "á" * 2300}]
    assert agora.tasks({"fields": ["id", "body"], "cursor": page["next_cursor"]})["tasks"][0]["id"] == ids[-2]

    large_id = seed_task(agora, body="x" * 30_000)
    with pytest.raises(AgoraError, match="Request fewer fields") as err:
        agora.tasks({"fields": ["body"]})
    assert err.value.status == 413
    assert err.value.extra["item_id"] == large_id
    assert agora.tasks({"fields": ["id"]})["tasks"][0] == {"id": large_id}


def test_explicit_normalized_fields_match_full_task(agora):
    full = agora.task_add({"agent": "fixture-worker", "title": "Map draft", "repo": "Mapping",
                           "paths": ["map.txt"], "body": "Full task text"})["task"]
    fields = ["paths", "reviewed", "activity", "body", "etag", "checkpoint_summary"]
    result = agora.tasks({"fields": fields})["tasks"]
    assert result == [{field: full[field] for field in fields}]


@pytest.mark.parametrize(("args", "message"), [
    ({"since": "2026-11-23T00:00:00"}, "since"),
    ({"since": "2026-11-23T00:00:00+01:70"}, "since"),
    ({"until": "2026-02-30"}, "until"),
    ({"since": "2026-11-24", "until": "2026-11-23"}, "since"),
    ({"date_field": "updated DESC"}, "date_field"),
    ({"fields": []}, "fields"),
    ({"fields": ["id FROM threads"]}, "Unknown list field"),
    ({"compact": True, "cursor": "id:9999999999999999999"}, "cursor"),
    ({"compact": True, "limit": 1.5}, "limit"),
])
def test_invalid_projected_queries(agora, args, message):
    with pytest.raises(AgoraError, match=message):
        agora.tasks(args)


def test_http_route_arguments_use_the_same_projection(offline_hub):
    _, facet = offline_hub
    tid = seed_task(facet.agora)
    query = {"fields": ["id, title"], "since": ["2026-11-23"], "until": ["2026-11-24"], "limit": ["1"]}

    def get(params):
        return facet.get(Request("GET", "/api/agora/tasks", params, {}, lambda: "hub", lambda: True))

    assert get(query)["tasks"] == [{"id": tid, "title": "Map draft"}]
    assert get({**query, "count_only": ["true"]})["count"] == 1
    assert get({**query, "since": ["invalid"]})["status"] == 400


def test_decision_pages_keep_both_sources_and_legacy_receipts(offline_hub):
    hub, facet = offline_hub
    agora = facet.agora
    ts = epoch("2026-11-23T12:00:00Z")
    with agora.db.tx():
        thread = agora.db.insert("threads", {"space_id": agora.space_id, "title": "Draft approved", "kind": "decision",
                                             "status": "resolved", "resolution": "Use the map", "resolved_by": "fixture",
                                             "created": ts - 86_400, "updated": ts})
        agora.db.insert("threads", {"space_id": agora.space_id, "title": "Unresolved", "kind": "decision",
                                    "status": "open", "created": ts, "updated": ts})
        production = agora.db.insert("agora_order_decisions", {
            "space_id": agora.space_id, "order_id": 1, "gate_id": 1, "piece_id": 1, "decision": "keep",
            "note": "Retain the missing corner", "created": ts, "situation": '{"decided_by":"fixture-worker"}',
        })

    legacy = agora.decisions({"limit": 1})
    assert legacy["decisions"][0]["resolution"] == "Use the map"
    assert legacy["production_decisions"][0]["note"] == "Retain the missing corner"
    assert "next_cursor" not in legacy

    query = {"since": "2026-11-23", "until": "2026-11-24"}
    first = agora.decisions({**query, "limit": 1})
    assert first["next_cursor"] == f"thread:{thread}"
    assert len(first["decisions"]) == 1 and first["production_decisions"] == []
    assert "resolution" not in first["decisions"][0]
    second = agora.decisions({**query, "limit": 1, "cursor": first["next_cursor"]})
    assert second["decisions"] == [] and second["production_decisions"][0]["id"] == production
    assert second["next_cursor"] is None
    assert "note" not in second["production_decisions"][0]
    assert agora.decisions({**query, "count_only": True})["count"] == 2

    fields = ["id", "note", "decided_by", "situation"]
    created = agora.decisions({**query, "date_field": "created", "fields": fields})
    assert created["decisions"] == []
    assert created["production_decisions"] == [{name: legacy["production_decisions"][0][name] for name in fields}]
    response, status = agent_routes.invoke(hub, "hub_agora_decisions", {**query, "count_only": True})
    assert status == 200 and response["result"]["count"] == 2
