"""Decision media and the production disclosure on the real Orders page."""

import base64
from pathlib import Path

import pytest

from hoard_link.hub.agora import PERSON

from ._hub_fakes import make_hub, serve


def test_draft_gate_prioritizes_the_two_keys_and_the_short_take(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    hub = make_hub(tmp_path)
    ag = hub.facet("agora").agora
    order = ag.order_create({"agent": PERSON, "_person": True, "title": "Map gesture",
                             "words": "Compare the gesture", "folder": str(tmp_path)})["order"]
    draft = ag.draft_pass_open({
        "agent": "builder", "order_id": order["id"],
        "variants": [{"key": "a", "title": "Unroll"}, {"key": "b", "title": "Turn"}],
    })["draft_pass"]
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII="
    )
    (tmp_path / "preview.png").write_bytes(png)
    (tmp_path / "take.mp4").write_bytes(b"Media placeholder for a controls check")
    anchors = ["depth", "keyframe_end", "normals", "animatic", "blocking_color", "keyframe_start"]
    for variant in draft["variants"]:
        selected = anchors if variant["key"] == "a" else ["depth"]
        pieces = []
        for anchor in selected:
            clip = anchor == "animatic"
            path = "take.mp4" if clip else f"{variant['key']}-{anchor}.png"
            if not clip:
                (tmp_path / path).write_bytes(png)
            piece = ag.piece_add({
                "agent": "builder", "order_id": order["id"], "title": anchor, "kind": "video" if clip else "image",
                "path": path,
                **({"previews": ["preview.png"]} if clip else {}),
            })["piece"]
            pieces.append({"piece": piece["id"], "anchor": anchor})
        ag.draft_variant_set({"agent": "builder", "variant_id": variant["id"], "status": "ready", "pieces": pieces})
    ag.draft_gate_open({"agent": "builder", "pass_id": draft["id"]})
    server = serve(hub)
    try:
        with playwright.sync_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            with runtime.chromium.launch(headless=True) as browser:
                page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="en-GB")
                page.goto(hub.config.url + "/#hub/orders")
                variant = page.locator(".drf-variant").filter(has_text="Unroll")
                summary = variant.get_by_text("See how it was made")
                summary.wait_for()
                decision_media = variant.locator(".drf-decision-media")
                assert decision_media.locator("figcaption b").all_text_contents() == [
                    "First key frame", "Last key frame", "Short take",
                ]
                video = decision_media.locator("video")
                assert video.get_attribute("controls") is not None
                assert video.get_attribute("poster").endswith("?preview=0")
                assert variant.get_by_role("button", name="Keep this one").is_visible()
                assert variant.get_by_role("button", name="Discard").is_visible()
                guides = variant.locator(".drf-strip")
                assert not guides.locator("figcaption b:visible").count()
                summary.press("Enter")
                assert guides.locator("figcaption b:visible").all_text_contents() == ["Depth", "Normals", "Blocking colour"]
                summary.press("Enter")
                assert not guides.locator("figcaption b:visible").count()
                fallback = page.locator(".drf-variant").filter(has_text="Turn")
                assert not fallback.locator(".drf-decision-media").count()
                fallback.get_by_text("See how it was made").click()
                assert fallback.get_by_text("Depth", exact=True).first.is_visible()
    finally:
        server.shutdown()
        server.server_close()
