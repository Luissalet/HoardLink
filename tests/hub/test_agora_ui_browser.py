"""#132 in a real browser on the hub page: unread per thread, «new» marks, filters, keyboard, errors with retry, and the
list/detail layout at 1280x720 (side by side) and 390 px (one column, no horizontal scroll)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._hub_fakes import make_hub, serve
from .test_agora_checkpoints import evidence_path


@pytest.mark.parametrize("width,height,language", [(1280, 720, "es"), (390, 844, "en")])
def test_agora_tab_unread_filters_keyboard_errors_and_layout(tmp_path, width, height, language):
    browser_api = pytest.importorskip("playwright.sync_api")
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    ag = hub.facet("agora").agora
    plan = ag.thread_open({"agent": "claude", "title": "Plan de la interfaz", "body": "Primera idea"})["thread"]["id"]
    ag.post({"agent": "codex", "thread_id": plan, "body": "Segunda opinion"})
    ag.thread_open({"agent": "codex", "title": "Otro hilo", "kind": "question", "body": "Una pregunta"})
    ag.task_add({"agent": "luis", "_person": True, "title": "Arreglo urgente", "repo": "Faustus", "priority": 0})
    ag.task_add({"agent": "luis", "_person": True, "title": "Tarea del Hub", "repo": "HoardLink", "priority": 2})
    base = hub.config.url.rstrip("/")
    unread = "2 sin leer" if language == "es" else "2 unread"
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/")
                page.evaluate("language => localStorage.setItem('hub.lang', language)", language)
                page.goto(base + "/")
                page.locator('#family-tabs .tab[data-tab="agora"]').click()
                tab = page.locator("#tab-agora")
                row = tab.locator(".ag-th", has_text="Plan de la interfaz")
                row.wait_for()
                assert unread in row.inner_text()
                assert "P0" in tab.locator(".ag-card", has_text="Arreglo urgente").inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(evidence_path(tmp_path, f"agora-ui-list-{width}.png")), full_page=False)

                # keyboard: focus the row and open it with Enter; the two messages from others are marked new
                row.focus()
                page.keyboard.press("Enter")
                tab.locator(".ag-detail .ag-msg.new").first.wait_for()
                assert tab.locator(".ag-detail .ag-msg.new").count() == 2
                assert ("NUEVO" if language == "es" else "NEW") in tab.locator(".ag-detail .ag-newtag").first.inner_text().upper()
                side_by_side = tab.locator(".ag-wrap").is_visible()
                assert side_by_side == (width >= 1200)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(evidence_path(tmp_path, f"agora-ui-detail-{width}.png")), full_page=False)
                # reading it cleared the count in the list (visible beside it on wide screens, after Back on narrow ones)
                if not side_by_side:
                    tab.locator(".ag-detail .ag-dhead button").first.click()
                tab.locator(".ag-th", has_text="Plan de la interfaz").locator(".ag-unread").wait_for(state="detached")
                if side_by_side:
                    tab.locator(".ag-detail .ag-dhead button").first.click()

                # filter: words match repository, title or agent; nothing else stays
                box = tab.locator(".ag-filterbar input")
                box.fill("faustus")
                tab.locator(".ag-card", has_text="Tarea del Hub").wait_for(state="detached")
                assert tab.locator(".ag-card", has_text="Arreglo urgente").count() == 1
                assert tab.locator(".ag-th").count() == 0
                box.fill("")
                tab.locator(".ag-card", has_text="Tarea del Hub").wait_for()
                tab.locator(".ag-filterbar select").select_option("question")
                tab.locator(".ag-th", has_text="Plan de la interfaz").wait_for(state="detached")
                assert tab.locator(".ag-th", has_text="Otro hilo").count() == 1

                # the hub does not answer: a notice with what happened and a retry; what was shown stays
                page.route("**/api/agora/board", lambda route: route.abort())
                tab.locator(".ag-filterbar select").select_option("")
                retry_name = "Reintentar" if language == "es" else "Retry"
                # the hub polls the tab every 5 s
                page.locator("#tab-agora .ag-error").wait_for(timeout=15000)
                assert page.get_by_role("button", name=retry_name).is_visible()
                assert tab.locator(".ag-card", has_text="Arreglo urgente").count() == 1
                page.screenshot(path=str(evidence_path(tmp_path, f"agora-ui-error-{width}.png")), full_page=False)
                page.unroute("**/api/agora/board")
                page.get_by_role("button", name=retry_name).click()
                page.locator("#tab-agora .ag-error").wait_for(state="hidden")
                # a long conversation mounts its recent part and the rest on request
                longer = ag.thread_open({"agent": "codex", "title": "Hilo largo", "body": "inicio"})["thread"]["id"]
                ag.thread({"thread_id": longer, "agent": "luis", "_person": True})
                for n in range(149):
                    ag.post({"agent": "codex", "thread_id": longer, "body": f"mensaje {n:03}"})
                ag.thread({"thread_id": longer, "agent": "luis", "_person": True})   # all read
                page.goto(base + f"/#agora-{longer}")
                earlier = page.locator("#tab-agora .ag-detail .ag-msgs > button")
                earlier.wait_for()
                assert page.locator("#tab-agora .ag-detail .ag-msg").count() == 120
                earlier.click()
                page.locator("#tab-agora .ag-detail").get_by_text("inicio", exact=True).wait_for()
                assert page.locator("#tab-agora .ag-detail .ag-msg").count() == 150

                # a deep link opens the thread before any list is drawn: what is new still comes marked
                other = ag.thread_open({"agent": "codex", "title": "Enlace directo", "body": "Abierto por enlace"})["thread"]["id"]
                page.goto(base + f"/#agora-{other}")
                page.locator("#tab-agora .ag-detail .ag-msg.new").first.wait_for()
                news = page.locator("#tab-agora .ag-detail .ag-msg.new").all_inner_texts()
                assert len(news) == 1, news
                assert not errors, errors
                evidence_path(tmp_path, f"agora-ui-{width}.json").write_text(json.dumps({
                    "viewport": [width, height], "language": language, "side_by_side": side_by_side,
                    "page_errors": errors}, indent=2), encoding="utf-8")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
