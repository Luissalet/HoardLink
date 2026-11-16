"""Ágora delivery review on the terminal and on the Hub's page: ``scripts/agora.py delivery …`` and the diff reader."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from ._hub_fakes import make_hub, serve
from .test_agora_review import GUIDE, _delivered, _repo, ag  # noqa: F401  (the fixture is shared)


def test_cli_delivery_subcommands(ag, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agora_cli_review",
                                                  Path(__file__).resolve().parents[2] / "scripts" / "agora.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    task = _delivered(ag)
    ag.review_chat = lambda body: (200, {"ok": True, "json": GUIDE, "model": "m"})
    sent = []

    def fake(method, path, body=None, timeout=150.0):
        sent.append((method, path, body))
        op = path.split("?")[0].rsplit("/", 1)[-1]
        if method == "POST":
            return getattr(ag, op)(dict(body))
        query = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
        return getattr(ag, op)({**query, "task_id": int(query["task_id"]),
                                "summary_only": query.get("summary_only") == "1"})

    monkeypatch.setattr(cli, "call", fake)
    assert cli.main(["delivery", "diff", str(task["id"]), "--commit", ag.first[:8], "--path", "src/app.py"]) == 0
    out = capsys.readouterr().out
    assert "[modified] src/app.py +2 -1" in out and "+inserted after ten" in out
    assert cli.main(["--as", "reviewer", "delivery", "comment", str(task["id"]), "--commit", ag.first[:8], "--path",
                     "src/app.py", "--line", "11", "--body", "Why here?", "--revision", "1"]) == 0
    assert sent[-1][2]["comments"] == [{"path": "src/app.py", "line": 11, "body": "Why here?", "side": "new",
                                        "commit": ag.first[:8]}] and sent[-1][2]["expected_submission_revision"] == 1
    assert "1 comentario(s) enviados" in capsys.readouterr().out
    assert cli.main(["delivery", "comments", str(task["id"])]) == 0
    assert "src/app.py:11 (new)" in capsys.readouterr().out
    assert cli.main(["--as", "reviewer", "delivery", "guide", str(task["id"])]) == 0
    assert "1. The app — src/app.py" in capsys.readouterr().out
    with pytest.raises(SystemExit):                       # a path without a line is refused before any request
        cli.main(["--as", "reviewer", "delivery", "comment", str(task["id"]), "--path", "src/app.py", "--body", "x"])


def test_review_reader_in_the_browser(tmp_path):
    browser_api = pytest.importorskip("playwright.sync_api")
    repo = tmp_path / "Demo"
    first, _second = _repo(repo)
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {"enabled": False}
    server = serve(hub)
    ag = hub.facet("agora").agora
    ag.repo_lookup = lambda name: repo if name == "Demo" else None
    ag.review_chat = lambda body: (200, {"ok": True, "json": GUIDE, "model": "m"})
    task = ag.task_add({"agent": "builder", "title": "Ship it", "repo": "Demo", "claim": True})["task"]
    ag.task_submit({"agent": "builder", "task_id": task["id"], "summary": "Done", "commits": [first]})
    base = hub.config.url.rstrip("/")
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Headless Chromium is not installed")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/")
                page.evaluate("language => localStorage.setItem('hub.lang', language)", "es")
                page.goto(base + "/#hub/agora")
                tab = page.locator("#tab-agora")
                tab.locator(".ag-card", has_text="Ship it").first.click()
                tab.locator(".ag-detail .ag-review").wait_for()
                tab.locator('[data-action="open-review"]').click()
                overlay = page.locator(".ag-rv-overlay")
                app = overlay.locator("details.ag-rv-file", has_text="src/app.py")
                app.locator('tr[data-new="11"]').wait_for()
                app.locator('tr[data-new="11"] button.plus').click(force=True)
                app.locator(".ag-rv-bubble.composer textarea").fill("¿Por qué aquí?")
                app.locator('[data-action="add-comment"]').click()
                assert "(1)" in overlay.locator('[data-action="send"]').inner_text()
                overlay.locator('[data-action="send"]').click()
                overlay.locator(".ag-rv-bubble.saved").first.wait_for()
                assert overlay.locator(".ag-rv-bubble.queued").count() == 0
                sent = ag.review_comments({"task_id": task["id"]})["comments"]
                assert [(c["author"], c["line"], c["body"]) for c in sent] == [("luis", 11, "¿Por qué aquí?")]
                assert any(m["mentions"] == ["builder"] and "¿Por qué aquí?" in m["body"]
                           for m in ag.task({"task_id": task["id"], "peek": True})["messages"])
                overlay.locator('[data-action="guide"]').click()
                overlay.locator(".ag-rv-chap[data-chapter]").first.wait_for()
                assert overlay.locator(".ag-rv-chap[data-chapter]").count() == 2
                page.keyboard.press("Escape")
                assert page.locator(".ag-rv-overlay").count() == 0
                assert errors == []
            finally:
                browser.close()
    finally:
        server.shutdown()
        hub.close()
