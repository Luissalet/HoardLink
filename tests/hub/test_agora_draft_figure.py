from pathlib import Path

from ._hub_fakes import http, make_hub, serve


def test_paused_take_queues_pinned_figure_without_deciding_and_can_undo(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    agent = {"agent": "codex-worker"}

    def call(name, args, status=200):
        code, body = http(hub.config.url + "/api/agent/tools/hub_agora_" + name, args, headers)
        assert code == status, body
        return body["result"]

    try:
        order = call("order_create", {**agent, "title": "Map pass", "folder": str(tmp_path)})["order"]
        order_args = {**agent, "order_id": order["id"]}
        draft = call("draft_pass_open", {**order_args, "variants": [{"key": "pass", "title": "Pass"}]})["draft_pass"]
        variant_id = draft["variants"][0]["id"]
        pieces = {}
        for kind, anchor, file in [("rigged_model", "blocking", "blocking.glb"), ("video", "animatic", "take.mp4")]:
            (tmp_path / file).write_bytes(file.encode())
            pieces[anchor] = call("piece_add", {**order_args, "title": file, "kind": kind, "path": file})["piece"]
        call("draft_variant_set", {
            **agent, "variant_id": variant_id, "status": "ready",
            "pieces": [{"piece": piece["id"], "anchor": anchor} for anchor, piece in pieces.items()],
        })
        shown = call("draft_gate_open", {**agent, "pass_id": draft["id"]})["draft_pass"]
        request = {**agent, "variant_id": variant_id, "take_piece": pieces["animatic"]["id"], "seconds": 1}
        stale = call("draft_figure_request", {**request, "signature": "stale"}, 400)
        assert stale["status"] == 409 and stale["error_code"] == "variant_changed"
        request["signature"] = shown["variants"][0]["signature"]
        queued = call("draft_figure_request", request)
        assert queued["frame"] == 24 and queued["test_pose"]
        duplicate = call("draft_figure_request", request)
        assert duplicate["existing"] and duplicate["task_id"] == queued["task_id"]
        job = call("job", {"task_id": queued["task_id"]})["job"]
        assert {p["id"] for p in job["input_pieces"]} == {p["id"] for p in pieces.values()}
        assert call("draft_passes", {"order_id": order["id"]})["draft_passes"] == [shown]
        call("order_undo", {**agent, "token": queued["undo"]})
        assert call("draft_figure_request", request)["task_id"] != queued["task_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_registered_frame_follows_take_or_waits_for_model_selection(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    agent = {"agent": "codex-worker"}

    def call(name, args, status=200):
        code, body = http(hub.config.url + "/api/agent/tools/hub_agora_" + name, args, headers)
        assert code == status, body
        return body["result"]

    try:
        order = call("order_create", {**agent, "title": "Map pass", "folder": str(tmp_path)})["order"]
        args = {**agent, "order_id": order["id"]}

        def add(file, kind, parents=(), **kwargs):
            (tmp_path / file).write_bytes(file.encode())
            return call("piece_add", {**args, "title": file, "kind": kind, "path": file,
                                      "derives_from": list(parents), **kwargs})["piece"]

        other_take = add("other.mp4", "video")
        still = add("frame.png", "image", [other_take["id"]])
        relief = add("relief.glb", "mesh")
        request = {**args, "frame_piece": still["id"], "seconds": 0.625}
        missing = call("draft_figure_request", request)
        assert missing["status"] == "needs_model" and missing["model_choices"] == []
        assert call("draft_figure_request", {**request, "model_piece": relief["id"]}, 400)["error_code"] == "articulated_model_required"

        model = add("pose.glb", "blocking")
        needs_choice = call("draft_figure_request", request)
        assert needs_choice["status"] == "needs_model"
        assert needs_choice["model_choices"] == [{"id": model["id"], "title": "pose.glb", "revision": 1}]
        assert not call("order", args)["order"]["plan"]
        queued = call("draft_figure_request", {**request, "model_piece": model["id"]})
        assert queued["freeze_arguments"] == {"source": model["stored"], "source_sha256": model["sha256"],
                                              "frame": 15, "fps": 24,
                                              "print_size_mm": 216, "sheet_wall_mm": 2.0}
        assert queued["derives_from"] == [{"piece": model["id"], "role": "material"},
                                          {"piece": still["id"], "role": "reference"}]
        assert queued["test_pose"]
        assert (call("draft_figure_request", {**request, "model_piece": model["id"]})["task_id"]
                == queued["task_id"])

        second = add("second.glb", "rigged_model")
        ambiguous = call("draft_figure_request", request)
        assert ambiguous["status"] == "needs_model"
        assert [p["id"] for p in ambiguous["model_choices"]] == [model["id"], second["id"]]

        take = add("take.mp4", "video", [second["id"], {"piece": model["id"], "role": "reference"}])
        linked = add("linked.png", "image", [take["id"]])
        traced = call("draft_figure_request", {**request, "frame_piece": linked["id"]})
        assert traced["derives_from"][0] == {"piece": second["id"], "role": "material"}
        assert traced["freeze_arguments"]["source_sha256"] == second["sha256"]
        assert not traced["test_pose"]
        assert call("draft_figure_request", {**request, "frame_piece": linked["id"]})["task_id"] == traced["task_id"]

        posed = add("posed.glb", "posed-figure-mesh", queued["derives_from"], task_id=queued["task_id"])
        closed = add("closed.stl", "closed-print-mesh", [posed["id"]])
        coloured = add("figure.3mf", "coloured-3mf", [closed["id"]])
        actual = add("production.glb", "posed-figure-mesh", traced["derives_from"])
        assert all(p.get("test_pose") for p in (posed, closed, coloured))
        assert not actual.get("test_pose")
        call("gate_open", {**args, "title": "Figures", "piece_ids": [coloured["id"], actual["id"]]})
        decision = call("pending_decisions", {})["rows"][0]
        assert [p.get("test_pose", False) for p in decision["artifacts"]] == [True, False]
        assert call("order", args)["order"]["gates"][0]["decisions"] == []
    finally:
        server.shutdown()
        server.server_close()
