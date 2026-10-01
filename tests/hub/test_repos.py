"""The Repos facet: real git repositories in a temp folder (with a bare repository as the remote), scanned by
``RepoMonitor``; then the routes, the tools, the events and the rule over a hub."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hoard_link
from hoard_link.hub import HUB_VERSION, repos, tools
from hoard_link.hub.repos import RepoMonitor, RepoSettings, build_issues, filter_rows, parse_ci, parse_github, parse_status, sanitize_url

from .test_hub_and_server import _http

ME = "luissalet@users.noreply.github.com"
GIT_ENV = {"GIT_AUTHOR_NAME": "Luis", "GIT_AUTHOR_EMAIL": ME, "GIT_COMMITTER_NAME": "Luis", "GIT_COMMITTER_EMAIL": ME,
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, email=None, check=True):
    env = {**os.environ, **GIT_ENV}
    if email:
        env.update({"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email})
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=str(cwd),
                          capture_output=True, env=env, text=True)
    if check and proc.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def commit(repo, name="file.txt", content=None, msg="change", email=None):
    p = Path(repo) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if content is not None else f"{time.time_ns()}\n", encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", msg, email=email)


def make_repo(path, files=("README.md", "README.es.md", "LICENSE")):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for f in files:
        (path / f).write_text(f"{f}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def with_remote(repo, tmp_path, name="remote.git", push=True):
    bare = Path(tmp_path) / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    if push:
        git(repo, "push", "-q", "-u", "origin", "main")
    return bare


def make_src(tmp_path):
    """A stand-in for the canonical hoard_link package (plus its theme and the JS client)."""
    root = tmp_path / "srcroot"
    pkg = root / "hoard_link"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "hub").mkdir()
    (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (pkg / "a.py").write_text("A = 1\n", encoding="utf-8")
    (pkg / "ui" / "hoard-theme.css").write_text("a { color: red }\n", encoding="utf-8")
    (pkg / "hub" / "core.py").write_text("HUB = 1\n", encoding="utf-8")
    (root / "js").mkdir()
    (root / "js" / "hoard-link.js").write_text("export const x = 1;\n", encoding="utf-8")
    return pkg


def vendor(src, dst):
    """What scripts/sync_vendored.py leaves in an app: the package minus hub/, plus VENDORED.txt and LICENSE."""
    import shutil
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("hub", "__pycache__"))
    (Path(dst) / "VENDORED.txt").write_text("vendored\n", encoding="utf-8")
    (Path(dst) / "LICENSE").write_text("MIT\n", encoding="utf-8")


class Env:
    def __init__(self, tmp_path, ci_runner=None, which=lambda n: None, now=time.time):
        self.tmp = tmp_path
        self.root = tmp_path / "repos"
        self.root.mkdir(parents=True)
        self.src = make_src(tmp_path)
        self.settings = RepoSettings(roots=[str(self.root)], ci=False)
        self.apps: list = []
        self.events: list[tuple[str, dict]] = []
        self.mon = RepoMonitor(str(tmp_path / "data"), lambda: self.settings, lambda: self.apps,
                               emit=lambda t, d: self.events.append((t, d)), src_pkg=str(self.src),
                               ci_runner=ci_runner, which=which, now=now)

    def scan(self, path):
        path = Path(path)
        return self.mon.scan_repo(str(path), path.name, None, self.settings)

    def refresh(self):
        self.mon.refresh(wait=True)
        self.mon.join_ci()
        return self.mon.snapshot()

    def kinds(self, rec):
        return {i["kind"] for i in rec["issues"]}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def fresh(rec):
    rec["issues"] = build_issues(rec)
    return rec


# ---- pure helpers ----------------------------------------------------------------------------------------------

def test_sanitize_and_parse_github():
    assert sanitize_url("https://user:tok3n@github.com/Luissalet/X.git") == "https://github.com/Luissalet/X.git"
    assert sanitize_url("https://tok3n@github.com/a/b") == "https://github.com/a/b"
    assert sanitize_url("git@Luissalet:Luissalet/X.git") == "git@Luissalet:Luissalet/X.git"
    cases = {
        "https://github.com/Luissalet/HoardLink.git": "Luissalet/HoardLink",
        "https://user:tok@github.com/Luissalet/HoardLink": "Luissalet/HoardLink",
        "git@github.com:Luissalet/HoardLink.git": "Luissalet/HoardLink",
        "git@Luissalet:Luissalet/Phileas-Hoard.git": "Luissalet/Phileas-Hoard",   # an ssh alias as the host
        "ssh://git@github.com/Luissalet/HoardLink.git": "Luissalet/HoardLink",
        "https://gitlab.com/a/b.git": None,
        "git@gitlab.com:a/b.git": None,
        "D:/repos/bare.git": None,                                                  # a Windows drive, not an alias
        "/tmp/some/remote.git": None,
        "file:///tmp/some/remote.git": None,
        "": None,
    }
    for url, want in cases.items():
        assert parse_github(url) == want, url


def test_parse_status_counts_and_paths():
    text = "\0".join([" M a b.txt", "M  staged.py", "MM both.py", "?? new file.txt", "R  new.py", "old.py", "UU conflict.c", "!! ignored", ""])
    st = parse_status(text)
    assert st["total"] == 6 and st["untracked"] == 1 and st["conflicted"] == 1
    assert st["staged"] == 4 and st["modified"] == 3          # M, MM (staged+modified), R, UU (counts in both)
    assert {"xy": " M", "path": "a b.txt"} in st["paths"] and {"xy": "??", "path": "new file.txt"} in st["paths"]
    assert not any(p["path"] == "old.py" for p in st["paths"])  # the origin of a rename is not a change of its own
    many = parse_status("\0".join(f"?? f{i}" for i in range(80)) + "\0")
    assert many["total"] == 80 and len(many["paths"]) == 50 and many["truncated"]


def test_secret_like_and_ci_parse():
    like = repos._secret_like
    for p in (".env", ".env.local", "app/.env.production", "certs/server.pem", "k.key", "id_rsa", "id_rsa.pub", "data/mcp-token",
              "mcp-token", "data/app.db"):
        assert like(p), p
    for p in (".env.example", "src/data.js", "client/src/data/x.json", "data/.gitkeep", "README.md", "keyboard.ts"):
        assert not like(p), p
    assert parse_ci("[]") == {"state": "none"}
    assert parse_ci("not json")["state"] == "unknown"
    ok = parse_ci(json.dumps([{"status": "completed", "conclusion": "success", "headSha": "abc123456789xyz", "url": "u"}]), "abc123456789")
    assert ok["state"] == "passing" and ok["head_matches"] is True
    assert parse_ci(json.dumps([{"status": "in_progress", "conclusion": ""}]))["state"] == "running"
    assert parse_ci(json.dumps([{"status": "completed", "conclusion": "cancelled"}]))["state"] == "unknown"
    assert parse_ci(json.dumps([{"status": "completed", "conclusion": "timed_out"}]))["state"] == "failing"


# ---- per-repo facts ---------------------------------------------------------------------------------------------

def test_clean_pushed_repo_has_no_issues(env):
    r = make_repo(env.root / "clean")
    with_remote(r, env.tmp)
    rec = env.scan(r)
    assert rec["error"] is None and rec["branch"] == "main" and not rec["detached"]
    assert rec["unpushed"] == 0 and rec["ahead"] == 0 and rec["behind"] == 0 and rec["upstream"] == "origin/main"
    assert not rec["never_pushed"] and rec["dirty"]["total"] == 0 and rec["stash"] == 0
    assert rec["head"]["subject"] == "initial" and rec["head"]["email"] == ME
    assert rec["files"] == {"readme": True, "readme_es": True, "license": True, "workflows": 0, "manifest": False, "icon": False}
    assert rec["issues"] == []
    assert [c["pushed"] for c in rec["commits"]] == [True]


def test_ahead_and_unpushed_with_an_upstream(env):
    r = make_repo(env.root / "ahead")
    with_remote(r, env.tmp)
    commit(r, "a.txt", msg="one")
    commit(r, "b.txt", msg="two")
    rec = env.scan(r)
    assert rec["unpushed"] == 2 and rec["ahead"] == 2 and rec["behind"] == 0 and not rec["never_pushed"]
    assert "unpushed" in env.kinds(rec) and "never_pushed" not in env.kinds(rec)
    assert [(c["subject"], c["pushed"]) for c in rec["commits"]] == [("two", False), ("one", False), ("initial", True)]
    assert rec["unpushed_authors"] == [{"email": ME, "count": 2}] and rec["unexpected_authors"] == []
    git(r, "push", "-q")
    assert env.scan(r)["unpushed"] == 0


def test_unpushed_without_an_upstream(env):
    r = make_repo(env.root / "noup")
    with_remote(r, env.tmp)
    git(r, "checkout", "-q", "-b", "feature")
    commit(r, "f.txt", msg="feature work")
    rec = env.scan(r)
    assert rec["upstream"] is None and rec["ahead"] is None and rec["no_upstream"]
    assert rec["unpushed"] == 1 and not rec["never_pushed"]      # not in any remote ref, although other branches are
    push = env.mon.push_command  # needs a snapshot to look the repo up
    env.refresh()
    cmd = push("noup")
    assert cmd["ok"] and cmd["command"] == f'git -C "{r}" push -u origin feature'


def test_behind_upstream_is_info(env):
    r = make_repo(env.root / "behind")
    bare = with_remote(r, env.tmp)
    other = env.tmp / "other"
    git(env.tmp, "clone", "-q", str(bare), str(other))
    commit(other, "x.txt", msg="elsewhere")
    git(other, "push", "-q")
    git(r, "fetch", "-q")
    rec = env.scan(r)
    assert rec["behind"] == 1 and rec["unpushed"] == 0
    issue = next(i for i in rec["issues"] if i["kind"] == "behind")
    assert issue["severity"] == "info"


def test_never_pushed(env):
    # a remote that was added but never pushed to, and a repo with no remote at all
    a = make_repo(env.root / "nopush")
    with_remote(a, env.tmp, push=False)
    b = make_repo(env.root / "noremote")
    ra, rb = env.scan(a), env.scan(b)
    assert ra["never_pushed"] and not ra["no_remote"] and ra["unpushed"] == 1
    assert rb["never_pushed"] and rb["no_remote"] and rb["remotes"] == []
    for rec in (ra, rb):
        issue = next(i for i in rec["issues"] if i["kind"] == "never_pushed")
        assert issue["severity"] == "warn" and "unpushed" not in env.kinds(rec)
    assert "sin remoto" in next(i for i in rb["issues"] if i["kind"] == "never_pushed")["text"]["es"]
    # after the first push it is an ordinary repo
    git(a, "push", "-q", "-u", "origin", "main")
    assert not env.scan(a)["never_pushed"]


def test_dirty_counts_and_paths(env):
    r = make_repo(env.root / "dirty")
    with_remote(r, env.tmp)
    (r / "README.md").write_text("changed\n", encoding="utf-8")                  # modified
    (r / "staged.txt").write_text("s\n", encoding="utf-8")
    git(r, "add", "staged.txt")                                                   # staged
    (r / "sub dir").mkdir()
    (r / "sub dir" / "it's new.txt").write_text("n\n", encoding="utf-8")         # untracked, space and apostrophe in the path
    git(r, "stash", "list")
    rec = env.scan(r)
    d = rec["dirty"]
    assert (d["staged"], d["modified"], d["untracked"], d["total"]) == (1, 1, 1, 3)
    assert {p["path"] for p in d["paths"]} == {"README.md", "staged.txt", "sub dir/"}          # an untracked folder is one entry
    assert next(i for i in rec["issues"] if i["kind"] == "dirty")["severity"] == "info"
    git(r, "stash", "push", "-q", "-u")
    rec = env.scan(r)
    assert rec["stash"] == 1 and rec["dirty"]["total"] == 0


def test_stray_branches_by_prefix_and_by_merged(env):
    r = make_repo(env.root / "stray")
    with_remote(r, env.tmp)
    git(r, "checkout", "-q", "-b", "claude/session-1")
    commit(r, "c.txt", msg="agent work")                      # not merged, but its prefix says stray
    git(r, "checkout", "-q", "main")
    git(r, "checkout", "-q", "-b", "done")
    commit(r, "d.txt", msg="finished")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "-m", "merge done", "done")   # fully merged into main
    git(r, "checkout", "-q", "-b", "wip")
    commit(r, "w.txt", msg="in progress")                       # unmerged, no prefix: a normal branch
    git(r, "checkout", "-q", "main")
    rec = env.scan(r)
    by = {b["name"]: b["stray"] for b in rec["branches"]}
    assert by == {"main": None, "claude/session-1": "prefix", "done": "merged", "wip": None}
    assert rec["default_branch"] == "main" and sorted(rec["stray_branches"]) == ["claude/session-1", "done"]
    issue = next(i for i in rec["issues"] if i["kind"] == "stray_branch")
    assert issue["severity"] == "warn" and set(issue["detail"]["branches"]) == {"claude/session-1", "done"}
    assert env.mon.row(fresh(rec))["extra_branches"] == 3
    # the prefixes are configurable
    env.settings.stray_prefixes = []
    assert sorted(env.scan(r)["stray_branches"]) == ["done"]
    env.settings.default_branches = ["trunk"]       # no default branch exists: only prefixes can say stray
    env.settings.stray_prefixes = ["wip"]
    assert env.scan(r)["stray_branches"] == ["wip"] and env.scan(r)["default_branch"] is None


def test_detached_head(env):
    r = make_repo(env.root / "detached")
    commit(r, "x.txt")
    git(r, "checkout", "-q", "--detach", "HEAD~1")
    rec = env.scan(r)
    assert rec["detached"] and rec["branch"] is None
    assert next(i for i in rec["issues"] if i["kind"] == "detached_head")["severity"] == "warn"
    env.refresh()
    assert env.mon.push_command("detached")["ok"] is False


def test_in_progress_operation_is_an_error(env):
    r = make_repo(env.root / "rebasing")
    assert env.scan(r)["operation"] is None
    gitdir = Path(git(r, "rev-parse", "--absolute-git-dir"))
    (gitdir / "rebase-merge").mkdir()
    rec = env.scan(r)
    assert rec["operation"] == "rebase"
    assert next(i for i in rec["issues"] if i["kind"] == "in_progress")["severity"] == "error"
    (gitdir / "rebase-merge").rmdir()
    (gitdir / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert env.scan(r)["operation"] == "merge"
    (gitdir / "MERGE_HEAD").unlink()
    (gitdir / "CHERRY_PICK_HEAD").write_text("0" * 40 + "\n")
    assert env.scan(r)["operation"] == "cherry-pick"


def test_stale_lock_by_age(env):
    r = make_repo(env.root / "locked")
    lock = Path(git(r, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("")
    rec = env.scan(r)                                    # fresh: someone is working in it right now
    assert rec["lock"] and not rec["lock"]["stale"] and "stale_lock" not in env.kinds(rec)
    old = time.time() - 20 * 60
    os.utime(lock, (old, old))
    rec = env.scan(r)
    assert rec["lock"]["stale"] and rec["lock"]["age_s"] >= 20 * 60 - 5
    issue = next(i for i in rec["issues"] if i["kind"] == "stale_lock")
    assert issue["severity"] == "error" and rec["error"] is None     # the scan itself still works with a lock present
    assert lock.exists()                                             # and the hub never removes it


def test_scan_never_creates_index_lock_or_touches_the_index(env):
    r = make_repo(env.root / "quiet", files=[f"f{i}.txt" for i in range(60)])
    for i in range(60):                                   # stat-dirty index: git would like to refresh (and lock) it
        os.utime(r / f"f{i}.txt", (1_000_000_000 + i, 1_000_000_000 + i))
    (r / "f3.txt").write_text("modified\n", encoding="utf-8")
    gitdir = Path(git(r, "rev-parse", "--absolute-git-dir"))
    index = gitdir / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    seen = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            if (gitdir / "index.lock").exists():
                seen.append(time.time())

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        for _ in range(8):
            rec = env.scan(r)
            assert rec["error"] is None and rec["dirty"]["modified"] == 1
    finally:
        stop.set()
        t.join()
    assert seen == [] and not (gitdir / "index.lock").exists()
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_git_calls_are_read_only_and_bounded(env, monkeypatch):
    r = make_repo(env.root / "audit")
    with_remote(r, env.tmp)
    calls = []
    real_run = subprocess.run

    def spy(argv, **kw):
        calls.append((list(argv), kw))
        return real_run(argv, **kw)

    monkeypatch.setattr(repos.subprocess, "run", spy)
    env.refresh()
    assert env.mon.fetch("audit")["ok"]
    assert calls
    allowed = {"rev-parse", "symbolic-ref", "log", "remote", "for-each-ref", "rev-list", "status", "stash", "ls-files", "fetch"}
    for argv, kw in calls:
        assert argv[1:5] == ["--no-optional-locks", "-c", "core.quotepath=off", "-C"], argv
        assert argv[5] == str(r)
        sub = argv[6]
        assert sub in allowed, argv
        assert kw["env"]["GIT_OPTIONAL_LOCKS"] == "0" and kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["env"]["LC_ALL"] == "C"
        assert kw["timeout"] == (60.0 if sub == "fetch" else 10.0)
        assert kw["stdin"] == subprocess.DEVNULL
    assert [a[6] for a, _ in calls].count("fetch") == 1
    fetch = next(a for a, _ in calls if a[6] == "fetch")
    assert fetch[7:] == ["--quiet", "--prune"]


def test_git_timeout_and_missing_git(tmp_path):
    if os.name == "nt":
        pytest.skip("needs a shell script")
    slow = tmp_path / "slowgit"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    t0 = time.monotonic()
    res = repos.run_git(str(tmp_path), "status", timeout=0.4, git=str(slow))
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 3
    assert repos.run_git(str(tmp_path), "status", git=str(tmp_path / "nope")).rc == 127


def test_not_a_repository_is_reported_not_raised(env, tmp_path):
    plain = env.root / "plain"
    plain.mkdir()
    rec = env.scan(plain)
    assert rec["error"] and [i["kind"] for i in rec["issues"]] == ["scan_error"]
    row = env.mon.row(rec)
    assert row["error"] and row["severity"] == "warn"


def test_empty_repository_without_commits(env):
    r = env.root / "empty"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    rec = env.scan(r)
    assert rec["error"] is None and rec["empty"] and rec["head"] is None and rec["unpushed"] == 0 and not rec["never_pushed"]
    assert not rec["detached"] and rec["branch"] == "main"


def test_unexpected_author_on_unpushed_commits_is_a_hint(env):
    r = make_repo(env.root / "authors")
    with_remote(r, env.tmp)
    commit(r, "a.txt", msg="mine")
    commit(r, "b.txt", msg="someone else", email="stranger@example.com")
    rec = env.scan(r)
    assert rec["unexpected_authors"] == ["stranger@example.com"]
    assert next(i for i in rec["issues"] if i["kind"] == "unexpected_author")["severity"] == "info"
    env.settings.expected_emails = ["stranger@example.com", ME]
    assert env.scan(r)["unexpected_authors"] == []
    git(r, "push", "-q")
    assert env.scan(r)["unexpected_authors"] == []        # only unpushed commits count


def test_docs_files_and_workflows(env):
    r = make_repo(env.root / "docs", files=("README.md",))
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (r / "faustus-plugin.json").write_text("{}", encoding="utf-8")
    (r / "app-icon.png").write_bytes(b"\x89PNG")
    rec = env.scan(r)
    assert rec["files"] == {"readme": True, "readme_es": False, "license": False, "workflows": 1, "manifest": True, "icon": True}
    assert {"no_readme_es", "no_license"} <= env.kinds(rec) and "no_readme" not in env.kinds(rec)
    assert all(i["severity"] == "info" for i in rec["issues"] if i["kind"].startswith("no_"))


def test_tracked_secret_files(env):
    r = make_repo(env.root / "secrets")
    for name in (".env", ".env.example", "certs/server.pem", "data/app.db", "client/src/data/list.json", "src/key.ts"):
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text("x\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "oops")
    (r / ".env.local").write_text("not tracked\n", encoding="utf-8")                 # untracked: not a leak (yet)
    rec = env.scan(r)
    assert sorted(rec["secrets"]) == [".env", "certs/server.pem", "data/app.db"]
    issue = next(i for i in rec["issues"] if i["kind"] == "tracked_secret")
    assert issue["severity"] == "error" and ".env.example" not in issue["text"]["en"]


# ---- drift ------------------------------------------------------------------------------------------------------

def test_vendored_drift(env):
    r = make_repo(env.root / "vend")
    vendor(env.src, r / "pkg" / "hoard_link")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "vendor")
    rec = env.scan(r)
    assert rec["drift"]["vendored"]["stale"] is False and "vendored_drift" not in env.kinds(rec)
    assert rec["drift"]["vendored"]["copies"][0]["path"] == "pkg/hoard_link"
    # upstream moves on: a changed file, a new file, a file that no longer exists upstream, the hub copy that must not be there
    (env.src / "a.py").write_text("A = 2\n", encoding="utf-8")
    (env.src / "new.py").write_text("N = 1\n", encoding="utf-8")
    (r / "pkg" / "hoard_link" / "gone.py").write_text("old\n", encoding="utf-8")
    (r / "pkg" / "hoard_link" / "hub").mkdir()
    (r / "pkg" / "hoard_link" / "hub" / "core.py").write_text("HUB\n", encoding="utf-8")
    rec = env.scan(r)
    copy = rec["drift"]["vendored"]["copies"][0]
    assert rec["drift"]["vendored"]["stale"] and copy["count"] == 4
    assert set(copy["stale"]) == {"a.py", "new.py", "gone.py", "hub/core.py"}
    assert next(i for i in rec["issues"] if i["kind"] == "vendored_drift")["severity"] == "warn"
    # the node client counts too
    (r / "server").mkdir()
    (r / "server" / "hoard-link.js").write_text("old\n", encoding="utf-8")
    rec = env.scan(r)
    assert {c["path"] for c in rec["drift"]["vendored"]["copies"]} == {"pkg/hoard_link", "server/hoard-link.js"}
    # compiled files, __pycache__ and the copy's own VENDORED.txt/LICENSE never count; node_modules/venv are not searched
    (r / "pkg" / "hoard_link" / "__pycache__").mkdir()
    (r / "pkg" / "hoard_link" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\0")
    (r / "node_modules" / "x" / "hoard_link").mkdir(parents=True)
    (r / "node_modules" / "x" / "hoard_link" / "__init__.py").write_text("stale\n")
    assert len(env.scan(r)["drift"]["vendored"]["copies"]) == 2


def test_a_directory_with_only_vendored_txt_is_a_copy(env):
    r = make_repo(env.root / "marker")
    (r / "lib" / "hoard_link").mkdir(parents=True)
    (r / "lib" / "hoard_link" / "VENDORED.txt").write_text("v\n")
    rec = env.scan(r)
    assert rec["drift"]["vendored"]["stale"] and rec["drift"]["vendored"]["copies"][0]["path"] == "lib/hoard_link"


def test_the_canonical_package_is_not_a_copy_of_itself(env):
    # a repo that IS the source (its hoard_link/ is the canonical one) has nothing to drift from
    root = env.src.parent
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "src")
    rec = env.mon.scan_repo(str(root), "src", None, env.settings)
    assert rec["drift"]["vendored"] == {"copies": [], "stale": False}
    assert rec["drift"]["theme"]["stale"] == []


def test_theme_drift(env):
    r = make_repo(env.root / "themed")
    (r / "client" / "src").mkdir(parents=True)
    (r / "static").mkdir()
    canon = (env.src / "ui" / "hoard-theme.css").read_text()
    (r / "client" / "src" / "hoard-theme.css").write_text(canon)
    (r / "static" / "hoard-theme.css").write_text("a { color: blue }\n")
    (r / "dist").mkdir()
    (r / "dist" / "hoard-theme.css").write_text("built, ignored\n")
    rec = env.scan(r)
    assert rec["drift"]["theme"] == {"copies": 2, "stale": ["static/hoard-theme.css"]}
    assert next(i for i in rec["issues"] if i["kind"] == "theme_drift")["severity"] == "info"
    (r / "static" / "hoard-theme.css").write_text(canon)
    assert env.scan(r)["drift"]["theme"]["stale"] == []


def test_manifest_drift_against_faustus(env, tmp_path):
    faustus = tmp_path / "faustus"
    (faustus / "plugins").mkdir(parents=True)
    env.settings.faustus_dir = str(faustus)
    r = make_repo(env.root / "plug")
    manifest = {"schema": 1, "id": "plug", "name": "Plug", "app": {"url_default": "http://127.0.0.1:1"}}
    (r / "faustus-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert env.scan(r)["drift"]["manifest"]["state"] == "missing_in_faustus"
    assert next(i for i in env.scan(r)["issues"] if i["kind"] == "manifest_missing")["severity"] == "info"
    (faustus / "plugins" / "plug").mkdir()
    theirs = faustus / "plugins" / "plug" / "plugin.json"
    theirs.write_text(json.dumps(dict(reversed(list(manifest.items()))), indent=4), encoding="utf-8")   # same content, other layout
    assert env.scan(r)["drift"]["manifest"] == {"state": "ok", "id": "plug", "path": str(theirs)}
    theirs.write_text(json.dumps({**manifest, "name": "Plug 2", "notes": "x"}), encoding="utf-8")
    rec = env.scan(r)
    assert rec["drift"]["manifest"]["state"] == "differs" and rec["drift"]["manifest"]["keys"] == ["name", "notes"]
    assert next(i for i in rec["issues"] if i["kind"] == "manifest_drift")["severity"] == "warn"
    theirs.write_text("{ not json", encoding="utf-8")
    assert env.scan(r)["drift"]["manifest"]["state"] == "invalid"
    env.settings.faustus_dir = None                                # no Faustus configured: not a problem, just unchecked
    rec = env.scan(r)
    assert rec["drift"]["manifest"]["state"] == "unchecked" and not ({"manifest_drift", "manifest_missing"} & env.kinds(rec))
    assert env.scan(make_repo(env.root / "other"))["drift"]["manifest"]["state"] == "n/a"


def test_scripts_and_hub_agree_on_what_is_stale(tmp_path):
    """sync_vendored.py fixes what the hub reports: after the script copies, the hub sees no drift (the real package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_vendored_under_test", Path(__file__).resolve().parents[2] / "scripts" / "sync_vendored.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = tmp_path / "apps" / "App"
    (app / "pkg" / "hoard_link" / "hub").mkdir(parents=True)
    (app / "pkg" / "hoard_link" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    (app / "pkg" / "hoard_link" / "hub" / "leftover.py").write_text("x\n")
    (app / "ui").mkdir()
    (app / "ui" / "hoard-theme.css").write_text("old\n")
    git(tmp_path, "init", "-q", "-b", "main", str(app))
    mon = RepoMonitor(str(tmp_path / "data"), lambda: RepoSettings(ci=False), lambda: [])
    before = mon.scan_repo(str(app), "App", None, RepoSettings(ci=False))
    assert before["drift"]["vendored"]["stale"] and before["drift"]["theme"]["stale"] == ["ui/hoard-theme.css"]
    mod.copy_tree(mod.SRC_PY, app / "pkg" / "hoard_link", dry=False)
    import shutil
    shutil.copy2(drift_canon(), app / "ui" / "hoard-theme.css")
    after = mon.scan_repo(str(app), "App", None, RepoSettings(ci=False))
    assert after["drift"]["vendored"] == {"copies": [{"path": "pkg/hoard_link", "kind": "python", "count": 0, "stale": []}], "stale": False}
    assert after["drift"]["theme"]["stale"] == []
    assert not (app / "pkg" / "hoard_link" / "hub").exists()


def drift_canon():
    return Path(hoard_link.__file__).parent / "ui" / "hoard-theme.css"


# ---- portfolio --------------------------------------------------------------------------------------------------

def test_portfolio_match(env, tmp_path):
    portfolio = make_repo(env.root / "portfolio-react")
    (portfolio / "src" / "data").mkdir(parents=True)
    (portfolio / "src" / "data" / "projects.ts").write_text(
        'export const projects = [{ name: "Alpha-App" }, { repo: "https://github.com/Luissalet/Gamma-Service" }];\n', encoding="utf-8")
    (portfolio / "node_modules" / "x").mkdir(parents=True)
    (portfolio / "node_modules" / "x" / "index.js").write_text("beta-app", encoding="utf-8")     # never searched
    make_repo(env.root / "alpha-app")
    make_repo(env.root / "beta-app")
    gamma = make_repo(env.root / "some folder name")
    git(gamma, "remote", "add", "origin", "git@Luissalet:Luissalet/Gamma-Service.git")
    env.settings.portfolio_dir = str(portfolio)
    snap = env.refresh()
    by = {r["name"]: r for r in snap["repos"]}
    assert by["alpha-app"]["in_portfolio"] is True                     # by folder name, case-insensitive
    assert by["beta-app"]["in_portfolio"] is False
    assert by["some folder name"]["in_portfolio"] is True              # by GitHub slug
    assert by["portfolio-react"]["in_portfolio"] is None               # the portfolio is not asked to list itself
    assert "not_in_portfolio" in {i["kind"] for i in by["beta-app"]["issues"]}
    assert all(i["kind"] != "not_in_portfolio" for i in by["alpha-app"]["issues"])
    assert next(i for i in by["beta-app"]["issues"] if i["kind"] == "not_in_portfolio")["severity"] == "info"
    env.settings.portfolio_dir = None
    assert all(r["in_portfolio"] is None for r in env.refresh()["repos"])


# ---- CI ---------------------------------------------------------------------------------------------------------

def _ci_payload(conclusion="failure", status="completed"):
    return [{"status": status, "conclusion": conclusion, "headSha": "a" * 40, "url": "https://github.com/Luissalet/Ci-App/actions/runs/1",
             "createdAt": "2026-10-01T10:00:00Z", "workflowName": "tests"}]


def ci_env(tmp_path, payload, rc=0, which=lambda n: "/usr/bin/gh", now=time.time):
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return rc, json.dumps(payload) if not isinstance(payload, str) else payload, "gh: not logged in" if rc else ""

    e = Env(tmp_path, ci_runner=runner, which=which, now=now)
    e.settings.ci = True
    e.calls = calls
    r = make_repo(e.root / "Ci-App")
    git(r, "remote", "add", "origin", "git@Luissalet:Luissalet/Ci-App.git")
    make_repo(e.root / "local-only")
    return e


def test_ci_failing_is_filled_in_afterwards(tmp_path):
    e = ci_env(tmp_path, _ci_payload())
    e.mon.refresh(wait=True)
    first = e.mon.snapshot()          # the git phase is done; CI may still be arriving: the snapshot never waits for it
    assert {r["name"] for r in first["repos"]} == {"Ci-App", "local-only"}
    assert e.mon.join_ci()
    snap = e.mon.snapshot()
    ci = next(r for r in snap["repos"] if r["name"] == "Ci-App")
    assert ci["ci"]["state"] == "failing" and ci["ci"]["workflow"] == "tests" and ci["ci"]["url"].endswith("/runs/1")
    issue = next(i for i in ci["issues"] if i["kind"] == "ci_failing")
    assert issue["severity"] == "error"
    assert snap["summary"]["ci_failing"] == 1 and snap["summary"]["errors"] == 1
    argv, timeout = e.calls[0]
    assert argv[:3] == ["gh", "run", "list"] and argv[argv.index("-R") + 1] == "Luissalet/Ci-App" and timeout == 15.0
    assert "--limit" in argv and argv[argv.index("--json") + 1] == "status,conclusion,headSha,url,createdAt,workflowName"
    assert len(e.calls) == 1                                        # the repo without a GitHub remote is not asked about
    assert next(r for r in snap["repos"] if r["name"] == "local-only")["ci"]["state"] == "unknown"


def test_ci_is_cached_for_ten_minutes(tmp_path):
    clock = [time.time()]
    e = ci_env(tmp_path, _ci_payload("success"), now=lambda: clock[0])
    e.refresh()
    e.refresh()
    assert len(e.calls) == 1
    assert next(r for r in e.mon.snapshot()["repos"] if r["name"] == "Ci-App")["ci"]["state"] == "passing"
    clock[0] += 601
    e.refresh()
    assert len(e.calls) == 2


def test_ci_quietly_unknown_without_gh_or_login(tmp_path):
    e = ci_env(tmp_path, [], which=lambda n: None)                                    # gh is not installed
    snap = e.refresh()
    assert e.calls == [] and all(r["ci"]["state"] == "unknown" for r in snap["repos"]) and not snap["ci_pending"]
    assert snap["summary"]["errors"] == 0
    e = ci_env(tmp_path / "second", [], rc=1)                                         # installed but not authenticated
    snap = e.refresh()
    ci = next(r for r in snap["repos"] if r["name"] == "Ci-App")["ci"]
    assert ci["state"] == "unknown" and len(e.calls) == 1 and snap["summary"]["errors"] == 0
    e = ci_env(tmp_path / "third", "garbage")                                         # unreadable output
    assert next(r for r in e.refresh()["repos"] if r["name"] == "Ci-App")["ci"]["state"] == "unknown"
    e = ci_env(tmp_path / "fourth", _ci_payload("success", "in_progress"))
    assert next(r for r in e.refresh()["repos"] if r["name"] == "Ci-App")["ci"]["state"] == "running"
    e = ci_env(tmp_path / "fifth", [])
    assert next(r for r in e.refresh()["repos"] if r["name"] == "Ci-App")["ci"]["state"] == "none"


def test_ci_can_be_turned_off(tmp_path):
    e = ci_env(tmp_path, _ci_payload())
    e.settings.ci = False
    snap = e.refresh()
    assert e.calls == [] and all(r["ci"]["state"] == "unknown" for r in snap["repos"])


# ---- the snapshot: cache, persistence, one refresh at a time ----------------------------------------------------

def test_snapshot_is_cached_persisted_and_one_refresh_at_a_time(env):
    a = make_repo(env.root / "a")
    with_remote(a, env.tmp)
    make_repo(env.root / "b")
    # reading never waits for git: the first read starts a scan and returns what there is
    first = env.mon.snapshot()
    assert first["repos"] == [] or first["refreshing"] or first["generated_ts"]
    assert env.mon._idle.wait(30)
    snap = env.mon.snapshot()
    assert snap["summary"]["repos"] == 2 and snap["age_s"] is not None and snap["age_s"] < 60 and not snap["refreshing"]
    assert [r["name"] for r in snap["repos"]] == ["a", "b"]
    assert snap["summary"]["never_pushed"] == 1
    # the cache is on disk: a new monitor (a restart) paints at once, with no scan
    assert json.loads(Path(env.mon.path).read_text(encoding="utf-8"))["repos"][0]["name"] == "a"
    again = RepoMonitor(str(env.tmp / "data"), lambda: env.settings, lambda: [], src_pkg=str(env.src))
    painted = again.snapshot()
    assert [r["name"] for r in painted["repos"]] == ["a", "b"] and not painted["refreshing"]
    # a second refresh while one runs does not start another
    gate = threading.Event()
    real_scan = env.mon.scan_repo

    def slow_scan(*a, **k):
        gate.wait(10)
        return real_scan(*a, **k)

    env.mon.scan_repo = slow_scan
    started = env.mon.refresh(wait=False)
    second = env.mon.refresh(wait=False)
    assert started["started"] is True and second["started"] is False and env.mon.snapshot()["refreshing"]
    gate.set()
    assert env.mon._idle.wait(30)
    assert len([e for e in env.events if e[0] == "hub.repos.scan"]) == 2     # the first scan, and this one — not three


def test_waiting_for_a_refresh_is_bounded(env):
    make_repo(env.root / "a")
    gate = threading.Event()
    real_scan = env.mon.scan_repo
    env.mon.scan_repo = lambda *a, **k: (gate.wait(10), real_scan(*a, **k))[1]
    t0 = time.monotonic()
    res = env.mon.refresh(wait=True, timeout=0.3)               # the MCP bridge gives up at 90 s: tools wait less
    assert res["refreshing"] is True and time.monotonic() - t0 < 5
    snap = env.mon.snapshot(block_first=True, wait_s=0.2)       # the very first scan is still running
    assert snap["repos"] == [] and snap["refreshing"] is True
    gate.set()
    assert env.mon._idle.wait(30) and len(env.mon.snapshot()["repos"]) == 1


def test_a_stale_snapshot_triggers_a_background_refresh(env):
    clock = [1_000_000.0]
    env.mon._now = lambda: clock[0]
    make_repo(env.root / "a")
    env.refresh()
    scans = lambda: len([e for e in env.events if e[0] == "hub.repos.scan"])  # noqa: E731
    assert scans() == 1
    clock[0] += 200
    env.mon.snapshot()
    time.sleep(0.2)
    assert scans() == 1                                                      # younger than 5 minutes: served as it is
    clock[0] += 200
    snap = env.mon.snapshot()
    assert snap["age_s"] == 400                                              # old data, returned at once
    assert env.mon._idle.wait(30)
    assert scans() == 2 and env.mon.snapshot()["age_s"] == 0


# ---- discovery --------------------------------------------------------------------------------------------------

def test_discovery_roots_extras_exclusions_and_odd_names(env, tmp_path):
    phil = make_repo(env.root / "Phileas's Hoard")
    make_repo(env.root / "plain")
    make_repo(env.root / "skipped")
    (env.root / "not a repo").mkdir()
    (env.root / ".hidden").mkdir()
    git(env.root / ".hidden", "init", "-q")
    outside = make_repo(tmp_path / "elsewhere" / "extra-one")
    faustus = make_repo(tmp_path / "LocalAI" / "faustus")
    env.settings.exclude = ["skipped"]
    env.settings.extra = [str(outside)]
    env.settings.faustus_dir = str(faustus)
    env.apps = [SimpleNamespace(id="phileas", folder=str(phil))]
    found = {d["name"]: d for d in env.mon.discover(env.settings)}
    assert set(found) == {"Phileas's Hoard", "plain", "extra-one", "faustus"}
    assert found["Phileas's Hoard"]["app"] == "phileas" and found["plain"]["app"] is None
    snap = env.refresh()
    by = {r["name"]: r for r in snap["repos"]}
    assert by["Phileas's Hoard"]["error"] is None and by["Phileas's Hoard"]["app"] == "phileas"
    # the roots are also the parents of the apps the hub lists, even when not configured
    env.settings.roots = []
    assert {d["name"] for d in env.mon.discover(env.settings)} >= {"Phileas's Hoard", "plain", "extra-one", "faustus"}
    # two folders with the same name from different roots stay distinguishable
    make_repo(tmp_path / "second" / "plain")
    env.settings.extra = [str(tmp_path / "second" / "plain")]
    names = sorted(d["name"] for d in env.mon.discover(env.settings) if d["name"].startswith("plain"))
    assert names == ["plain (repos)", "plain (second)"]


def test_lookup_by_fragment(env):
    make_repo(env.root / "Phileas's Hoard")
    make_repo(env.root / "Tantalus's Hoard")
    make_repo(env.root / "HoardLink")
    env.refresh()
    find = lambda n: env.mon.find(n)  # noqa: E731
    assert find("Phileas's Hoard")[0]["name"] == "Phileas's Hoard"
    assert find("phileas")[0]["name"] == "Phileas's Hoard"
    assert find("hoardlink")[0]["name"] == "HoardLink"
    rec, candidates = find("hoard")
    assert rec is None and set(candidates) == {"Phileas's Hoard", "Tantalus's Hoard", "HoardLink"}
    assert find("nothing") == (None, [])
    assert env.mon.detail("nothing")["ok"] is False and env.mon.detail("hoard")["ok"] is False


# ---- fetch and push command ------------------------------------------------------------------------------------

def test_fetch_updates_behind_and_changes_nothing_else(env):
    r = make_repo(env.root / "fetchme")
    bare = with_remote(r, env.tmp)
    other = env.tmp / "other"
    git(env.tmp, "clone", "-q", str(bare), str(other))
    commit(other, "n.txt", msg="elsewhere")
    git(other, "push", "-q")
    (r / "wip.txt").write_text("wip\n", encoding="utf-8")
    env.refresh()
    before = {"head": git(r, "rev-parse", "HEAD"), "branch": git(r, "branch", "--show-current"), "status": git(r, "status", "--porcelain")}
    assert next(x for x in env.mon.snapshot()["repos"])["behind"] in (0, None)
    res = env.mon.fetch("fetchme")
    assert res["ok"] and res["row"]["behind"] == 1 and res["row"]["dirty"]["untracked"] == 1
    assert next(x for x in env.mon.snapshot()["repos"])["behind"] == 1                    # the snapshot was updated in place
    after = {"head": git(r, "rev-parse", "HEAD"), "branch": git(r, "branch", "--show-current"), "status": git(r, "status", "--porcelain")}
    assert after == before and (r / "wip.txt").exists() and not (r / "n.txt").exists()
    assert ("hub.repos.fetch", {"repo": "fetchme", "ok": True, "error": None, "ms": res and env.events[-1][1]["ms"]}) in [
        e for e in env.events if e[0] == "hub.repos.fetch"]
    # --prune: a branch deleted on the remote disappears from the remote-tracking refs
    git(other, "push", "-q", "origin", "main:gone")
    git(r, "fetch", "-q")
    assert "origin/gone" in git(r, "branch", "-r")
    git(other, "push", "-q", "origin", ":gone")
    env.mon.fetch("fetchme")
    assert "origin/gone" not in git(r, "branch", "-r")


def test_fetch_failure_is_reported(env):
    r = make_repo(env.root / "broken-remote")
    bare = with_remote(r, env.tmp)
    env.refresh()
    # Move the remote away (deleting it fails on Windows: git object files are read-only).
    bare.rename(bare.with_name(bare.name + ".gone"))
    res = env.mon.fetch("broken-remote")
    assert res["ok"] is False and res["error"] and res["row"]["name"] == "broken-remote"
    assert env.mon.fetch("nope")["ok"] is False
    assert [e for e in env.events if e[0] == "hub.repos.fetch"][-1][1]["ok"] is False


def test_push_command_text(env):
    r = make_repo(env.root / "Phileas's Hoard")
    with_remote(r, env.tmp)
    commit(r, "x.txt")
    env.refresh()
    res = env.mon.push_command("phileas")
    assert res["ok"] and res["command"] == f'git -C "{r}" push' and res["unpushed"] == 1
    assert [e for e in env.events if e[0] == "hub.repos.push_command"]
    make_repo(env.root / "lonely")
    env.refresh()
    assert env.mon.push_command("lonely")["ok"] is False and "remote" in env.mon.push_command("lonely")["error"]
    assert env.mon.push_command("zzz")["ok"] is False
    # nothing was pushed by asking
    assert git(r, "rev-list", "--count", "HEAD", "--not", "--remotes") == "1"


# ---- events -----------------------------------------------------------------------------------------------------

def _make_stale_lock(r):
    lock = Path(git(r, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    return lock


def test_scan_event_and_issue_events_only_for_new_errors(env):
    r = make_repo(env.root / "evented")
    with_remote(r, env.tmp)
    make_repo(env.root / "other")
    env.refresh()
    scans = [d for t, d in env.events if t == "hub.repos.scan"]
    assert scans == [{"repos": 2, "with_issues": 1, "unpushed_total": 1, "errors": 0, "scan_errors": 0}]    # "other" was never pushed
    assert not [e for e in env.events if e[0] == "hub.repos.issue"]
    # a repo gains an error-level problem
    lock = _make_stale_lock(r)
    env.refresh()
    issues = [d for t, d in env.events if t == "hub.repos.issue"]
    assert len(issues) == 1 and issues[0]["repo"] == "evented" and issues[0]["kind"] == "stale_lock" and issues[0]["severity"] == "error"
    assert "index.lock" in issues[0]["text"] and "huérfano" in issues[0]["text"]
    # still there on the next scan: no new event
    env.refresh()
    assert len([e for e in env.events if e[0] == "hub.repos.issue"]) == 1
    assert [d for t, d in env.events if t == "hub.repos.scan"][-1]["errors"] == 1
    # gone, then back: it is news again
    lock.unlink()
    env.refresh()
    lock = _make_stale_lock(r)
    env.refresh()
    assert len([e for e in env.events if e[0] == "hub.repos.issue"]) == 2
    # warnings and infos never emit
    lock.unlink(missing_ok=True)
    commit(r, "z.txt")
    env.refresh()
    assert len([e for e in env.events if e[0] == "hub.repos.issue"]) == 2


def test_first_ever_scan_does_not_flood_the_bus_with_old_problems(env):
    r = make_repo(env.root / "old-trouble")
    _make_stale_lock(r)
    env.refresh()
    assert [t for t, _ in env.events] == ["hub.repos.scan"]
    # after a restart the previous snapshot comes from disk, so it is the baseline there too
    again = Env.__new__(Env)
    again.__dict__.update(env.__dict__)
    again.events = []
    again.mon = RepoMonitor(str(env.tmp / "data"), lambda: env.settings, lambda: [], emit=lambda t, d: again.events.append((t, d)),
                            src_pkg=str(env.src))
    again.mon.refresh(wait=True)
    assert [t for t, _ in again.events] == ["hub.repos.scan"]


def test_the_issue_text_follows_the_language(env):
    r = make_repo(env.root / "lang")
    env.refresh()
    env.mon._lang_fn = lambda: "en"
    _make_stale_lock(r)
    env.refresh()
    text = next(d for t, d in env.events if t == "hub.repos.issue")["text"]
    assert "stale index.lock" in text


def test_ci_failure_arriving_later_emits_an_issue_event(tmp_path):
    clock = [time.time()]
    e = ci_env(tmp_path, _ci_payload("success"), now=lambda: clock[0])
    e.refresh()
    assert not [x for x in e.events if x[0] == "hub.repos.issue"]
    clock[0] += 601                                   # the cached answer expires
    e.calls.clear()

    def failing(argv, timeout):
        return 0, json.dumps(_ci_payload("failure")), ""

    e.mon._ci_runner = failing
    e.refresh()
    issues = [d for t, d in e.events if t == "hub.repos.issue"]
    assert len(issues) == 1 and issues[0]["kind"] == "ci_failing" and issues[0]["repo"] == "Ci-App"
    assert issues[0]["url"] == "https://github.com/Luissalet/Ci-App"
    clock[0] += 601
    e.refresh()
    assert len([x for x in e.events if x[0] == "hub.repos.issue"]) == 1             # still failing: not news


# ---- filters ---------------------------------------------------------------------------------------------------

def test_filter_rows(env):
    a = make_repo(env.root / "pushed")
    with_remote(a, env.tmp)
    b = make_repo(env.root / "ahead")
    with_remote(b, env.tmp, name="r2.git")
    commit(b, "x.txt")
    c = make_repo(env.root / "dirty")
    with_remote(c, env.tmp, name="r3.git")
    (c / "new.txt").write_text("x")
    d = make_repo(env.root / "drifted")
    with_remote(d, env.tmp, name="r4.git")
    (d / "static").mkdir()
    (d / "static" / "hoard-theme.css").write_text("other")
    rows = env.refresh()["repos"]
    names = lambda mode, text="": sorted(r["name"] for r in filter_rows(rows, mode, text))  # noqa: E731
    assert names("all") == ["ahead", "dirty", "drifted", "pushed"]
    assert names("unpushed") == ["ahead"]
    assert names("dirty") == ["dirty", "drifted"]
    assert names("drift") == ["drifted"]
    assert names("ci_failing") == []
    assert "pushed" not in names("issues") and "ahead" in names("issues")
    assert names("all", "AHE") == ["ahead"] and names("all", "theme_drift") == ["drifted"] and names("all", "main") == names("all")
    assert names("bogus") == names("all")


# ---- through the hub: routes, tools, rule -----------------------------------------------------------------------

@pytest.fixture
def hub_with_repos(hub, family, tmp_path):
    hub.config.repos = {"ci": False, "expected_emails": [ME]}
    app = family["root"] / "Fake's Hoard"                  # an app folder (id "fake"): becomes a repo with an icon
    git(app, "init", "-q", "-b", "main")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "app")
    with_remote(app, tmp_path, name="fake.git")
    plain = make_repo(family["root"] / "plain repo")
    git(plain, "checkout", "-q", "-b", "claude/x")
    commit(plain, "p.txt")
    hub.repos.refresh(wait=True)
    return hub


@pytest.fixture
def served_repos(hub_with_repos):
    from hoard_link.hub.server import make_server
    hub = hub_with_repos
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, hub.config.url
    server.shutdown()


def test_routes(served_repos):
    hub, url = served_repos
    status, snap = _http(url + "/api/repos")
    assert status == 200 and snap["ok"] and snap["summary"]["repos"] == 2 and not snap["refreshing"]
    by = {r["name"]: r for r in snap["repos"]}
    assert by["Fake's Hoard"]["app"] == "fake" and by["plain repo"]["app"] is None
    assert by["plain repo"]["branch"] == "claude/x" and by["plain repo"]["never_pushed"] is True
    assert "issues" in by["plain repo"] and "paths" not in by["plain repo"]["dirty"]       # compact rows
    status, one = _http(url + "/api/repos/Fake%27s%20Hoard")
    assert status == 200 and one["repo"]["name"] == "Fake's Hoard" and one["repo"]["upstream"] == "origin/main" and one["repo"]["commits"]
    assert one["github_url"] is None and one["row"]["name"] == "Fake's Hoard"
    status, one = _http(url + "/api/repos/plain")                       # a fragment is enough
    assert status == 200 and one["repo"]["name"] == "plain repo"
    assert _http(url + "/api/repos/nope")[0] == 404
    status, cmd = _http(url + "/api/repos/Fake%27s%20Hoard/push-command")
    assert status == 200 and cmd["command"].endswith(' push') and "never pushes" in cmd["note"]
    status, cmd = _http(url + "/api/repos/plain%20repo/push-command")
    assert status == 409 and cmd["ok"] is False and "remote" in cmd["error"]            # nothing to push to
    assert _http(url + "/api/repos/nope/push-command")[0] == 404


def test_routes_actions(served_repos, monkeypatch):
    hub, url = served_repos
    status, res = _http(url + "/api/repos/refresh", {"wait": True})
    assert status == 200 and res["ok"] and res["summary"]["repos"] == 2
    status, res = _http(url + "/api/repos/refresh", {})                  # the page's call: returns at once
    assert status == 200 and "started" in res
    assert hub.repos._idle.wait(30)
    status, res = _http(url + "/api/repos/Fake%27s%20Hoard/fetch", {})
    assert status == 200 and res["ok"] and res["row"]["name"] == "Fake's Hoard"
    status, res = _http(url + "/api/repos/nope/fetch", {})
    assert status == 404 and res["ok"] is False
    status, res = _http(url + "/api/repos/plain%20repo/fetch", {})
    assert status == 409 and res["ok"] is False and "no remote" in res["error"]
    opened = []
    monkeypatch.setattr("hoard_link.hub.desktop.open_folder", lambda p: opened.append(p) or {"ok": True})
    status, res = _http(url + "/api/repos/Fake%27s%20Hoard/folder", {})
    assert status == 200 and [os.path.normcase(p) for p in opened] == [os.path.normcase(hub.get("fake").folder)]
    assert _http(url + "/api/repos/nope/folder", {})[0] == 404
    assert _http(url + "/api/repos/Fake%27s%20Hoard/bogus", {})[0] == 404
    assert _http(url + "/api/repos/Fake%27s%20Hoard/bogus")[0] == 404
    # same guard as every hub action: a cross-site page cannot trigger a fetch or a scan
    hdr = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}
    assert _http(url + "/api/repos/Fake%27s%20Hoard/fetch", {}, headers=hdr)[0] == 403
    assert _http(url + "/api/repos/refresh", {}, headers=hdr)[0] == 403
    assert _http(url + "/api/repos", headers=hdr)[0] == 403


def test_agent_route_calls_repo_tools(served_repos):
    hub, url = served_repos
    auth = {"Authorization": "Bearer " + hub.token}
    status, body = _http(url + "/api/agent/call", {"tool": "hub_repos", "arguments": {"filter": "unpushed"}}, headers=auth)
    assert status == 200 and body["ok"] and [r["name"] for r in body["result"]["repos"]] == ["plain repo"]
    status, body = _http(url + "/api/agent/call", {"tool": "hub_repo", "arguments": {"name": "zzz"}}, headers=auth)
    assert status == 400 and body["ok"] is False
    status, body = _http(url + "/api/agent/tools", headers=auth)
    assert {"hub_repos", "hub_repo", "hub_repos_refresh", "hub_repo_fetch", "hub_repo_push_command"} <= {t["name"] for t in body["tools"]}


def test_tools(hub_with_repos):
    hub = hub_with_repos
    cat = {t["name"]: t for t in tools.catalogue()}
    for name in ("hub_repos", "hub_repo", "hub_repos_refresh", "hub_repo_fetch", "hub_repo_push_command"):
        assert name in cat
        first = cat[name]["description"].splitlines()[0]
        assert len(first) <= 110 and cat[name]["inputSchema"]["type"] == "object"
    assert cat["hub_repos"]["annotations"]["readOnlyHint"] and cat["hub_repo"]["annotations"]["readOnlyHint"]
    assert cat["hub_repo_push_command"]["annotations"]["readOnlyHint"]
    assert cat["hub_repo_fetch"]["annotations"]["openWorldHint"] and "readOnlyHint" not in cat["hub_repo_fetch"]["annotations"]
    text = " ".join(cat["hub_repos"]["description"].lower().split())
    for kw in ("repos sin push", "commits pendientes", "ramas sueltas", "deriva de hoard_link"):
        assert kw in text
    res = tools.call(hub, "hub_repos", {})
    assert res["ok"] and res["count"] == 2 and res["summary"]["repos"] == 2
    row = next(r for r in res["repos"] if r["name"] == "plain repo")
    assert row["branch"] == "claude/x" and row["never_pushed"] and row["stray_branches"] == ["claude/x"]   # its prefix says stray
    assert "never_pushed" in row["issues"] and row["errors"] == [] and set(row) >= {"ahead", "behind", "dirty", "ci", "drift"}
    assert [r["name"] for r in tools.call(hub, "hub_repos", {"filter": "unpushed"})["repos"]] == ["plain repo"]
    assert tools.call(hub, "hub_repos", {"text": "fake"})["count"] == 1
    assert tools.call(hub, "hub_repos", {"filter": "ci_failing"})["count"] == 0
    detail = tools.call(hub, "hub_repo", {"name": "plain"})
    assert detail["ok"] and detail["name"] == "plain repo" and detail["commits"] and detail["branches"]
    assert tools.call(hub, "hub_repo", {"name": "zzz"})["ok"] is False
    assert tools.call(hub, "hub_repo_push_command", {"name": "fake"})["command"].endswith(" push")
    assert tools.call(hub, "hub_repo_fetch", {"name": "fake"})["ok"] is True
    res = tools.call(hub, "hub_repos_refresh", {})
    assert res["ok"] and res["summary"]["repos"] == 2 and res["refreshing"] is False


def test_tools_wait_for_the_very_first_scan(hub, family):
    hub.config.repos = {"ci": False}
    make_repo(family["root"] / "first")
    assert not os.path.exists(hub.repos.path)                    # nothing scanned at hub start
    res = tools.call(hub, "hub_repos", {})
    assert res["ok"] and [r["name"] for r in res["repos"]] == ["first"]
    assert os.path.exists(hub.repos.path)


def test_recommended_rule_turns_repo_issues_into_digest_items(hub):
    from hoard_link.hub.rules import example_rules, rule_matches
    rule = next(r for r in example_rules() if r["id"] == "rule-repo-issue-digest")
    assert rule["when"] == {"type": "hub.repos.issue"}
    assert rule_matches(rule, {"type": "hub.repos.issue", "source": "hub", "data": {}})
    assert not rule_matches(rule, {"type": "hub.repos.scan", "source": "hub", "data": {}})
    assert hub.rules.install_examples()["ok"] and hub.rules.get("rule-repo-issue-digest")
    hub.events.emit("hub.repos.issue", {"repo": "Phileas's Hoard", "kind": "stale_lock", "severity": "error",
                                        "text": "index.lock huérfano desde hace 60 min", "url": "https://github.com/Luissalet/Phileas"})
    deadline = time.time() + 10
    items = []
    while time.time() < deadline and not items:
        items = hub.events.query(type="digest.item")
        time.sleep(0.05)
    data = {k: v for k, v in items[0]["data"].items() if not k.startswith("_")}
    assert data == {"title": "Phileas's Hoard: index.lock huérfano desde hace 60 min",
                    "url": "https://github.com/Luissalet/Phileas", "watch": "Phileas's Hoard", "kind": "stale_lock"}


def test_the_scan_job_template_exists():
    from hoard_link.hub.jobs import example_jobs, validate_job
    job = next(j for j in example_jobs() if "repositories" in j["name"])
    assert job["then"][0]["tool"] == "hub_repos_refresh" and not validate_job(job)


def test_config_carries_repos_settings(tmp_path):
    from hoard_link.hub.config import HubConfig
    cfg = HubConfig(data_dir=str(tmp_path), repos={"faustus_dir": "D:/LocalAI/faustus", "ci": False})
    cfg.save()
    loaded = HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)})
    assert loaded.repos == {"faustus_dir": "D:/LocalAI/faustus", "ci": False}
    (tmp_path / "hub.json").write_text('{"repos": "nonsense"}', encoding="utf-8")
    assert HubConfig.load(env={"HOARD_HUB_DATA_DIR": str(tmp_path)}).repos == {}
    s = RepoSettings.from_config({"faustus_dir": "D:/LocalAI/faustus", "stray_prefixes": [], "expected_emails": ["A@B.c"]}, "/hub/faustus")
    assert s.stray_prefixes == [] and s.expected_emails == ["a@b.c"] and s.ci is True and s.default_branches == ["main", "master"]
    assert RepoSettings.from_config({}, "/hub/faustus").faustus_dir.replace("\\", "/").endswith("/hub/faustus")
    assert RepoSettings.from_config(None).stray_prefixes == ["claude/"]


def test_versions_agree():
    root = Path(__file__).resolve().parents[2]
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert pyproject == hoard_link.__version__ == HUB_VERSION == "0.5.0"
