"""Localised console chrome with real Hub tasks and quota snapshots."""

import os
from pathlib import Path

import pytest

from hoard_link.hub import agora_leader_state
from hoard_link.hub.agora_console import AgoraConsoleFacet, LEADERS

from ._hub_fakes import make_hub, serve


@pytest.mark.parametrize("lang, copy", [
    ("es", {
        "space": "Estudio de producción", "team": "Equipo", "quota": "Cuota sin datos",
        "week": "Semana · todos los modelos", "model_week": "Semana · Fixture",
        "used": "35% usado", "remaining": "65% libre", "reset": "reinicia",
        "unknown": "Sin datos", "declared": "Declarado", "stale": "Dato caducado",
        "browse": "Consultar modelos", "model": "Modelo del líder", "search": "Buscar tareas",
        "pending": "Pendiente", "completed": "Finalizado", "unassigned": "Sin asignar", "open": "Abierta",
        "reply": "Respuesta", "live": "En directo", "board": "Pizarra", "files": "Ficheros en uso",
        "leader_detail": "No hay sesiones de Claude Code en este equipo.", "intro": "Conversación de este espacio.",
    }),
    ("en", {
        "space": "Production studio", "team": "Team", "quota": "No quota data",
        "week": "Week · all models", "model_week": "Week · Fixture",
        "used": "35% used", "remaining": "65% remaining", "reset": "resets",
        "unknown": "Unknown", "declared": "Declared", "stale": "Stale data",
        "browse": "Browse models", "model": "Leader model", "search": "Search tasks",
        "pending": "Pending", "completed": "Completed", "unassigned": "Unassigned", "open": "Open",
        "reply": "Reply", "live": "Live", "board": "Board", "files": "Files in use",
        "leader_detail": "No Claude Code sessions on this computer.", "intro": "Conversation for this space.",
    }),
])
def test_console_renders_localised_chrome_and_quota(tmp_path, monkeypatch, lang, copy):
    browser_api = pytest.importorskip("playwright.sync_api")
    if not (Path(__file__).parents[2] / "apps/agora/dist/index.html").exists():
        pytest.skip("Build apps/agora before browser tests")
    monkeypatch.setattr(AgoraConsoleFacet, "_provider_data", lambda self, force=False:
                        {leader_id: {"state": "unavailable"} for leader_id, *_ in LEADERS})
    monkeypatch.setattr(agora_leader_state, "default_paths", lambda home=None: {
        key: tmp_path / key for key in ("codex_sessions", "claude_projects", "claude_credentials", "cursor_dir")
    })
    hub = make_hub(tmp_path, [])
    root = hub.facet("agora").agora
    agent = "codex-fixture"
    root.space_create({"agent": agent, "id": "estudio", "title": "Estudio de producción"})
    root.space_create({"agent": agent, "id": "custom", "title": "Nombre personalizado"})
    studio = root.for_space("estudio")
    title = "Revisar la lámina de prueba"
    studio.task_add({"agent": agent, "title": title, "body": "Texto original del encargo."})
    server = serve(hub)
    expect = browser_api.expect
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("AGORA_TEST_BROWSER_CHANNEL"), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="de-DE")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(f"localStorage.setItem('hub.lang', '{'es' if lang == 'en' else 'en'}')")
            url = f"{hub.config.url}/agora/?space_id=estudio&lang={lang}"
            with page.expect_response(lambda response: "/api/agora/console/overview?" in response.url) as overview:
                page.goto(url)
            assert overview.value.ok
            expect(page.locator("html")).to_have_attribute("lang", lang)
            expect(page.get_by_role("heading", name=copy["team"], exact=True)).to_be_visible()
            expect(page.locator(".space-selector option:checked")).to_have_text(copy["space"])
            expect(page.locator(".space-selector option[value=custom]")).to_have_text("Nombre personalizado")
            expect(page.get_by_text(copy["quota"], exact=True)).to_have_count(4)
            expect(page.get_by_test_id("live-claude")).to_have_attribute("title", copy["leader_detail"])
            expect(page.get_by_role("log").get_by_text(copy["intro"], exact=True)).to_be_visible()
            expect(page.locator(".task-tabs").get_by_role("button", name=copy["pending"])).to_be_visible()
            expect(page.get_by_role("button", name=copy["completed"], exact=True)).to_be_visible()
            expect(page.get_by_role("textbox", name=copy["search"])).to_be_visible()
            row = page.locator(".task-row").filter(has_text=title)
            expect(row.get_by_text(copy["open"], exact=True)).to_be_visible()
            expect(row.get_by_text(copy["unassigned"], exact=True)).to_be_visible()
            row.click()
            expect(page.get_by_role("dialog").get_by_role("textbox", name=copy["reply"], exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            page.get_by_text(copy["browse"], exact=True).first.click()
            expect(page.get_by_role("heading", name=copy["model"], exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            page.screenshot(path=tmp_path / f"no-quota-{lang}.png")

            studio.usage_report({"agent": "claude-fixture", "provider_id": "claude", "pool_id": "fixture",
                                 "pool_label": "Cuenta de Claude", "source": "fixture", "windows": [
                {"window_id": "claude.week", "label": "Semana · todos los modelos", "measurement": "measured",
                 "used_percent": 35, "remaining_percent": 65, "reset_at": "2099-11-25T20:00:00Z"},
                {"window_id": "claude.week-fixture", "label": "Semana · Fixture", "measurement": "unknown"},
                {"window_id": "custom", "label": "Ventana personalizada", "measurement": "declared",
                 "used_percent": 10, "remaining_percent": 90},
                {"window_id": "expired", "label": "Fixture (old)", "measurement": "measured",
                 "used_percent": 1, "remaining_percent": 99, "reset_at": "2000-11-25T20:00:00Z"},
            ]})
            page.reload()
            quota = page.locator(".leader").filter(has=page.get_by_role("heading", name="Claude", exact=True)).locator(".quota")
            expect(page.get_by_text(copy["quota"], exact=True)).to_have_count(3)
            expect(quota.get_by_text(copy["week"], exact=True)).to_be_visible()
            expect(quota.get_by_role("meter", name=f"Claude: {copy['week']} {'usada' if lang == 'es' else 'used'}")).to_have_attribute("aria-valuenow", "35")
            expect(quota.get_by_text(copy["used"], exact=True)).to_be_visible()
            expect(quota.get_by_text(copy["remaining"], exact=False)).to_contain_text(copy["reset"])
            expect(quota.get_by_text(copy["model_week"], exact=True)).to_be_visible()
            expect(quota.get_by_text("Ventana personalizada", exact=True)).to_be_visible()
            expect(quota.get_by_text(copy["declared"], exact=False)).to_be_visible()
            expect(quota.get_by_text(copy["unknown"], exact=True)).to_have_count(2)
            expect(quota.get_by_text(copy["stale"], exact=False)).to_be_visible()
            page.screenshot(path=tmp_path / f"quota-{lang}.png")

            page.set_viewport_size({"width": 390, "height": 920})
            header = page.locator(".topbar")
            space = header.locator(".space-selector")
            expect(space.locator("option:checked")).to_have_text(copy["space"])
            label = space.bounding_box()
            select = space.locator("select").bounding_box()
            brand = header.locator(".brand").bounding_box()
            assert select["width"] >= 160
            assert label["x"] + label["width"] <= brand["x"]
            assert brand["x"] + brand["width"] <= 390

            show_tasks = "Mostrar tareas" if lang == "es" else "Show tasks"
            header.get_by_role("button", name=show_tasks).click()
            drawer = page.locator(".operations").bounding_box()
            bar = header.bounding_box()
            assert drawer["y"] == pytest.approx(bar["y"] + bar["height"])
            assert drawer["x"] + drawer["width"] <= 390
            page.set_viewport_size({"width": 1440, "height": 1000})

            page.get_by_role("button", name=copy["live"], exact=True).click()
            expect(page.get_by_role("heading", name=copy["board"], exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name=copy["files"], exact=True)).to_be_visible()
            expect(page.locator(".aglive-task").filter(has_text=title)).to_be_visible()
            expect(page.locator(".aglive-head").get_by_text(copy["space"], exact=True)).to_be_visible()
            page.screenshot(path=tmp_path / f"live-{lang}.png")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
