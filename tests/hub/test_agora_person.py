"""The configured person uses existing Ágora routes and keeps historical attribution."""

from pathlib import Path

import pytest

from hoard_link.hub import agora_liveness, tools
from hoard_link.hub.agora import Agora
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub

from ._hub_fakes import free_port, http, serve, ui_headers


PROFILE = {"id": "studio-owner", "name": "Studio Operator"}
AGENT = "qa-builder"


def test_adopts_human_account_without_rewriting_history(tmp_path):
    path = tmp_path / "agora.db"
    legacy = Agora(path, config=HubConfig(data_dir=str(tmp_path), person=PROFILE))
    historical = legacy.thread_open({"agent": PROFILE["id"], "_person": True, "title": "A previous conversation",
                                     "body": "Historical message"})["thread"]
    legacy.post({"agent": AGENT, "thread_id": historical["id"], "body": "Recorded mention",
                 "mentions": [PROFILE["id"]]})
    legacy.member_rank_set({"agent": PROFILE["id"], "_person": True, "member": AGENT, "rank": "lead"})
    before = [dict(row) for row in legacy.db.query("SELECT * FROM messages ORDER BY id")]
    legacy.close()
    assert not (tmp_path / "hub.json").exists()

    cfg = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)})
    upgraded = Agora(path, config=cfg)
    try:
        assert cfg.person_profile == PROFILE
        assert HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)}).person_profile == cfg.person_profile
        assert {m["id"]: m["rank"] for m in upgraded.ranks()["members"]}[AGENT] == "lead"
        assert [dict(row) for row in upgraded.db.query("SELECT * FROM messages ORDER BY id")] == before
    finally:
        upgraded.close()


@pytest.mark.parametrize("answer", ["Waiting for Studio Operator.", "Necesito que studio-owner decida.",
                                    "Bloqueado hasta la persona.", "Waiting for @person to decide."])
def test_liveness_recognises_configured_person_and_neutral_language(answer):
    result = agora_liveness.classify_turn(state="completed", exit_code=0, answer=answer, evidence={}, person=PROFILE)
    assert result["liveness"] == "blocked"


def test_configured_person_at_the_http_and_agent_boundaries(tmp_path):
    cfg = HubConfig(port=free_port(), data_dir=str(tmp_path), roots=[], icon_dirs=[], faustus_urls=[],
                    jobs_enabled=False, isolated_catalog=True, agent_watch={"enabled": False}, person=PROFILE)
    cfg.save()
    hub = Hub(cfg)
    ag = hub.facet("agora").agora
    ag.escalate_hook = None
    ag.mention_hook = None
    server = serve(hub)
    agent = {"Authorization": "Bearer " + Path(cfg.token_file).read_text(encoding="utf-8").strip()}

    def request(op, payload=None, headers=agent):
        return http(f"{cfg.url}/api/agora/{op}", payload, headers)

    try:
        page = ui_headers(hub)
        without_session = {"Sec-Fetch-Site": "same-origin", "Origin": cfg.url}
        assert request("person")[1]["person"] == PROFILE
        assert request("person", {"name": "Impostor"})[0] == 403
        assert request("person", {"name": "Unverified"}, without_session)[0] == 403
        assert request("person", {"name": " Production Owner ", "id": "ignored"}, page)[1]["person"] == {
            "id": PROFILE["id"], "name": "Production Owner"}
        assert HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)}).person_profile["name"] == "Production Owner"

        status, opened = request("thread_open", {"agent": AGENT, "title": "Need a direction", "kind": "question",
                                                 "body": "Which direction should the test use?"})
        assert status == 200
        tid = opened["thread"]["id"]
        forged = {"agent": PROFILE["id"], "_person": True, "thread_id": tid, "body": "Forged"}
        assert request("post", forged)[0] == 403
        assert tools.call(hub, "hub_agora_post", forged)["status"] == 403
        assert request("channel_post", {"agent": AGENT, "thread_id": tid,
                                        "body": f"@person @{PROFILE['id']} same recipient"})[1]["mentions"] == [PROFILE["id"]]
        assert request("escalate", {"agent": AGENT, "thread_id": tid, "question": "Choose the direction"})[1]["asked"] == [PROFILE["id"]]
        assert request("inbox?peek=1", headers=page)[1]["escalated"][0]["id"] == tid
        assert request("inbox?peek=1", headers=without_session)[1]["escalated"][0]["id"] == tid
        assert request("post", {"thread_id": tid, "body": "Unverified"}, without_session)[0] == 403
        assert request("post", {"thread_id": tid, "body": "Here is the direction", "mentions": [AGENT]}, page)[0] == 200
        assert request(f"threads/{tid}", headers=page)[1]["messages"][-1]["author"] == PROFILE["id"]

        rank = {"member": AGENT, "rank": "lead"}
        assert request("member_rank_set", {**rank, "agent": PROFILE["id"], "_person": True})[0] == 403
        assert request("member_rank_set", rank, without_session)[0] == 403
        assert request("member_rank_set", rank, page)[0] == 200
        assert ag.db.one("SELECT set_by FROM agora_ranks WHERE agent=?", (AGENT,))["set_by"] == PROFILE["id"]

        d = ag.debate_open({"agent": AGENT, "thread_id": tid})["debate"]["id"]
        legacy = "needs_luis"
        status, finding = request("debate_finding", {"agent": AGENT, "debate_id": d, "claim": "Consider this option",
                                                        "evidence": ["qa.txt"], "disposition": legacy})
        assert status == 200 and finding["finding"]["disposition"] == "needs_person"
        fid = finding["finding"]["id"]
        with ag.db.tx():
            ag.db.execute("UPDATE agora_debate_findings SET disposition=? WHERE id=?", (legacy, fid))
        view = request(f"debates/{d}")[1]
        assert view["findings"][0]["disposition"] == "needs_person"
        assert view["consensus"]["needs_person"] == [fid]
        from_tool = tools.call(hub, "hub_agora_debate_finding", {"agent": AGENT, "debate_id": d,
                              "claim": "A second option", "evidence": ["second.txt"], "disposition": legacy})
        assert from_tool["finding"]["disposition"] == "needs_person"

        order = ag.order_create({"agent": AGENT, "title": "Synthetic order", "folder": str(tmp_path)})["order"]
        (tmp_path / "fixture.txt").write_text("Synthetic piece", encoding="utf-8")
        piece = ag.piece_add({"agent": AGENT, "order_id": order["id"], "title": "Fixture", "kind": "text",
                              "path": "fixture.txt"})["piece"]
        ag.piece_check({"agent": AGENT, "piece_id": piece["id"], "note": "Reviewed the test piece."})
        gate = ag.gate_open({"agent": AGENT, "order_id": order["id"], "title": "Fixture gate",
                             "piece_ids": [piece["id"]]})["gate"]
        decision = {"gate_id": gate["id"], "decision": "keep"}
        assert request("gate_decide", {**decision, "agent": PROFILE["id"], "_person": True})[0] == 403
        assert request("gate_decide", decision, page)[0] == 200
        log = request("decisions", headers=page)[1]
        assert log["viewer"] == PROFILE["id"]
        assert log["production_decisions"][0]["decided_by"] == {"id": PROFILE["id"], "name": "Production Owner"}
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
