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
        assert queued["frame"] == 24
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


def test_registered_frame_uses_order_model_and_reports_missing_model(tmp_path):
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
        for name in ("frame.png", "relief.glb", "pose.glb", "second.glb"):
            (tmp_path / name).write_bytes(name.encode())
        still = call("piece_add", {**args, "title": "Still", "kind": "image", "path": "frame.png"})["piece"]
        relief = call("piece_add", {**args, "title": "Relief", "kind": "mesh", "path": "relief.glb"})["piece"]
        request = {**args, "frame_piece": still["id"], "seconds": 0.625}
        assert call("draft_figure_request", request, 400)["error_code"] == "articulated_model_required"
        assert call("draft_figure_request", {**request, "model_piece": relief["id"]}, 400)["error_code"] == "articulated_model_required"
        model = call("piece_add", {**args, "title": "Rigged", "kind": "rigged_model", "path": "pose.glb"})["piece"]
        queued = call("draft_figure_request", request)
        assert queued["freeze_arguments"] == {"source": model["stored"], "source_sha256": model["sha256"],
                                              "frame": 15, "fps": 24,
                                              "print_size_mm": 216, "sheet_wall_mm": 2.0}
        assert queued["derives_from"] == [{"piece": model["id"], "role": "material"},
                                         {"piece": still["id"], "role": "reference"}]
        assert call("draft_figure_request", request)["freeze_arguments"] == queued["freeze_arguments"]
        second = call("piece_add", {**args, "title": "Second", "kind": "rigged_model", "path": "second.glb"})["piece"]
        assert call("draft_figure_request", request, 400)["error_code"] == "articulated_model_ambiguous"
        assert call("draft_figure_request", {**request, "model_piece": second["id"]})["derives_from"][0]["piece"] == second["id"]
    finally:
        server.shutdown()
        server.server_close()
