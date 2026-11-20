"""Prepare exact merge commits and preserve Git's index and working changes."""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .repos import git_env


class IntegrationError(RuntimeError):
    pass


def git(repo, *args, check=True, data=None):
    env = git_env()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, *args],
        input=data, capture_output=True, env=env, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and result.returncode:
        raise IntegrationError(result.stderr.decode("utf-8", "replace").strip() or
                               result.stdout.decode("utf-8", "replace").strip())
    return result


def git_text(repo, *args):
    return git(repo, *args).stdout.decode("utf-8").strip()


def diff_bytes(repo, *args):
    """Preserve Git's patch bytes across shells, files and subprocesses."""
    return git(repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
               "--no-renames", "--no-color", *args).stdout


def paths(raw):
    return [os.fsdecode(name) for name in raw.split(b"\0") if name]


def changed(repo, old, new):
    return set(paths(git(repo, "diff", "--name-only", "--no-renames", "-z", old, new).stdout))


def tracked(repo, commit):
    return set(paths(git(repo, "ls-tree", "-r", "--name-only", "-z", commit).stdout))


def test_path(value):
    if not isinstance(value, str):
        raise IntegrationError("A test path must be a string")
    path = PurePosixPath(value)
    if (path.as_posix() != value or path.is_absolute()
            or any(part in (".", "..") or ":" in part or "\\" in part for part in path.parts)
            or path.suffix != ".py"):
        raise IntegrationError(f"Invalid pytest path: {value}")
    return path.as_posix()


def branch_tip(repo, branch):
    if not isinstance(branch, str) or not branch or branch.startswith(("refs/", "-")):
        raise IntegrationError("A local delivery branch is required")
    git(repo, "check-ref-format", "refs/heads/" + branch)
    return git_text(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")


def ancestor(repo, older, newer):
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def delivery_repository(agora, task, *, summary="", repositories=None):
    """Resolve missing task metadata from an exact branch tip in the Git registry."""
    declared = re.search(r"^Repository:[ \t]*(.+)$", summary, flags=re.MULTILINE)
    if declared:
        name = declared.group(1).strip()
        if agora.repo_lookup and agora.repo_lookup(name):
            return name
    if repositories is None:
        repositories = getattr(agora, "registered_repositories", lambda: [])()
    refs = task.get("commits") or []
    branch = task.get("branch")
    if not refs or not all(re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) for ref in refs) or not branch:
        return ""
    matches = []
    for item in repositories:
        try:
            tip = branch_tip(item["path"], branch)
        except IntegrationError:
            continue
        if any(tip.startswith(ref.lower()) for ref in refs):
            matches.append(item["name"])
    return matches[0] if len(matches) == 1 else ""


def repository_commit_index(repositories, refs):
    """Check historical refs in one Git call per repo after delivery branches are gone."""
    matches = {ref: [] for ref in refs}
    if not matches:
        return matches
    query = "".join(ref + "^{commit}\n" for ref in matches).encode("ascii")
    for repo in repositories:
        result = git(repo["path"], "cat-file", "--batch-check=%(objecttype)", check=False, data=query)
        if result.returncode == 0:
            for ref, kind in zip(matches, result.stdout.splitlines()):
                if kind == b"commit":
                    matches[ref].append(repo["name"])
    return matches


def registered_worktree(repo, branch):
    raw = git(repo, "worktree", "list", "--porcelain", "-z").stdout.decode("utf-8")
    for record in raw.split("\0\0"):
        lines = record.split("\0")
        if f"branch refs/heads/{branch}" in lines:
            return Path(lines[0].removeprefix("worktree "))
    return None


@contextmanager
def worktree(repo, path, commit):
    git(repo, "worktree", "add", "--detach", str(path), commit)
    try:
        yield path
    finally:
        git(repo, "worktree", "remove", "--force", str(path), check=False)


def merge_candidate(work, head):
    subject = git_text(work, "show", "-s", "--format=%s", head)
    git(work, "-c", "user.name=Luissalet", "-c", "user.email=luissalet@users.noreply.github.com",
        "merge", "--no-edit", "--no-gpg-sign", "-m", f"Merge delivery: {subject}", head)
    return git_text(work, "rev-parse", "HEAD")


def overlaps(left, right):
    left, right = os.path.normcase(left), os.path.normcase(right)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def patch(repo, names, *, staged=False):
    if not names:
        return b""
    flags = ["--cached"] if staged else []
    return diff_bytes(repo, *flags, "--", *names)


def foreign(repo, delta):
    if git(repo, "ls-files", "--unmerged", "-z").stdout:
        raise IntegrationError("The destination has unresolved index entries")
    dirty = set(paths(git(repo, "diff", "HEAD", "--name-only", "--no-renames", "-z").stdout))
    dirty.update(paths(git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z").stdout))
    untracked = paths(git(repo, "ls-files", "--others", "--directory", "-z").stdout)
    blocked = [name for name in untracked if any(overlaps(name.rstrip("/"), item) for item in delta)]
    if blocked:
        raise IntegrationError("Untracked or ignored files overlap the merge: " + ", ".join(blocked))
    names = sorted(name for name in dirty if any(overlaps(name, item) for item in delta))
    return names, patch(repo, names, staged=True), patch(repo, names)


def replay(work, staged, unstaged, names):
    """Replay each layer on the new base; keep the second layer out of the index."""
    if staged:
        git(work, "apply", "--3way", "--index", "-", data=staged)
    index = git_text(work, "write-tree")
    if unstaged:
        git(work, "apply", "--3way", "--index", "-", data=unstaged)
        git(work, "read-tree", index)
    return patch(work, names, staged=True), patch(work, names)


def apply_layers(repo, staged, unstaged):
    if staged:
        git(repo, "apply", "--index", "-", data=staged)
    if unstaged:
        git(repo, "apply", "-", data=unstaged)


def idle_checkout(repo):
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        location = Path(git_text(repo, "rev-parse", "--git-path", marker))
        if (repo / location).exists():
            raise IntegrationError(f"The destination already has a Git operation in progress: {marker}")


def move_checkout(repo, old, new, scratch, record):
    """Move the checked-out branch with recoverable snapshots of overlapping edits."""
    idle_checkout(repo)
    branch = git_text(repo, "symbolic-ref", "HEAD")
    delta = changed(repo, old, new)
    original = foreign(repo, delta)
    names, staged, unstaged = original
    if names:
        with worktree(repo, scratch / "replay", new) as copy:
            try:
                restored = replay(copy, staged, unstaged, names)
            except IntegrationError as exc:
                raise IntegrationError(f"Foreign changes conflict on {', '.join(names)}: {exc}") from exc
        (scratch / "parked-index.patch").write_bytes(staged)
        (scratch / "parked-worktree.patch").write_bytes(unstaged)
        (scratch / "restored-index.patch").write_bytes(restored[0])
        (scratch / "restored-worktree.patch").write_bytes(restored[1])
    idle_checkout(repo)
    if (git_text(repo, "symbolic-ref", "HEAD") != branch or
            git_text(repo, "rev-parse", "HEAD") != old or foreign(repo, delta) != original):
        raise IntegrationError("The destination changed while preparing the merge")
    record("prepared_checkout", {"before": old, "after": new, "parked": names})
    if names:
        git(repo, "restore", "--source=" + old, "--staged", "--worktree", "--", *names)
    try:
        # Two-tree checkout leaves unrelated index entries and working files in place.
        git(repo, "read-tree", "-m", "-u", old, new)
    except IntegrationError:
        apply_layers(repo, staged, unstaged)
        raise
    try:
        git(repo, "update-ref", "-m", "integration station", branch, new, old)
    except IntegrationError:
        git(repo, "read-tree", "-m", "-u", new, old)
        apply_layers(repo, staged, unstaged)
        raise
    record("moved_checkout", {"before": old, "after": new, "parked": names})
    if names:
        apply_layers(repo, *restored)
    record("restored_checkout", {"before": old, "after": new, "parked": names})
