import json

import pytest

from hoard_link.hub import order_budget
from hoard_link.hub.agora import Agora, AgoraError
from hoard_link.hub.config import HubConfig
from hoard_link.hub.integration_git import git, git_text


def test_catalog_isolation_ignores_the_configured_faustus_urls(tmp_path):
    config = tmp_path / "hub.json"
    config.write_text('{"faustus_urls":["http://catalog.test"]}', encoding="utf-8")
    assert HubConfig.load(config, env={}).faustus_urls == ["http://catalog.test"]
    isolated = HubConfig.load(config, env={"HOARD_HUB_ISOLATED_CATALOG": "1"})
    assert isolated.isolated_catalog and isolated.faustus_urls == []


def test_order_metrics_use_closed_task_revisions_and_elapsed_time(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    try:
        order_budget.setup(agora)
        order = agora.order_create({"agent": "maker", "title": "How the studio works"})["order"]
        scope = {"agent": "maker", "order_id": order["id"]}
        plan = agora.plan_set({**scope, "jobs": [
            {"key": "first", "title": "Data", "station": "data"},
            {"key": "second", "title": "Check data", "station": "data"},
            {"key": "open", "title": "Slides", "station": "slides"},
            {"key": "removed", "title": "Earlier plan", "station": "draft"},
        ]})
        first, second, opened, removed = (plan["keys"][key] for key in ("first", "second", "open", "removed"))
        agora.db.execute("UPDATE tasks SET status='done', submission_revision=1, created=0, claimed_at=30, "
                         "submitted_at=90, closed_at=120, repo='Data' WHERE id=?", (first,))
        agora.db.execute("UPDATE tasks SET status='done', submission_revision=3, created=10, claimed_at=20, "
                         "submitted_at=200, closed_at=250, repo='Data' WHERE id=?", (second,))
        agora.db.execute("UPDATE tasks SET status='review', submission_revision=2, claimed_at=1, "
                         "submitted_at=60 WHERE id=?", (opened,))
        agora.plan_set({**scope, "remove": [removed]})
        other = agora.task_add({"agent": "maker", "title": "Earlier task"})["task"]["id"]
        agora.db.execute("UPDATE tasks SET status='done', submission_revision=2 WHERE id=?", (other,))

        groups = order_budget.metrics(agora, scope)["task_stations"]
        data, slides = groups
        assert data == {"station": "data", "repo": "Data", "tasks": 2, "closed_tasks": 2,
                        "first_submission_tasks": 1, "returned_tasks": 1, "resubmissions": 2,
                        "median_close_first_seconds": 120, "median_close_returned_seconds": 240,
                        "median_submit_first_seconds": 60, "median_submit_returned_seconds": 180}
        assert slides["station"] == "slides" and slides["closed_tasks"] == 0
        assert slides["median_submit_returned_seconds"] is None
        unknown = order_budget.metrics(agora, {})["task_stations"][-1]
        assert (unknown["station"], unknown["repo"], unknown["resubmissions"]) == ("", "", 1)
        agora.space_create({"agent": "maker", "id": "studio", "title": "Studio"})
        with pytest.raises(AgoraError, match="no order"):
            order_budget.metrics(agora.for_space("studio"), scope)
        assert order_budget.metrics(agora.for_space("studio"), {**scope, "all_spaces": True})["task_stations"] == groups
    finally:
        agora.close()


def test_plan_keeps_repository_and_station_in_sync_with_undo(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    try:
        agora.order_station_repo = {"writer": "Writing", "layout": "Layout"}.get
        order = agora.order_create({"agent": "maker", "title": "Presentation"})["order"]
        scope = {"agent": "maker", "order_id": order["id"]}
        plan = agora.plan_set({**scope, "jobs": [{"key": "brief", "title": "Brief", "station": "writer"}]})
        tid = plan["keys"]["brief"]
        original = agora._task_row(tid)
        assert original["repo"] == "Writing" and original["context"]["station"] == "writer"
        assert plan["plan"][0]["repo"] == "Writing"
        patch = agora.plan_set({**scope, "jobs": [{"task_id": tid, "station": "layout", "repo": "Shared repo"}]})
        task = agora._task_row(tid)
        assert task["repo"] == "Shared repo" and task["context"]["station"] == "layout"
        assert agora.job({"task_id": tid})["job"]["repo"] == "Shared repo"
        agora.order_undo({"agent": "maker", "token": patch["undo"]})
        task = agora._task_row(tid)
        assert task["repo"] == "Writing" and task["context"] == original["context"]
        assert agora.job({"task_id": tid})["job"]["station"] == "writer"
    finally:
        agora.close()


def test_construction_returns_use_declared_station_or_unique_commit_repository(tmp_path):
    def repo_with_commit(name, app):
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "--allow-empty", "-m", f"Studio construction in {name}")
        return {"name": name, "path": str(repo), "app": app}, git_text(repo, "rev-parse", "HEAD")

    repo, head = repo_with_commit("Hoard Hub", None)
    service, service_head = repo_with_commit("Runtime", "comfyui")
    agora = Agora(tmp_path / "agora.db")
    try:
        order_budget.setup(agora)
        agora.registered_repositories = lambda: [repo]
        agora.order_service_repository = lambda: service
        agora.order_station_repo = {"writer": "Writing"}.get
        historical = agora.task_add({"agent": "maker", "title": "Previous delivery"})["task"]
        declared = agora.task_add({"agent": "maker", "title": "Construction", "context": {"station": "writer"}})["task"]
        multi = agora.task_add({"agent": "maker", "title": "Cross-repository delivery"})["task"]
        for tid, refs in ((historical["id"], [head[:10]]), (declared["id"], []), (multi["id"], [head, service_head])):
            agora.db.execute("UPDATE tasks SET status='done', submission_revision=3, commits=? WHERE id=?",
                             (json.dumps(refs), tid))
        groups = order_budget.metrics(agora, {})["task_stations"]
        assert {(row["station"], row["repo"], row["returned_tasks"], row["resubmissions"]) for row in groups} == {
            ("hoardhub", "Hoard Hub", 1, 2), ("writer", "Writing", 1, 2),
            ("hoardhub, comfyui", "Hoard Hub, Runtime", 1, 2)}
    finally:
        agora.close()


def test_construction_returns_inherit_repository_from_approved_delivery(tmp_path):
    repo = tmp_path / "Renderer"
    repo.mkdir()
    (repo / "faustus-plugin.json").write_text('{"id":"renderer"}', encoding="utf-8")
    agora = Agora(tmp_path / "agora.db", repo_lookup=lambda name: repo if name.casefold() == "renderer" else None)
    try:
        order_budget.setup(agora)
        agora.registered_repositories = lambda: [{"name": "Renderer", "path": str(repo)}]
        task = agora.task_add({"agent": "maker", "title": "Build preview", "claim": True})["task"]
        scope = {"agent": "maker", "task_id": task["id"]}
        for revision in (1, 2):
            agora.task_submit({**scope, "commits": [f"unavailable-{revision}"], "summary": "Repository: renderer"})
            agora.task_review({**scope, "agent": "reviewer", "expected_submission_revision": revision,
                               "verdict": "changes" if revision == 1 else "approve", "body": "Reviewed."})
        agora.task_done({**scope, "commits": ["integrated-elsewhere"], "force": True, "reason": "Equivalent"})
        group, = order_budget.metrics(agora, {})["task_stations"]
        assert agora._task_row(task["id"])["repo"] == ""
        assert (group["station"], group["repo"], group["returned_tasks"], group["resubmissions"]) == (
            "renderer", "Renderer", 1, 1)
    finally:
        agora.close()
