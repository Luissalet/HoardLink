"""An agent can correct the note on a shown pass without changing its gate."""

from pathlib import Path

from ._hub_fakes import http, make_hub, serve


def test_note_tool_updates_and_restores_a_shown_pass(tmp_path):
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
        order = call("order_create", {**agent, "title": "Map gesture", "folder": str(tmp_path)})["order"]
        draft = call("draft_pass_open", {
            **agent, "order_id": order["id"], "note": "Images are still pending.",
            "variants": [{"key": "gesture", "title": "Unroll the map"}],
        })["draft_pass"]
        (tmp_path / "frame.png").write_bytes(b"frame")
        piece = call("piece_add", {
            **agent, "order_id": order["id"], "title": "First frame", "kind": "image", "path": "frame.png",
        })["piece"]
        call("draft_variant_set", {
            **agent, "variant_id": draft["variants"][0]["id"], "status": "ready",
            "pieces": [{"piece": piece["id"], "anchor": "keyframe_start"}],
        })
        before = call("draft_gate_open", {**agent, "pass_id": draft["id"]})["draft_pass"]
        identity = {**agent, "pass_id": draft["id"]}
        assert call("draft_pass_note_set", identity, status=400)["ok"] is False
        correction = call("draft_pass_note_set", {**identity, "note": "La imagen está disponible."})
        assert correction["draft_pass"] == {**before, "note": "La imagen está disponible."}
        assert call("draft_passes", {"order_id": order["id"]})["draft_passes"] == [correction["draft_pass"]]
        cleared = call("draft_pass_note_set", {**identity, "note": ""})
        assert cleared["draft_pass"] == {**before, "note": ""}
        assert call("order_undo", {**agent, "token": correction["undo"]}, status=400)["ok"] is False
        call("order_undo", {**agent, "token": cleared["undo"]})
        call("order_undo", {**agent, "token": correction["undo"]})
        assert call("draft_passes", {"order_id": order["id"]})["draft_passes"] == [before]
    finally:
        server.shutdown()
        server.server_close()
