"""Scoped production reads through the Hub's normal MCP-to-HTTP path."""

from contextlib import ExitStack, closing
import json
from threading import Thread

import pytest

from hoard_link.hub import agora_orders, mcp
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server

from .conftest import free_port


def seed(agora):
    order = agora.order_create({"agent": "producer", "title": "Synthetic order", "words": "Brief. " * 300})["order"]
    jobs = agora.plan_set({
        "agent": "producer", "order_id": order["id"],
        "jobs": [{"key": "own", "title": "Own job", "body": "Instructions for the worker.", "check": "Check its piece."}]
        + [{"key": f"peer-{i}", "title": f"Peer {i}", "body": "Peer private detail.", "check": "Peer check. " * 115}
           for i in range(39)],
    })["keys"]
    return order["id"], jobs["own"], jobs["peer-0"]


def test_standalone_and_foreign_job_ids_get_no_own_detail(tmp_path):
    with closing(Agora(tmp_path / "agora.db")) as agora:
        oid, own, _ = seed(agora)
        other = agora.order_create({"agent": "producer", "title": "Second order"})["order"]["id"]
        tid = agora.plan_set({"agent": "producer", "order_id": other,
                              "jobs": [{"key": "other", "title": "Other job"}]})["keys"]["other"]
        for header in ("0", "bad", "9999999999999999999", str(tid)):
            args = agora_orders.order_read_arguments({"order_id": oid}, header)
            plan = agora.order(args)["order"]["plan"]
            assert plan[0] == {"task_id": own, "title": "Own job", "status": "open"}
            assert all(set(peer) == {"task_id", "title", "status"} for peer in plan)
        with pytest.raises(AgoraError) as error:
            agora.order({"order_id": oid, "job": tid})
        assert error.value.status == 404


def test_oversized_own_job_can_be_requested_in_full(tmp_path):
    with closing(Agora(tmp_path / "agora.db")) as agora:
        oid, own, _ = seed(agora)
        long_note = "🦉" * 3900
        agora.db.execute("UPDATE tasks SET body=? WHERE id=?", (long_note, own))
        agora.db.execute("UPDATE agora_orders SET words=? WHERE id=?", ("\x01" * 1200, oid))
        args = agora_orders.order_read_arguments({"order_id": oid}, str(own))
        result = agora.order(args)
        own_detail = result["order"]["plan"][0]
        assert own_detail["task_id"] == own and own_detail["detail_truncated"] is True
        assert own_detail["task"]["body"].startswith("🦉")
        assert len(json.dumps(mcp._text(result), ensure_ascii=False).encode("utf-8")) < 8_000
        assert agora.order({"order_id": oid, "job": own})["job"]["task"]["body"] == long_note


def test_scoped_order_over_mcp_and_http_stays_under_eight_kilobytes(tmp_path, monkeypatch, record_property):
    config = HubConfig.load(env={"HOARD_HUB_PORT": str(free_port()), "HOARD_HUB_DATA_DIR": str(tmp_path / "hub")})
    assert config.isolated
    with ExitStack() as stack:
        hub = Hub(config)
        stack.callback(hub.close)
        agora = hub.facet("agora").agora
        oid, own, peer = seed(agora)
        old = agora.order({"order_id": oid})
        server = stack.enter_context(make_server(hub))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(server.shutdown)
        monkeypatch.setenv("HOARD_HUB_URL", config.url)
        monkeypatch.setenv("HOARD_HUB_TOKEN_FILE", config.token_file)
        monkeypatch.setenv("HOARD_HUB_AUTOSTART", "0")
        monkeypatch.setenv("AGORA_SPACE", agora.space_id)
        monkeypatch.setenv(agora_orders.WORKER_ORDER_TASK_ENV, str(own))

        def call(arguments, name=agora_orders.ORDER_TOOL):
            reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
            text = reply["result"]["content"][0]["text"]
            assert reply["result"]["isError"] is False, text
            return json.loads(text), len(json.dumps(reply, ensure_ascii=False).encode("utf-8"))

        args = {"order_id": oid}
        compact, size = call({**args, "_worker_order_task": peer, "full": False})
        record_property("scoped_order_wire_bytes", size)
        assert size < 8_000
        out = compact["order"]
        assert out["summary"] is True and len(out["words"]) < len(old["order"]["words"])
        assert out["plan"][0]["task_id"] == own
        assert "Instructions for the worker." in out["plan"][0]["task"]["body"]
        assert out["plan"][0]["check_how"] == "Check its piece."
        assert out["plan"][1]["task_id"] == peer
        assert all(set(item) == {"task_id", "title", "status"} for item in out["plan"][1:])
        assert out["jobs_omitted"] == 40 - len(out["plan"])
        assert all(key not in out for key in ("piece_list", "gates", "decisions", "dossier"))

        full, full_size = call({**args, "full": True})
        record_property("full_order_wire_bytes", full_size)
        assert full == old and full_size > 50_000
        explicit, _ = call({**args, "job": peer})
        assert explicit["job"]["task_id"] == peer
        assert "Peer private detail." in explicit["job"]["task"]["body"]
        individual, _ = call({"task_id": own}, "hub_agora_job")
        assert individual == agora.job({"task_id": own})
        monkeypatch.delenv(agora_orders.WORKER_ORDER_TASK_ENV)
        unscoped, _ = call({**args, "_worker_order_task": own})
        assert unscoped == old
