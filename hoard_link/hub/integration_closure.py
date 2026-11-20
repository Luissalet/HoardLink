"""Git evidence that a published merge contains exactly the reviewed delivery."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from . import integration_git as git


def _commit(repo, ref):
    result = git.git(repo, "rev-parse", "--verify", ref + "^{commit}", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else ""


def published_merge(repo, ref, *, base=None, head=None, target=None):
    """Return the exact merge and its second parent when it is on a mainline."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", ref):
        return None
    merge = _commit(repo, ref)
    if not merge:
        return None
    _, *parents = git.git_text(repo, "rev-list", "--parents", "-n", "1", merge).split()
    if len(parents) != 2:
        return None
    first, second = parents
    if (base and first != base) or (head and second != head):
        return None
    branches = (target,) if target in ("main", "master") else ("main", "master")
    targets = [_commit(repo, "refs/heads/" + branch) for branch in branches]
    targets = [tip for tip in targets if tip and git.ancestor(repo, merge, tip)]
    if not targets or merge not in git.git_text(repo, "rev-list", "--first-parent", *targets).splitlines():
        return None
    tree = git.git(repo, "merge-tree", "--write-tree", "--no-messages", first, second, check=False)
    if tree.returncode or tree.stdout.decode().strip() != git.git_text(repo, "rev-parse", merge + "^{tree}"):
        return None
    return merge, second


def verified_closure(agora, task, commits, *, require_merges=False):
    """Resolve each new hash to one approved branch and account for all approved commits."""
    approved = set(task["reviewed_commits"] or [])
    if not approved or approved != set(task["commits"]) or not all(
            re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) for ref in approved):
        return False
    repos = {Path(entry["path"]).resolve() for entry in
             getattr(agora, "registered_repositories", lambda: [])()}
    if task["repo"] and agora.repo_lookup and (path := agora.repo_lookup(task["repo"])):
        repos.add(Path(path).resolve())
    if require_merges and approved.intersection(commits):
        return False
    covered = approved.intersection(commits)
    for ref in set(commits) - approved:
        matches = set()
        for repo in repos:
            proof = published_merge(repo, ref)
            if proof is None:
                continue
            _, second = proof
            resolved = {item: _commit(repo, item) for item in approved}
            if second in resolved.values():
                matches.add(frozenset(item for item, sha in resolved.items() if sha and git.ancestor(repo, sha, second)))
        if len(matches) != 1:
            return False
        covered.update(matches.pop())
    return covered == approved


def closure_snapshot(agora, task):
    return {"task": {key: task[key] for key in ("status", "commits", "result", "review_state", "closed_at")},
            "thread": dict(agora.db.one("SELECT status,resolution,resolved_by FROM threads WHERE id=?",
                                        (task["thread_id"],))),
            "locks": [dict(row) for row in agora.db.query("SELECT * FROM locks WHERE task_id=?", (task["id"],))]}


def restore_closure(agora, task, before):
    values = before["task"]
    now = agora.clock()
    agora.db.execute("UPDATE tasks SET status=?, commits=?, result=?, review_state=?, closed_at=?, updated=? WHERE id=?",
                     (values["status"], json.dumps(values["commits"]), values["result"], values["review_state"],
                      values["closed_at"], now, task["id"]))
    thread = before["thread"]
    agora.db.execute("UPDATE threads SET status=?, resolution=?, resolved_by=?, updated=? WHERE id=?",
                     (thread["status"], thread["resolution"], thread["resolved_by"], now, task["thread_id"]))


def record_closure_undo(agora, task, agent, now, commits, result):
    token = uuid4().hex
    snapshot = {"before": closure_snapshot(agora, task), "agent": agent, "revision": task["submission_revision"],
                "after": {"closed_at": now, "status": "done", "commits": commits, "result": result,
                          "review_state": "equivalent"}}
    agora.db.insert("integration_closure_undo", {"task_id": task["id"], "token": token, "snapshot": json.dumps(snapshot)})
    return {"tool": "hub_agora_task_done", "arguments": {"task_id": task["id"], "agent": agent,
                                                       "space_id": agora.space_id, "undo_token": token}}


def undo_closure(agora, task, agent, token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise git.IntegrationError("Invalid closure undo token")
    row = agora.db.one("SELECT snapshot FROM integration_closure_undo WHERE task_id=? AND token=?", (task["id"], token))
    if not row:
        raise git.IntegrationError("Unknown or already undone closure")
    snapshot = json.loads(row["snapshot"])
    thread = agora._thread_row(task["thread_id"])
    if (snapshot["agent"] != agent or task["submission_revision"] != snapshot["revision"]
            or any(task[key] != value for key, value in snapshot["after"].items())
            or thread["status"] != "resolved" or thread["resolved_by"] != agent
            or thread["resolution"] != task["result"]):
        raise git.IntegrationError("The closure or task thread no longer matches the undo request")
    before = snapshot["before"]
    live = {lock["resource"] for lock in agora._live_locks()}
    for lock in before["locks"]:
        if lock["expires"] <= agora.clock():
            continue
        resource = lock["resource"]
        if resource in live or not agora._acquire(lock["owner"], [resource], task["id"], lock["ttl_s"], lock["note"])["ok"]:
            raise git.IntegrationError(f"The former task lock is now in use: {resource}")
        agora.db.execute("UPDATE locks SET acquired=?, expires=? WHERE resource=?",
                         (lock["acquired"], lock["expires"], resource))
    restore_closure(agora, task, before)
    agora.db.execute("DELETE FROM integration_closure_undo WHERE task_id=?", (task["id"],))
    agora._system(task, "Se ha deshecho el cierre verificado en el Ágora.", agent)
    return {"ok": True, "task": agora._task_row(task["id"])}
