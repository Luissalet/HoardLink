from pathlib import Path

from ._hub_fakes import http, make_hub, serve, ui_headers


def test_take_and_frame_pin_model_material_or_propose_a_modelling_job(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    token = Path(hub.config.token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    agent = {"agent": "codex-worker"}

    def call(name, args, status=200):
        code, body = http(hub.config.url + "/api/agent/tools/hub_agora_" + name, {**agent, **args}, headers)
        assert code == status, body
        return body["result"]

    def add(order_id, path, kind, *parents):
        (tmp_path / path).write_bytes(path.encode())
        return call("piece_add", {"order_id": order_id, "title": path, "kind": kind, "path": path,
                                  "derives_from": list(parents)})["piece"]

    try:
        order = call("order_create", {"title": "Map pass", "folder": str(tmp_path)})["order"]
        oid = order["id"]
        model = add(oid, "model.glb", "articulated-mesh")["id"]
        unrelated = add(oid, "visual-reference.glb", "rigged_model")["id"]
        guide = add(oid, "guide.png", "render", model)["id"]
        take = add(oid, "take.mp4", "video-clip", guide, {"piece": unrelated, "role": "reference"})
        frame = add(oid, "frame.png", "image", take["id"])
        assert take["derivation"] == [{"piece": guide, "role": "material"},
                                      {"piece": unrelated, "role": "reference"},
                                      {"piece": model, "role": "material"}]
        assert frame["derives_from"] == [take["id"], model]
        req = {"order_id": oid, "frame_piece": frame["id"], "seconds": 0.625}
        figure = call("draft_figure_request", req)
        assert figure["frame"] == 15 and figure["derives_from"][0] == {"piece": model, "role": "material"}

        joined = add(oid, "joined.mp4", "video", model, unrelated)
        joined_frame = add(oid, "joined.png", "image", joined["id"])
        ambiguous = call("draft_figure_request", {**req, "frame_piece": joined_frame["id"]}, status=400)
        assert ambiguous["error_code"] == "articulated_model_ambiguous"

        flat_take = add(oid, "flat.mp4", "video", {"piece": unrelated, "role": "reference"})
        flat_frame = add(oid, "flat.png", "image", flat_take["id"])
        request = {"order_id": oid, "piece_id": flat_frame["id"]}
        assert flat_frame["missing_figure_model"] == {"piece_id": flat_frame["id"], "station": "vulcan", "task_id": None}
        result = call("draft_figure_request", {**req, "frame_piece": flat_frame["id"]})
        assert result["status"] == "needs_model" and result["next_step"] == flat_frame["missing_figure_model"]
        assert [p["id"] for p in result["model_choices"]] == [model, unrelated]
        queued = call("draft_model_request", request)
        job = call("job", {"task_id": queued["task_id"]})["job"]
        assert job["station"] == "vulcan"
        assert [p["id"] for p in job["input_pieces"]] == [flat_frame["id"], flat_take["id"]]
        assert call("draft_model_request", request)["task_id"] == queued["task_id"]
        call("order_undo", {"token": queued["undo"]})
        page = ui_headers(hub)
        code, queued_in_page = http(hub.config.url + "/api/agora/draft_model_request", request, page)
        assert code == 200 and queued_in_page["undo"]
        code, undone = http(hub.config.url + "/api/agora/order_undo", {"token": queued_in_page["undo"]}, page)
        assert code == 200 and undone["ok"]
        assert [j["task_id"] for j in call("order", {"order_id": oid})["order"]["plan"]] == [figure["task_id"]]
    finally:
        server.shutdown()
        server.server_close()
