"""Receipts, type-linked skills and benchmark comparisons without inference."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hoard_link import skills
from hoard_link.hub import agent_routes, skill_bench, tools
from hoard_link.hub.agora import Agora
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.order_receipts import CONTEXT, OrderReceiptsFacet
from hoard_link.hub.production import ProductionFacet


@pytest.fixture
def orders(tmp_path, monkeypatch):
    ag = Agora(tmp_path / "agora.db")
    ag.space_create({"agent": "claude", "id": "estudio", "title": "Studio"})
    scoped = ag.for_space("estudio")
    order = scoped.order_create({"agent": "codex", "title": "Clay workshop", "words": "Design a website"})["order"]
    plan = scoped.plan_set({"agent": "codex", "order_id": order["id"], "jobs": [
        {"key": "design", "title": "Design", "station": "vitruvius", "produces": ["interface-brief"],
         "check": "Verify twelve accessibility rules"},
    ]})
    scope = {"order_id": order["id"], "task_id": plan["keys"]["design"], "space_id": "estudio"}
    app = SimpleNamespace(id="vitruvius", name="Design", family={"production": {"handoffs": [
        {"accepts": "production-brief", "produces": ["interface-brief", "other-output"], "skill": "legacy",
         "check": {"en": "Check the result", "es": "Comprueba el resultado"}},
    ]}})
    library = tmp_path / "library"
    facets = {"agora": SimpleNamespace(agora=ag)}
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path), faustus_dir=str(library)),
                          facet=facets.get, apps=[app])
    receipts = facets["order_receipts"] = OrderReceiptsFacet(hub)
    facets["production"] = ProductionFacet(hub)
    console = facets["agora_console"] = AgoraConsoleFacet(hub)
    calls = []

    def native(_hub, name, args):
        calls.append((name, args))
        payload = args["arguments"] if name == "hub_call_app" else args
        return {"ok": not payload.get("fail"), "result": {"count": payload.get("limit", 12)}, "ms": len(calls)}

    monkeypatch.setattr(tools, "call", native)
    yield receipts, scope, calls, console, library
    console.close()
    ag.close()


def _call(facet, scope, **arguments):
    return facet.call({**scope, "app": "vitruvius", "tool": "design_rules", "arguments": arguments})


def test_normal_proxy_and_hub_calls_share_receipts_and_keep_tool_arguments(orders):
    facet, scope, calls, _, _ = orders
    body, status = agent_routes.invoke(facet.hub, "hub_call_app", {CONTEXT: scope, "app": "vitruvius",
                                                                  "tool": "design_rules", "arguments": {"limit": 12}})
    assert status == 200
    assert body["result"]["result"]["count"] == 12
    receipt = facet.read({**scope, **body})["receipt"]
    assert receipt["app"] == "vitruvius" and receipt["tool"] == "design_rules"
    assert receipt["arguments"] == {"limit": 12}
    assert CONTEXT not in calls[0][1]

    broken, status = agent_routes.invoke(facet.hub, "hub_example", {CONTEXT: scope, "fail": True, "token": "secret"})
    assert status == 400
    receipt = facet.read({**scope, **broken})["receipt"]
    assert receipt["arguments"] == {"fail": True, "token": "[redacted]"}
    assert not receipt["ok"] and not receipt["reply"]["ok"]
    page = facet.list({**scope, "limit": 1})
    assert page["next_before"] == broken["receipt_id"]
    assert facet.list({**scope, "before": page["next_before"]})["receipts"][0]["id"] == body["receipt_id"]
    assert facet.undo(broken["receipt_undo"]["arguments"])["ok"]
    assert not facet.undo(broken["receipt_undo"]["arguments"])["ok"]
    refused, status = agent_routes.invoke(facet.hub, "hub_example", {CONTEXT: {**scope, "space_id": "other"}})
    assert status == 400 and refused["result"]["ok"] is False and len(calls) == 2


def test_draft_uses_job_types_and_references_successful_calls(orders):
    facet, scope, _, _, _ = orders
    ran = _call(facet, scope, area="a11y")
    _call(facet, scope, area="wrong")
    draft = facet.draft(scope)
    meta, body = skills.parse(draft["draft"]["text"])
    assert draft["draft"]["receipts"][0] == ran["receipt_id"]
    assert meta["accepts"] == ["production-brief"] and meta["produces"] == ["interface-brief"]
    assert meta["status"] == "draft" and "design_rules" in body and "Verify twelve accessibility rules" in body
    assert facet.undo(draft["undo"]["arguments"])["ok"]
    assert len(facet.list(scope)["receipts"]) == 2


def test_receipts_keep_distinct_ids_for_concurrent_calls(orders):
    facet, scope, _, _, _ = orders
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda limit: _call(facet, scope, limit=limit), range(8)))
    assert len({reply["receipt_id"] for reply in replies}) == 8
    assert {facet.read({**scope, **reply})["receipt"]["arguments"]["limit"] for reply in replies} == set(range(8))


def test_catalog_links_typed_skills_per_output_and_preserves_declared_skill(orders):
    facet, _, _, _, library = orders
    target = library / "guided-design"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\napps: [vitruvius]\naccepts: [production-brief]\n"
                                    "produces: [interface-brief]\n---\nDesign it.", encoding="utf-8")
    production = facet.hub.facet("production")
    good = production.catalog({"from_type": "production-brief", "to_type": "interface-brief"})["chains"][0][0]
    other = production.catalog({"from_type": "production-brief", "to_type": "other-output"})["chains"][0][0]
    assert good["skill"] == other["skill"] == "legacy"
    assert good["skills"] == ["guided-design"] and other["skills"] == []


@pytest.mark.parametrize(("sample", "expected"), [(12, "passed"), (7, "different"), (None, "failed")])
def test_benchmark_compares_the_workers_observed_reply(orders, monkeypatch, sample, expected):
    facet, scope, _, console, _ = orders
    baseline = _call(facet, scope, limit=12)
    draft = facet.draft(scope)["draft"]
    scoped = console.for_space(scope["space_id"])
    seen = {}

    def launch(args, actor, worker_id, benchmark_prompt):
        seen.update(args=args, prompt=benchmark_prompt)
        with scoped._db() as db:
            db.execute("INSERT INTO workers(id,parent_id,agent_id,model,effort,task,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,'running',1,1)",
                       (worker_id, "claude", "claude-worker", "haiku", "low", args["task"]))
        return {"ok": True}

    monkeypatch.setattr(scoped, "_create_worker", launch)
    started = skill_bench.start(facet, {**scope, "receipt_id": baseline["receipt_id"], "draft_id": draft["id"],
                                        "step": "Get accessibility rules", "inputs": {"area": "a11y"},
                                        "compare": ["/result/count"]})
    run = started["benchmarks"][0]
    assert run["state"] == "running"
    assert "area" in seen["prompt"] and '"count":12' not in seen["prompt"]
    assert seen["args"]["model"] == "haiku"
    if sample is not None:
        _call(facet, {"run_id": run["id"]}, limit=sample)
    with scoped._db() as db:
        db.execute("UPDATE workers SET state='completed' WHERE id=?", (run["id"],))
    checked = skill_bench.history(facet, {"slug": draft["slug"]})["benchmarks"][0]
    assert checked["state"] == expected
    if expected == "different":
        assert checked["result"]["differences"] == [{"path": "/result/count", "expected": 12, "actual": 7}]
    assert len(facet.list(scope)["receipts"]) == 1
    assert skill_bench.undo(facet, started["undo"]["arguments"])["ok"]
    assert skill_bench.history(facet, {"slug": draft["slug"]})["benchmarks"] == []


def test_comparison_distinguishes_missing_null_and_boolean():
    expected = {"values": [True, None], "a/b~c": 10}
    actual = {"values": [1], "a/b~c": 10}
    assert skill_bench.compare(expected, actual, ["/a~1b~0c", "/values/0", "/values/1"]) == [
        {"path": "/values/0", "expected": True, "actual": 1},
        {"path": "/values/1", "expected": None, "missing": True},
    ]


def test_benchmark_runner_uses_a_clean_directory_and_preserves_hub_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_bench, "gettempdir", lambda: str(tmp_path))
    command, directory, env = skill_bench.clean_worker(["claude", "--print"], "test only",
                                                       {"HOARD_HUB_URL": "http://127.0.0.1:8976"}, "run-id")
    assert command[-2] == "--" and "test only" not in command
    assert "--strict-mcp-config" in command and "--no-session-persistence" in command
    assert directory.parent == tmp_path and list(directory.iterdir()) == [directory / "benchmark.md"]
    assert (directory / "benchmark.md").read_text(encoding="utf-8") == "test only"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["HOARD_HUB_URL"].endswith("8976")
    assert env["HOARD_ORDER_RECEIPT"] == '{"run_id": "run-id"}'
