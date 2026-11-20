"""Correct a recorded fast-forward after the equivalent merge was published."""

from __future__ import annotations

import json

from .integration_closure import published_merge
from .integration_git import IntegrationError


def correction_for(agora, receipt_id):
    row = agora.db.one("SELECT correction FROM integration_receipt_corrections WHERE receipt_id=?", (receipt_id,))
    return json.loads(row["correction"]) if row else None


def corrected_close(close, correction):
    old, new = correction["before"], correction["after"]
    return {**close, "commits": [new if ref == old else ref for ref in close["commits"]],
            "result": close["result"].replace(old, new)}


def corrected_baseline(baseline, correction):
    if baseline.get("commit") != correction["before"]:
        return baseline
    return {**baseline, "commit": correction["after"]}


def apply_correction(receipt, correction):
    if not correction:
        return receipt
    rows = receipt.get("repositories", [receipt])
    row = rows[correction["index"]]
    if row["merge_commit"] == correction["before"]:
        row["merge_commit"] = correction["after"]
        if "baseline" in row:
            row["baseline"] = corrected_baseline(row["baseline"], correction)
        receipt["close_result"] = corrected_close(receipt["close_result"], correction)
    receipt["correction"] = correction
    return receipt


def reconcile(agora, agent, receipt, args):
    rows = receipt.get("repositories", [receipt])
    close = receipt.get("close_result")
    if receipt["state"] != "integrated" or not close:
        raise IntegrationError("The receipt must describe a completed integration")
    stored = correction_for(agora, receipt["id"])
    task = agora._task_row(receipt["task_id"])
    restoring = args.get("restore") is True
    if restoring and not stored:
        raise IntegrationError("The receipt has no correction to undo")
    if restoring:
        patch = stored
    else:
        selected = [(i, row) for i, row in enumerate(rows) if row["plan"]["repo_ref"] == args.get("repo")]
        if len(selected) != 1:
            raise IntegrationError("Choose the registered repository from the receipt")
        index, row = selected[0]
        plan = row["plan"]
        old = row["merge_commit"]
        if old != plan["head"] or close["commits"].count(old) != 1:
            raise IntegrationError("The receipt does not record a fast-forward for that repository")
        path = agora.repo_lookup(plan["repo_ref"]) if agora.repo_lookup else None
        ref = args.get("commit")
        proof = (published_merge(path, ref, base=plan["base"], head=old, target=plan["target"])
                 if path and isinstance(ref, str) else None)
        if not proof:
            raise IntegrationError("The replacement must be the exact merge of the recorded base and delivery on main")
        patch = {"index": index, "repo": plan["repo_ref"], "before": old, "after": proof[0],
                 "original_review_state": stored["original_review_state"] if stored else task["review_state"]}
        if stored and stored != patch:
            raise IntegrationError("Undo the existing correction before choosing a different one")
    revised = corrected_close(close, patch)
    expected = revised if stored else close
    desired = close if restoring else revised
    previous_review = patch["original_review_state"]
    if (task["status"] != "done" or previous_review not in ("approved", "equivalent")
            or task["review_state"] != ("equivalent" if stored else previous_review)
            or task["submission_revision"] != receipt["revision"]
            or task["reviewed_submission_revision"] != receipt["revision"]
            or not any(patch["before"].startswith(ref.lower()) for ref in task["reviewed_commits"] or [])
            or any(task[key] != value for key, value in expected.items())):
        raise IntegrationError("The Agora task no longer matches the receipt")
    thread = agora._thread_row(task["thread_id"])
    agora._may_close(thread, agent, "correct the integration receipt")
    if thread["status"] != "resolved" or thread["resolution"] != expected["result"]:
        raise IntegrationError("The task thread no longer matches the receipt")
    if stored and not restoring:
        return apply_correction(receipt, stored), {"restore": True}
    if restoring:
        agora.db.execute("DELETE FROM integration_receipt_corrections WHERE receipt_id=?", (receipt["id"],))
    else:
        agora.db.insert("integration_receipt_corrections", {"receipt_id": receipt["id"], "correction": json.dumps(patch)})
    now = agora.clock()
    agora.db.execute("UPDATE tasks SET commits=?, result=?, review_state=?, updated=? WHERE id=?",
                     (json.dumps(desired["commits"]), desired["result"],
                      previous_review if restoring else "equivalent", now, task["id"]))
    agora.db.execute("UPDATE threads SET resolution=?, updated=? WHERE id=?",
                     (desired["result"], now, task["thread_id"]))
    before, after = (patch["after"], patch["before"]) if restoring else (patch["before"], patch["after"])
    agora._system(task, f"Recibo {receipt['id']} · {patch['repo']} · {before} → {after}.", agent)
    if restoring:
        return receipt, {"repo": patch["repo"], "commit": patch["after"]}
    return apply_correction(receipt, patch), {"restore": True}
