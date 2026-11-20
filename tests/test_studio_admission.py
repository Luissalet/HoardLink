from types import SimpleNamespace

import pytest

from hoard_link.hub import order_budget
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.agora_console import AgoraConsoleFacet
from hoard_link.hub.work_admission import WorkQueue, TTL_SECONDS
from hoard_link.hub.worker_capabilities import candidates


def test_admission_is_atomic_prioritized_global_and_releases():
    now = [0]
    queue = WorkQueue(clock=lambda: now[0])

    def request(agent, priority="normal", **resources):
        return queue.manage({"action": "request", "agent": agent, "resources": resources, "priority": priority})

    def poll(agent, ticket):
        return queue.manage({"action": "poll", "agent": agent, "ticket": ticket["ticket"]})

    first = request("codex-one", suite=1, cpu=1)
    second = request("claude-two", suite=1, cpu=1)
    critical = request("codex-three", "high", suite=1, ram=2)
    other = request("codex-four", worker=1)
    assert [row["state"] for row in [first, second, critical, other]] == ["running", "queued", "queued", "running"]
    assert not poll("someone-else", critical)["ok"]
    assert queue.manage(first["undo"])["state"] == "released"
    assert poll("codex-three", critical)["state"] == "running"
    assert poll("claude-two", second)["state"] == "queued"
    now[0] += TTL_SECONDS - 1
    poll("claude-two", second)
    now[0] += 2
    assert poll("claude-two", second)["state"] == "running"


def test_ticket_retry_reuses_the_same_reservation():
    queue = WorkQueue()
    args = {"action": "request", "agent": "worker-one", "key": "tests-1", "resources": {"suite": 1}}
    first = queue.manage(args)
    assert queue.manage(args)["ticket"] == first["ticket"]
    assert queue.manage({**args, "priority": "high"})["status"] == 409


@pytest.fixture
def order(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    order_budget.setup(agora)
    order = agora.order_create({"agent": "maker", "title": "Cartographer", "words": "Try a draft",
                                "template": "visual"})["order"]
    key = agora.plan_set({"agent": "maker", "order_id": order["id"], "jobs": [
        {"key": "relay", "title": "Relay", "station": "concept", "produces": ["text"]},
    ]})["keys"]["relay"]
    yield agora, {"agent": "maker", "order_id": order["id"], "task_id": key}
    agora.close()


def test_failed_relay_holds_at_gate_and_undo_leaves_no_gate(order, tmp_path):
    agora, args = order
    limits = {**args, "retries": 1, "returns": 4, "gpu_seconds": 120}
    order_budget.configure(agora, limits)
    draft = tmp_path / "draft.txt"
    draft.write_text("a piece that can be inspected", encoding="utf-8")
    piece = agora.piece_add({**args, "kind": "text", "title": "Draft", "path": str(draft)})["piece"]
    for key in ("first", "second"):
        begun = order_budget.attempt(agora, {**args, "action": "begin", "key": key, "gpu_seconds": 12})
        assert begun["attempt"]["result"] == "running"
        again = order_budget.attempt(agora, {**args, "action": "begin", "key": key, "gpu_seconds": 12})
        assert again["existing"] and again["attempt"]["id"] == begun["attempt"]["id"]
        result = order_budget.attempt(agora, {**args, "action": "fail", "key": key, "gpu_seconds": 10})
    assert result["hold"] == "retries"
    gate, = agora.orders({})["waiting"]
    assert gate["piece_ids"] == [piece["id"]]
    assert order_budget.attempt(agora, {**args, "action": "begin", "key": "third"})["state"] == "waiting"
    with pytest.raises(AgoraError, match="only the person"):
        agora.gate_decide({"agent": "maker", "gate_id": gate["id"], "decision": "keep"})
    data = order_budget.metrics(agora, {})
    assert data["stations"][0]["failed_gpu_seconds"] == 20
    assert data["holds"][0]["reason"] == "retries"
    agora.order_undo({"agent": "maker", "token": result["undo"]})
    assert not agora.orders({})["waiting"]
    assert order_budget.metrics(agora, {})["stations"][0]["failed"] == 1


def test_gpu_reservation_and_first_pass_counts(order, tmp_path):
    agora, args = order
    order_budget.configure(agora, {**args, "retries": 4, "returns": 4, "gpu_seconds": 10})
    draft = tmp_path / "draft.txt"
    draft.write_text("concept", encoding="utf-8")
    piece = agora.piece_add({**args, "kind": "text", "title": "Draft", "path": str(draft)})["piece"]
    over = order_budget.attempt(agora, {**args, "action": "begin", "key": "oversize", "gpu_seconds": 11})
    assert over["hold"] == "gpu"
    gate, = agora.orders({})["waiting"]
    agora.gate_decide({"agent": "luis", "_person": True, "gate_id": gate["id"], "decision": "keep"})
    with pytest.raises(AgoraError, match="has already decided"):
        agora.order_undo({"agent": "maker", "token": over["undo"]})
    chain, = order_budget.metrics(agora, {})["chains"]
    assert chain["orders_kept_first"] == chain["pieces_kept_first"] == 1
    assert chain["chain"] == "visual" and not order_budget.metrics(agora, {})["stations"]


def test_return_reaches_budget_in_native_piece_defect(order, tmp_path):
    agora, args = order
    order_budget.configure(agora, {**args, "retries": 4, "returns": 1, "gpu_seconds": 10})
    draft = tmp_path / "draft.txt"
    draft.write_text("concept", encoding="utf-8")
    piece = agora.piece_add({**args, "kind": "text", "title": "Draft", "path": str(draft)})["piece"]
    result = agora.piece_defect({**args, "piece_id": piece["id"], "note": "Missing caption"})
    assert order_budget.metrics(agora, {})["holds"][0]["reason"] == "returns"
    assert agora.orders({})["waiting"][0]["piece_ids"] == [piece["id"]]
    agora.order_undo({"agent": "maker", "token": result["undo"]})
    assert not agora.orders({})["waiting"]


def test_capability_ignores_an_exhausted_provider_and_requires_vision():
    def provider(name, quota):
        return {"provider_id": name, "pools": [{"windows": [{"measurement": "measured", "remaining_percent": quota}]}]}

    def model(name, modalities):
        return {"id": name, "input_modalities": modalities, "efforts": ["low", "high"]}

    console = SimpleNamespace(
        _agora=lambda: SimpleNamespace(usage=lambda: {"providers": [provider("claude", 0), provider("codex", 80)]}),
        _provider_data=lambda: {
            "claude": {"state": "available", "catalog": [model("sonnet", ["image"])]},
            "codex": {"state": "available", "catalog": [model("text", ["text"]), model("vision", ["image"])]},
        },
    )
    assert candidates(console, "vision_strict") == [{"leader_id": "codex", "model": "vision", "effort": "high"}]
    assert candidates(console, "reasoning_high")[0]["leader_id"] == "codex"


def test_worker_create_tries_the_next_provider_on_confirmed_unavailability(monkeypatch):
    from hoard_link.hub import worker_capabilities

    attempts = []
    monkeypatch.setattr(worker_capabilities, "candidates", lambda *args, **kwargs: [
        {"leader_id": "claude", "model": "sonnet", "effort": "high"},
        {"leader_id": "codex", "model": "sol", "effort": "high"},
    ])

    def create(args, actor, reserved, worker_id):
        attempts.append((args["leader_id"], worker_id))
        return {"ok": True, "worker": {"state": "unavailable" if len(attempts) == 1 else "running"}}

    console = SimpleNamespace(_create_worker_now=create)
    reply = AgoraConsoleFacet._create_worker(console, {"capability": "reasoning_high", "task": "Compile the brief"})
    assert reply["worker"]["state"] == "running"
    assert [provider for provider, _ in attempts] == ["claude", "codex"]
    assert attempts[0][1] != attempts[1][1]
