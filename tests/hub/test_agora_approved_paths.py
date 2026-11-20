"""A successor can acquire a path held by an approved delivery."""

from pathlib import Path

from ._hub_fakes import http, make_hub, serve


def test_claim_cedes_only_conflicting_approved_path_leases(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    headers = {"Authorization": "Bearer " + Path(hub.config.token_file).read_text().strip()}
    prefix = hub.config.url + "/api/agora/"
    parent_path = "path:workshop/src"
    unrelated_path = "path:workshop/docs.md"
    child_path = "path:workshop/src/assets.py"
    model = "model:principal"

    def post(operation, agent, *, status=200, **args):
        code, body = http(prefix + operation, {"agent": agent, **args}, headers)
        assert code == status, body
        return body

    try:
        parent = post("task_add", "qa-builder", title="Add renderer")["task"]["id"]
        post("task_claim", "qa-builder", task_id=parent, locks=[parent_path, unrelated_path, model])
        child = post("task_add", "qa-successor", title="Filter rendered assets")["task"]["id"]
        post("task_submit", "qa-builder", task_id=parent, summary="First delivery", commits=["abcd1234"])
        post("task_submit", "qa-builder", task_id=parent, summary="Latest delivery", commits=["abcd5678"])

        claim = {"task_id": child, "locks": [child_path]}
        vote = {"task_id": parent, "body": "Checked", "verdict": "approve"}
        post("task_review", "qa-reviewer", expected_submission_revision=1, status=409, **vote)
        blocked = post("task_claim", "qa-successor", status=409, **claim)
        assert blocked["conflicts"][0]["held"] == parent_path

        approved = post("task_review", "qa-reviewer", expected_submission_revision=2, **vote)["task"]
        assert approved["status"] == "approved"
        assert approved["reviewed_commits"] == ["abcd5678"]
        blocked = post("task_claim", "qa-successor", task_id=child, locks=[child_path, model], status=409)
        assert [lock["held"] for lock in blocked["conflicts"]] == [model]
        _, locks = http(prefix + "locks", headers=headers)
        assert {row["resource"] for row in locks["locks"]} == {parent_path, unrelated_path, model}

        result = post("task_claim", "qa-successor", **claim)
        assert result["locks"] == [child_path]
        assert result["ceded_paths"] == [parent_path]
        _, locks = http(prefix + "locks", headers=headers)
        assert {row["resource"]: row["task_id"] for row in locks["locks"]} == {
            child_path: child, unrelated_path: parent, model: parent,
        }
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
