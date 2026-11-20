"""The Orders list shows the stored preview of an editable slide deck."""

from base64 import b64decode

import pytest

from ._hub_fakes import make_hub, serve


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=")


def test_order_thumbnail_uses_the_registered_preview_for_a_slide_deck(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    files = tmp_path / "pieces"
    files.mkdir()
    (files / "deck.pptx").write_bytes(b"editable presentation")
    (files / "deck.pdf").write_bytes(b"%PDF-1.7")
    (files / "slide.png").write_bytes(PNG)
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    try:
        agora = hub.facet("agora").agora
        agent = "qa-orders"
        order = agora.order_create({"agent": agent, "title": "Presentation", "folder": str(files)})["order"]
        cases = [
            ("Editable presentation", "slides", "deck.pptx", ["slide.png"], "?preview=0"),
            ("PDF presentation", "slides", "deck.pdf", ["slide.png"], "?preview=0"),
            ("Slide image", "image", "slide.png", [], ""),
        ]
        sources = {}
        for title, kind, path, previews, suffix in cases:
            piece = agora.piece_add({"agent": agent, "order_id": order["id"], "title": title,
                                     "kind": kind, "path": path, "previews": previews})["piece"]
            sources[title] = f"/api/agora/piece_file/{piece['id']}{suffix}"

        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except playwright.Error as exc:
                if "not found" in str(exc) or "Executable doesn't exist" in str(exc):
                    pytest.skip("Headless Chrome is not installed")
                raise
            try:
                page = browser.new_page()
                responses = {}
                page.on("response", lambda response: responses.update({response.url: response}))
                page.goto(hub.config.url + "/#hub/orders")
                row = page.locator(f'details[data-order="{order["id"]}"]')
                row.scroll_into_view_if_needed()
                for title, src in sources.items():
                    thumb = row.locator(".ord-thumbs").get_by_alt_text(title, exact=True)
                    playwright.expect(thumb).to_have_attribute("src", src)
                    page.wait_for_function("(selector) => document.querySelector(selector)?.naturalWidth > 0",
                                           arg=f'img[src="{src}"]')
                    response = responses[hub.config.url + src]
                    assert response.ok and response.body() == PNG
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
