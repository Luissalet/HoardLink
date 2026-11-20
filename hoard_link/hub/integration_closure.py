"""Git evidence that a published merge contains exactly the reviewed delivery."""

from __future__ import annotations

import re
from pathlib import Path

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


def verified_closure(agora, task, commits):
    """Resolve each new hash to one approved branch and account for all approved commits."""
    approved = set(task["reviewed_commits"] or [])
    if not approved or approved != set(task["commits"]) or not all(
            re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) for ref in approved):
        return False
    repos = {Path(entry["path"]).resolve() for entry in
             getattr(agora, "registered_repositories", lambda: [])()}
    if task["repo"] and agora.repo_lookup and (path := agora.repo_lookup(task["repo"])):
        repos.add(Path(path).resolve())
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
