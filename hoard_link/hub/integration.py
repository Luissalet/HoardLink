"""Agora's station for comparing, integrating and closing software deliveries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Condition, Lock
from types import SimpleNamespace
from uuid import uuid4

from ..atomic import read_json, write_json_atomic
from . import agora_ranks
from . import integration_checks as checks
from . import integration_frontend as frontend
from . import integration_git as repo_git
from . import integration_receipt
from .agora import DEFAULT_LOCK_TTL_S, AgoraError, SpaceError, normalize_resource
from .config import REPO_DIR
from .facets import Facet
from .integration_closure import closure_snapshot, restore_closure
from .integration_runtime import IntegrationRuntime, pending_reloads


API = "/api/integration"
INTEGRATION_TOKEN = object()


def submission_tests(summary, *, scoped=False):
    matches = re.findall(r"^Test-Paths:[ \t]*([^\r\n]+)", summary or "", flags=re.MULTILINE)
    # A space after a .py path is a separator, while spaces within a path are preserved.
    paths = (item.strip() for line in matches for item in re.split(r",|(?<=\.py)[ \t]+", line))
    return list(dict.fromkeys((path if scoped else repo_git.test_path(path)) for path in paths if path))


def submission_live_test(summary, name, repositories):
    matches = re.findall(r"^Live-Test:[ \t]*([^\r\n]+)", summary or "", flags=re.MULTILINE)
    cases = set()
    names = sorted(repositories or [name], key=len, reverse=True)
    for case in map(str.strip, matches):
        if not case:
            continue
        prefix = next((repo for repo in names if case.casefold().startswith(repo.casefold() + "/")), None)
        if prefix:
            if prefix.casefold() != name.casefold():
                continue
            case = case[len(prefix) + 1:]
        elif repositories:
            raise repo_git.IntegrationError("Prefix each submitted live test with its registered repository name")
        cases.add(case)
    if len(cases) > 1:
        raise repo_git.IntegrationError(f"{name}: declare one focused live test")
    return next(iter(cases), None)


def repository_tests(paths, name, repositories):
    prefixes = sorted((repo + "/" for repo in repositories), key=len, reverse=True)
    files = []
    for path in paths:
        prefix = next((p for p in prefixes if path.casefold().startswith(p.casefold())), "")
        if not prefix or prefix[:-1].casefold() == name.casefold():
            files.append((repo_git.test_path(path[len(prefix):]), bool(prefix)))
    return files


def declared_repository_tests(repo, tip, paths, name, repositories):
    tracked = repo_git.tracked(repo, tip)
    return list(dict.fromkeys(file for file, scoped in repository_tests(paths, name, repositories)
                              if scoped or file in tracked))


def validate_test_declarations(found, branch, paths):
    if not paths:
        return
    names = [name for name, _ in found.values()]
    assigned = set()
    for repo, (name, _) in found.items():
        tracked = repo_git.tracked(repo, repo_git.branch_tip(repo, branch))
        for path in paths:
            for file, scoped in repository_tests([path], name, names):
                if file in tracked:
                    assigned.add(path)
                elif scoped:
                    raise repo_git.IntegrationError(f"{name}: declared test is absent from the delivery: {file}")
    if missing := set(paths) - assigned:
        raise repo_git.IntegrationError("Declared tests absent from all delivery branches: " + ", ".join(sorted(missing)))


def submission_summary(agora, task):
    revision = task["submission_revision"]
    submitted = task.get("reviewed_commits") if task.get("status") == "done" else None
    commits = ", ".join(submitted or task["commits"]) or "(none)"
    marker = f"\n\nSubmission revision: {revision}\nCommits: {commits}"
    # Submit publishes the revision and commits together. Handover only changes the owner.
    proposals = agora.db.query("SELECT body FROM messages WHERE thread_id=? AND kind='proposal' "
                               "AND created>=? ORDER BY id", (task["thread_id"], task["submitted_at"]))
    for row in proposals:
        body = row["body"]
        if marker + "\n" in body or body.endswith(marker):
            return body.rsplit(marker, 1)[0]
    return ""


def extra_tests(args):
    provided = args.get("test_paths", [])
    if not isinstance(provided, list):
        raise repo_git.IntegrationError("test_paths must be a list of tracked pytest files")
    return list(dict.fromkeys(repo_git.test_path(file) for file in provided))


def delivery_contract(repo, branch, commits, files, *, require_tests=True):
    if require_tests and not files:
        raise repo_git.IntegrationError("Add Test-Paths with the path of a tracked pytest file")
    tip = repo_git.branch_tip(repo, branch)
    if not commits or any(not re.fullmatch(r"[0-9a-fA-F]{7,64}", item) for item in commits):
        raise repo_git.IntegrationError("The delivery needs commit hashes")
    resolved = [repo_git.resolve_commit(repo, commit) for commit in commits]
    if tip not in resolved or not all(repo_git.ancestor(repo, commit, tip) for commit in resolved):
        raise repo_git.IntegrationError(f"{repo.name}: restore {branch} to the submitted head and check its commits")
    if missing := set(files) - repo_git.tracked(repo, tip):
        raise repo_git.IntegrationError("Test files absent from the delivery: " + ", ".join(sorted(missing)))
    return tip


def enforce_submission(agora, task, snap, summary):
    job = agora.db.one("SELECT produces FROM agora_order_jobs WHERE task_id=?", (task["id"],))
    if "integration" not in task.get("labels", []) and not (job and "code_branch" in json.loads(job["produces"])):
        return
    try:
        if len(snap["commits"]) > 1:
            found = repo_git.delivery_repositories(agora, snap)
            if len(found) > 1:
                names = [name for name, _ in found.values()]
                paths = submission_tests(summary, scoped=True)
                validate_test_declarations(found, snap["branch"], paths)
                for repo, (name, commits) in found.items():
                    tip = repo_git.branch_tip(repo, snap["branch"])
                    delivery_contract(repo, snap["branch"], commits,
                                      declared_repository_tests(repo, tip, paths, name, names))
                return
        name = snap["repo"] or repo_git.delivery_repository(agora, snap, summary=summary)
        repo = agora.repo_lookup(name) if agora.repo_lookup and name else None
        if repo is None:
            raise repo_git.IntegrationError("An integration task needs a registered repository")
        files = [file for file, _ in repository_tests(submission_tests(summary, scoped=True), name, [name])]
        delivery_contract(repo, snap["branch"], snap["commits"], files)
    except repo_git.IntegrationError as exc:
        raise AgoraError(str(exc), 422, error_code="integration_contract") from exc
    snap["repo"] = name


@contextmanager
def test_slot(file, update, cancelled):
    """One contract suite per machine, including runs in other Hub instances."""
    with file.open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"\0")
            lock.flush()
        if os.name == "nt":
            import msvcrt

            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        update("waiting_for_suite_slot")
        while not cancelled():
            lock.seek(0)
            try:
                acquire()
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise repo_git.IntegrationError("Run cancelled while waiting for the suite slot")
        try:
            yield
        finally:
            lock.seek(0)
            release()


class IntegrationFacet(Facet):
    id = "integration"
    ui_scripts = ("integration.js",)

    def __init__(self, hub):
        super().__init__(hub)
        self.root = Path(hub.config.data_dir) / "integration"
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="integration")
        self.lock = Lock()
        self.queue_changed = Condition(self.lock)
        self.queue = {}
        self.active = {}
        self.cancelled = set()
        self.runtime = None

    def start(self):
        self.runtime = IntegrationRuntime(REPO_DIR)
        running = {"queued", "waiting_for_merge_lock", "waiting_for_suite_slot", "testing_main",
                   "testing_candidate", "testing_inference", "integrating", "prepared_checkout",
                   "moved_checkout", "restored_checkout", "publishing_frontend", "checking_live_case",
                   "saving_baseline", "cleaning_delivery", "delivery_retained", "worktree_removed", "branch_removed",
                   "processing_repositories"}
        for path in self.root.glob("*/receipt.json"):
            receipt = read_json(path, {})
            if receipt.get("state") in running:
                receipt["state"] = "interrupted"
                receipt["error"] = "The Hub restarted during integration. The receipt preserves the last completed step."
                write_json_atomic(path, receipt)

    def close(self):
        with self.queue_changed:
            self.cancelled.update(self.active.values())
            self.queue_changed.notify_all()
        self.pool.shutdown(wait=True, cancel_futures=False)

    def _agora(self, args):
        return self.hub.facet("agora").agora.for_space(args.get("space_id"))

    def _snapshot(self, args, *, merge=False):
        agora = self._agora(args)
        agent = agora._agent(args.get("agent"))
        task = agora._task_row(args.get("task_id"))
        if (agent != task["owner"] and not agora_ranks.outranks(agora, agent, task["owner"])
                and (merge or agent != task.get("reviewer"))):
            raise AgoraError("The delivery belongs to another worker", 403)
        revision = args.get("revision")
        if type(revision) is not int or revision < 1 or revision != task["submission_revision"]:
            raise AgoraError("Read the task and provide its current submission revision", 409)
        if task["status"] not in ("review", "approved"):
            raise AgoraError("The task needs a submitted delivery", 409)
        if merge and (task["status"] != "approved" or task["reviewed_submission_revision"] != revision
                      or set(task["commits"]) != set(task["reviewed_commits"] or [])):
            raise AgoraError("Integration requires approval of the exact submitted revision", 409)
        return agora, agent, task

    def _delivery(self, agora, task, args, repo, entry, *, repositories=()):
        summary = submission_summary(agora, task)
        repo_name, commits = entry
        target = repo_git.git_text(repo, "symbolic-ref", "--short", "HEAD")
        if target not in ("main", "master"):
            raise repo_git.IntegrationError("The registered repository must have its main or master branch checked out")
        base = repo_git.git_text(repo, "rev-parse", "HEAD")
        paths = submission_tests(summary, scoped=True)
        if repositories:
            declared = declared_repository_tests(repo, repo_git.branch_tip(repo, task["branch"]),
                                                 paths, repo_name, repositories)
        else:
            declared = [file for file, _ in repository_tests(paths, repo_name, [repo_name])]
        extra = extra_tests(args)
        live = args.get("live_test") or submission_live_test(summary, repo_name, repositories)
        live_file = repo_git.test_path(live.split("::", 1)[0]) if live else None
        test_paths = list(dict.fromkeys(declared + extra))
        head = delivery_contract(repo, task["branch"], commits, test_paths + ([live_file] if live else []),
                                 require_tests=False)
        fork = repo_git.git_text(repo, "merge-base", base, head)
        delta = repo_git.changed(repo, fork, head)
        if repo_git.ancestor(repo, head, base):
            raise repo_git.IntegrationError("The delivery is already present in the target")
        config = checks.config_at(repo, base)
        # A submission cannot disable main's configured contract files or silently move them into inference.
        tracked = repo_git.tracked(repo, head) | repo_git.tracked(repo, base)
        machine = "spark" if args.get("spark_repo") else "local"
        previous = read_json(self._baseline(repo), {}).get(machine, {})
        known_files = {row.get("file") for row in previous.get("failures", {}).values()} & tracked
        try:
            files = checks.tests_for(delta, tracked, set(test_paths) | known_files | ({live_file} if live else set()),
                                     config)
        except repo_git.IntegrationError as exc:
            raise repo_git.IntegrationError(f"{repo_name}: {exc}. Pass tracked pytest files in "
                                            "test_paths or test_paths_by_repo.") from exc
        if live and live_file not in files["contract"]:
            raise repo_git.IntegrationError(f"{repo_name}: choose a live test outside the inference lane")
        return {"repo": repo.name, "repo_ref": repo_name, "base": base, "head": head, "target": target,
                      "branch": task["branch"], "commits": commits, "delta": sorted(delta), "tests": files, "live_test": live,
                      "live_mode": "focused" if live else "contract",
                      "test_paths": test_paths, "test_paths_source": (
                          "submission_and_integrator" if declared and extra else
                          "submission" if declared else "integrator" if extra else "affected"),
                      "baseline_files": sorted(known_files),
                      "contract_timeout_seconds": checks.contract_timeout(config),
                      "test_roots": checks.test_roots(files["contract"] + files["inference"], tracked)}

    def _deliveries(self, agora, task, args, *, inspect=False):
        found = (repo_git.delivery_repositories(agora, task, args.get("repo"))
                 if len(task["commits"]) > 1 or not task["repo"] else {})
        if len(found) < 2:
            name = next(iter(found.values()))[0] if found else (
                task["repo"] or args.get("repo") or repo_git.delivery_repository(
                    agora, task, summary=submission_summary(agora, task)))
            if not name or not (location := agora.repo_lookup(name)):
                raise repo_git.IntegrationError("Register the repository and restore the local delivery branch, "
                                                "or pass its registered name as repo")
            if (hint := args.get("repo")) and (not (other := agora.repo_lookup(hint))
                                               or Path(other).resolve() != Path(location).resolve()):
                raise repo_git.IntegrationError(f"{hint}: no submitted branch in this repository")
            local = self._repository_args(args, name, [name])
            repo = Path(location).resolve()
            plan = self._delivery(agora, task, local, repo, (name, task["commits"]))
            return [(repo, plan, local)], [name]
        hint = args.get("repo")
        first = Path(agora.repo_lookup(hint)).resolve() if hint and agora.repo_lookup(hint) else None
        if hint and first not in found:
            raise repo_git.IntegrationError(f"{hint}: no submitted branch in this repository")
        names = [name for name, _ in found.values()]
        if not first and any(key in args for key in ("live_test", "test_paths", "spark_repo")):
            raise repo_git.IntegrationError("For several repositories, pass repo with a single override "
                                            "or use live_tests, test_paths_by_repo and spark_repos.")
        validate_test_declarations(found, task["branch"],
                                   submission_tests(submission_summary(agora, task), scoped=True))
        plans = []
        for repo, entry in sorted(found.items(), key=lambda item: (item[0] != first, item[1][0].casefold())):
            if inspect and first and repo != first:
                continue
            local = {key: value for key, value in args.items() if key not in ("live_test", "test_paths", "spark_repo")
                     or repo == first}
            local = self._repository_args(local, entry[0], names)
            plan = self._delivery(agora, task, local, repo, entry, repositories=names)
            plans.append((repo, plan, local))
        return plans, names

    @staticmethod
    def _repository_args(args, name, names):
        local = args.copy()
        for field, param in (("live_test", "live_tests"), ("test_paths", "test_paths_by_repo"),
                             ("spark_repo", "spark_repos")):
            mapping = args.get(param, {})
            if not isinstance(mapping, dict) or set(mapping) - set(names):
                raise repo_git.IntegrationError("Use registered delivery names as repository map keys")
            if name in mapping:
                local[field] = mapping[name]
        if any(field in local and not isinstance(local[field], str) for field in ("live_test", "spark_repo")):
            raise repo_git.IntegrationError("Spark checkouts and live tests need string values")
        return local

    def _path(self, run):
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{32}", run):
            raise repo_git.IntegrationError("Invalid integration receipt")
        return self.root / run / "receipt.json"

    def _record_path(self, receipt):
        path = self._path(receipt["id"])
        if "index" in receipt:
            return path.parent / str(receipt["index"]) / path.name
        return path

    def _undo_request(self, receipt):
        return {"tool": "hub_integration_undo", "arguments": {"id": receipt["id"],
                "agent": receipt["agent"], "space_id": receipt["space_id"]}}

    def inspect(self, args):
        agora, _, task = self._snapshot(args)
        deliveries, names = self._deliveries(agora, task, args, inspect=True)
        repo, plan, _ = deliveries[0]
        nearby = agora.db.query("SELECT id, title, status, submission_revision FROM tasks "
                                "WHERE repo=? AND space_id=? AND id<>? AND status IN ('review', 'approved') "
                                "ORDER BY updated DESC LIMIT 10", (plan["repo_ref"], agora.space_id, task["id"]))
        return {"ok": True, "plan": plan, "baseline": read_json(self._baseline(repo), {}),
                "plans": [plan for _, plan, _ in deliveries], "repositories": names,
                "missing_live_tests": [plan["repo_ref"] for _, plan, _ in deliveries if not plan["live_test"]],
                "post_merge_checks": {plan["repo_ref"]: plan["live_test"] or plan["tests"]["contract"]
                                      for _, plan, _ in deliveries},
                "nearby_deliveries": [dict(row) for row in nearby],
                "runtime": self.runtime.status() if self.runtime else None}

    def run(self, args):
        if any(type(args.get(flag, False)) is not bool for flag in ("merge", "inference")):
            raise repo_git.IntegrationError("merge and inference must be booleans")
        if any(key in args and not isinstance(args[key], str) for key in ("spark_repo", "live_test")):
            raise repo_git.IntegrationError("spark_repo and live_test must be strings")
        merge = args.get("merge", False)
        agora, agent, task = self._snapshot(args, merge=merge)
        key = (agora.space_id, task["id"])
        with self.lock:
            if existing := self.active.get(key):
                return self.status({"id": existing})
        deliveries, _ = self._deliveries(agora, task, args)
        _, plan, _ = deliveries[0]
        with self.lock:
            if existing := self.active.get(key):
                return self.status({"id": existing})
            run = uuid4().hex
            path = self._path(run)
            path.parent.mkdir(parents=True)
            receipt = {"id": run, "task_id": task["id"], "revision": task["submission_revision"],
                       "space_id": agora.space_id, "agent": agent, "plan": plan, "merge": merge,
                       "state": "queued", "machine": "spark" if deliveries[0][2].get("spark_repo") else "local",
                       "created": time.time(), "events": []}
            if self.runtime:
                receipt["runtime"] = self.runtime.status()
            receipt["undo"] = self._undo_request(receipt)
            if len(deliveries) > 1:
                receipt["repositories"] = [
                    {**receipt, "plan": plan, "index": i, "events": [],
                     "machine": "spark" if local.get("spark_repo") else "local"}
                    for i, (_, plan, local) in enumerate(deliveries)
                ]
                receipt["machine"] = "multiple" if len({row["machine"] for row in receipt["repositories"]}) > 1 else receipt["repositories"][0]["machine"]
            write_json_atomic(path, receipt)
            self.active[key] = run
            self.queue[run] = {repo for repo, _, _ in deliveries}
            self.pool.submit(self._work, args, receipt)
        return {"ok": True, "receipt": receipt}

    def _baseline(self, repo):
        name = hashlib.sha256(os.fsencode(os.path.normcase(str(repo)))).hexdigest()
        return self.root / "baselines" / (name + ".json")

    def station(self):
        manifest = read_json(Path(__file__).resolve().parents[2] / "faustus-plugin.json", {})
        return SimpleNamespace(id=manifest.get("id", "hoardhub"), name=manifest.get("name", "Hoard Hub"),
                               family=manifest.get("x-family", {}))

    def _run_checks(self, repo, work, test_files, receipt, *, base, ref, lane="contract", remote=None, empty_ok=False):
        timeout = (receipt["plan"]["contract_timeout_seconds"] if lane in ("contract", "live")
                   else checks.TIMEOUT)
        groups = {}
        for item in test_files:
            file = item.split("::", 1)[0]
            group = groups.setdefault(receipt["plan"]["test_roots"][file], [])
            group.append(item)
        results = {}
        for root_index, (root, files) in enumerate(sorted(groups.items())):
            for start in range(0, len(files), checks.BATCH_SIZE):
                batch_index = start // checks.BATCH_SIZE
                batch = [item for item in files[start:start + checks.BATCH_SIZE]
                         if (work / item.split("::", 1)[0]).is_file()]
                report = self._record_path(receipt).parent / f"{lane}-{ref}-{root_index}-{batch_index}.xml"
                key = f"{root} [{batch_index + 1}]" if len(files) > checks.BATCH_SIZE else root
                try:
                    started = time.monotonic()
                    results[key] = checks.suite(self.hub, repo, work, batch, report, lane, base=base, ref=ref,
                                                remote=remote, empty_ok=empty_ok or len(groups) > 1 or not batch,
                                                timeout=timeout)
                    results[key]["elapsed_seconds"] = round(time.monotonic() - started, 3)
                except TimeoutExpired as exc:
                    receipt["test_timeout"] = {"group": key, "commit": ref[:12], "seconds": timeout}
                    raise repo_git.IntegrationError(
                        f"{lane} tests in {key} at {ref[:12]} exceeded {timeout} s. "
                        f"Adjust {checks.CONFIG} on main or narrow the test selection.") from exc
        return checks.combine(results)

    def _repository_turn(self, run):
        for earlier, paths in self.queue.items():
            if earlier == run:
                return True
            if paths & self.queue[run]:
                return False
        return False

    def _merge_locks(self, agora, agent, run, repos, receipt, update):
        resources = sorted({normalize_resource(f"merge:{repo.name}") for repo in repos})
        deadline = time.monotonic() + 1800
        while run not in self.cancelled:
            with agora.db.tx():
                existing = {row["resource"] for row in agora._live_locks() if row["owner"] == agent}
                # Replan only after acquiring the same locks used by manual integrators.
                got = agora._acquire(agent, resources, None, DEFAULT_LOCK_TTL_S, f"Integration {run}")
            if got["ok"]:
                receipt.pop("waiting_for", None)
                return [resource for resource in resources if resource not in existing]
            if time.monotonic() >= deadline:
                owners = ", ".join(f"{row['resource']} ({row['owner']})" for row in got["conflicts"])
                raise repo_git.IntegrationError(f"Wait for these merge locks and retry: {owners}")
            if receipt.get("waiting_for") != got["conflicts"]:
                receipt["waiting_for"] = got["conflicts"]
                update("waiting_for_merge_lock")
            with self.queue_changed:
                self.queue_changed.wait_for(lambda: run in self.cancelled, timeout=1)
        return []

    def _work(self, args, receipt):
        run = receipt["id"]
        path = self._path(run)
        group = receipt.get("repositories")
        agent = receipt["agent"]
        locks = []

        def update(state):
            receipt["state"] = state
            receipt["events"].append({"state": state, "at": time.time()})
            write_json_atomic(path, receipt)

        try:
            with self.queue_changed:
                self.queue_changed.wait_for(lambda: run in self.cancelled or self._repository_turn(run))
                cancelled = run in self.cancelled
            if cancelled:
                update("cancelled")
                return
            agora, _, task = self._snapshot(args, merge=receipt["merge"])
            if receipt["merge"]:
                locks = self._merge_locks(agora, agent, run, self.queue[run], receipt, update)
                if run in self.cancelled:
                    update("cancelled")
                    return
                _, _, task = self._snapshot(args, merge=True)
            deliveries, _ = self._deliveries(agora, task, args)
            if {repo for repo, _, _ in deliveries} != self.queue[run]:
                raise repo_git.IntegrationError("The registered delivery repositories changed while queued")
            receipt["plan"] = deliveries[0][1]
            if group:
                for (_, plan, _), row in zip(deliveries, group):
                    row["plan"] = plan
            update("queued")
            if not group:
                repo, _, local = deliveries[0]
                self._work_repo(local, repo, task, receipt)
                return
            for (repo, _, local), row in zip(deliveries, group):
                self._record_path(row).parent.mkdir()
                self._work_repo(local, repo, task, row, progress=lambda state: update(
                    state if state in ("waiting_for_suite_slot", "testing_main", "testing_candidate", "testing_inference")
                    else "processing_repositories"))
                if row["state"] not in ("checked", "integrated"):
                    failure = row["state"]
                    if row.get("error"):
                        receipt["error"] = f"{row['plan']['repo']}: {row['error']}"
                    if receipt["merge"] and any(r["state"] == "integrated" for r in group):
                        self._restore_repositories(receipt, agora, agent, rollback=True)
                    update(failure)
                    return
            if receipt["merge"]:
                self._snapshot(args, merge=True)
                for (repo, _, _), row in zip(deliveries, group):
                    self._cleanup(repo, row, lambda state, details=None: update(state))
                self._close_delivery(agora, task, receipt, group)
            update("integrated" if receipt["merge"] else "checked")
        except Exception as exc:
            receipt["error"] = str(exc)
            update("needs_attention" if group and any(r.get("merge_commit") for r in group) else "failed")
        finally:
            try:
                if locks:
                    agora.unlock({"agent": agent, "resources": locks})
            finally:
                with self.queue_changed:
                    self.active.pop((receipt["space_id"], receipt["task_id"]), None)
                    self.queue.pop(run, None)
                    self.cancelled.discard(run)
                    self.queue_changed.notify_all()

    def _work_repo(self, args, repo, task, receipt, *, progress=None):
        run = receipt["id"]
        path = self._record_path(receipt)
        folder = path.parent
        plan = receipt["plan"]
        base, head = plan["base"], plan["head"]
        remote = args.get("spark_repo")
        agent = receipt["agent"]

        def update(state, details=None):
            receipt["state"] = state
            receipt["events"].append({"state": state, "at": time.time(), **(details or {})})
            write_json_atomic(path, receipt)
            if progress:
                progress(state)

        def cancelled():
            return run in self.cancelled

        try:
            agora = self._agora(args)
            slot = test_slot(checks.SUITE_LOCK, update, cancelled) if not remote else test_slot(
                self.root / "remote-slot.json", update, cancelled)
            with slot, repo_git.worktree(repo, folder / "candidate", base) as candidate:
                merged = repo_git.merge_candidate(candidate, head)
                receipt["merge_commit"] = merged
                tests = plan["tests"]["contract"]
                update("testing_main")
                with repo_git.worktree(repo, folder / "baseline", base) as baseline:
                    main = self._run_checks(repo, baseline, tests, receipt,
                                            base=base, ref=base, remote=remote, empty_ok=True)
                update("testing_candidate")
                proposed = self._run_checks(repo, candidate, tests, receipt, base=base, ref=merged, remote=remote)
                receipt["comparison"] = comparison = checks.compare(main, proposed)
                if main.get("runner_error") or proposed.get("runner_error"):
                    raise repo_git.IntegrationError("Open the comparison to check the pytest collection or setup "
                                                    "error, then fix the test environment and retry.")
                if proposed["counts"]["passed"] + proposed["counts"]["failed"] == 0:
                    raise repo_git.IntegrationError("No contract test ran. Select a pytest file outside the inference lane.")
                if args.get("inference"):
                    update("testing_inference")
                    receipt["inference"] = {
                        "marked": self._run_checks(repo, candidate, tests, receipt, base=base, ref=merged,
                                                   lane="marked_inference", remote=remote, empty_ok=True),
                        "files": self._run_checks(repo, candidate, plan["tests"]["inference"], receipt,
                                                  base=base, ref=merged, lane="inference", remote=remote),
                    }
                if cancelled():
                    update("cancelled")
                    return
                if comparison["new"] or not receipt["merge"]:
                    update("regressions" if comparison["new"] else "checked")
                    return
                if any(lane["failures"] for lane in receipt.get("inference", {}).values()):
                    update("inference_failed")
                    return
                build = frontend.prepare_frontend(candidate, repo_git.changed(repo, base, merged), folder)
                if cancelled():
                    update("cancelled")
                    return
                self._snapshot(args, merge=True)
                if repo_git.branch_tip(repo, plan["branch"]) != head:
                    raise repo_git.IntegrationError("Delivery branch changed after its tests")
                if build:
                    frontend.prepare_publication(repo, build, folder)
                    receipt["frontend_build"] = build
                update("integrating")
                repo_git.move_checkout(repo, base, merged, folder, update)
                try:
                    if build:
                        build["publication_started"] = True
                        update("publishing_frontend")
                        frontend.publish_frontend(repo, build, folder)
                        build["published"] = True
                    self._reload_apps(agora, repo, agent, receipt, "app_reload")
                    receipt["reload_pending"] = pending_reloads(repo, base, merged, build, receipt["app_reload"])
                    update("checking_live_case")
                    if any(result["state"] == "failed" for result in receipt["app_reload"]):
                        raise repo_git.IntegrationError("A running app did not come back ready after integration")
                    live = plan["live_test"]
                    with repo_git.worktree(repo, folder / "live", merged) as live_tree:
                        receipt["live"] = self._run_checks(repo, live_tree, [live] if live else tests, receipt,
                                                           base=base, ref=merged, lane="live", remote=remote)
                    result = receipt["live"]
                    failures = result["failures"] if live else checks.compare(proposed, result)["new"]
                    if result.get("runner_error") or failures or not result["counts"]["passed"]:
                        raise repo_git.IntegrationError("The post-merge check failed. Inspect the live test results before retrying.")
                except Exception as exc:
                    self._undo_merge(path, receipt, agora, repo, agent)
                    receipt["error"] = f"{exc}. The previous checkout was restored."
                    update("rolled_back")
                    return
                prev_baseline = read_json(self._baseline(repo), {})
                receipt["previous_baseline"] = prev_baseline
                lane = receipt["machine"]
                known = {key: {**row, "checked_at_commit": merged} for key, row in proposed["failures"].items()}
                record = {"repo": plan["repo"], "commit": merged, "machine": receipt["machine"],
                          "tests": tests, "failures": known, "run": run}
                receipt["baseline"] = record
                update("saving_baseline")
                write_json_atomic(self._baseline(repo), {**prev_baseline, lane: record})
                if progress is None:
                    self._finish(agora, args, repo, task, receipt, update)
                update("integrated")
        except Exception as exc:
            receipt["error"] = str(exc)
            moved = any(event["state"] == "moved_checkout" for event in receipt["events"])
            update("needs_attention" if moved else "failed")

    def _reload_apps(self, agora, repo, agent, receipt, field):
        results = receipt[field] = []
        for app in getattr(self.hub, "apps", []):
            if not app.launch or not Path(app.launch.cwd).resolve().is_relative_to(repo):
                continue
            state = self.hub.app_status(app)
            prior = next((row for row in receipt.get("app_reload", []) if row["app"] == app.id), None)
            recover = field == "undo_app_reload" and prior and prior["state"] == "failed" and prior.get("stopped")
            if state["state"] != "running" and not recover:
                continue
            if state["state"] == "running" and (app.kind == "window-app" or state.get("windows")):
                results.append({"app": app.id, "state": "deferred", "reason_code": "desktop_in_use",
                                "reason": "Desktop app is running or has an open Hub window"})
                continue
            if not state.get("stoppable") and not recover:
                results.append({"app": app.id, "state": "deferred", "reason": "App process is not managed locally"})
                continue
            resource = normalize_resource(f"app:{app.id}")
            with agora.db.tx():
                existing = any(row["resource"] == resource for row in agora._live_locks())
                got = agora._acquire(agent, [resource], None, 900, "Reload integrated application code")
            if not got["ok"]:
                results.append({"app": app.id, "state": "deferred", "reason": "App lease is held by another worker"})
                continue
            try:
                result = self.hub.start(app.id, automatic=True) if recover else self.hub.restart(app.id, automatic=True)
                deferred = result.get("skipped") or self.hub.automation_blocked(app.id)
                ready = result.get("ok") and result.get("ready")
                results.append({"app": app.id, "state": "deferred" if deferred else "reloaded" if ready else "failed",
                                "reason": result.get("reason") or result.get("error"), "pid": result.get("pid"),
                                "stopped": bool(result.get("stopped", {}).get("ok"))})
            finally:
                if not existing:
                    agora.unlock({"agent": agent, "resources": [resource]})

    def _finish(self, agora, args, repo, task, receipt, update):
        _, agent, approved = self._snapshot(args, merge=True)
        if approved["commits"] != task["commits"]:
            raise repo_git.IntegrationError("The approved commits changed")
        self._cleanup(repo, receipt, update)
        self._close_delivery(agora, task, receipt, [receipt])

    def _cleanup(self, repo, receipt, update):
        branch = receipt["plan"]["branch"]
        head = receipt["plan"]["head"]
        source = repo_git.registered_worktree(repo, branch)
        cleanup = {"worktree": str(source) if source else None, "removed_worktree": False,
                   "removed_branch": False, "head": head}
        receipt["cleanup"] = cleanup
        update("cleaning_delivery")
        if source and source.resolve() == repo:
            raise repo_git.IntegrationError("The delivery checkout is the destination")
        if repo_git.branch_tip(repo, branch) != head:
            raise repo_git.IntegrationError("The delivery branch moved before cleanup")
        if source and not repo_git.discard_delivery_caches(source):
            cleanup["retained"] = "The delivery worktree contains uncommitted or ignored files"
            update("delivery_retained", {"worktree": str(source), "reason": cleanup["retained"]})
        else:
            if source:
                repo_git.git(repo, "worktree", "remove", str(source))
                cleanup["removed_worktree"] = True
                update("worktree_removed")
            repo_git.git(repo, "update-ref", "-d", "refs/heads/" + branch, head)
            cleanup["removed_branch"] = True
            update("branch_removed")
    def _close_delivery(self, agora, task, receipt, rows):
        receipt["before_close"] = closure_snapshot(agora, task)
        note = f"Integración {receipt['id']} · entrega r{receipt['revision']}.\n"
        note += "\n".join(f"{row['plan']['repo']} · {row['merge_commit']} · {row['plan']['live_test']}. "
                            f"{row['cleanup'].get('retained') or 'Rama de entrega retirada.'}" for row in rows)
        write_json_atomic(self._path(receipt["id"]), receipt)
        result = agora.task_done({"agent": receipt["agent"], "task_id": task["id"], "result": note,
                                  "commits": [row["merge_commit"] for row in rows],
                                  "_integration": INTEGRATION_TOKEN,
                                  "_integration_revision": receipt["revision"]})
        receipt["close_result"] = {key: result["task"][key] for key in ("commits", "result", "closed_at")}

    def _read_receipt(self, path):
        receipt = read_json(path, None)
        if receipt:
            agora = self._agora(receipt)
            return integration_receipt.apply_correction(receipt, integration_receipt.correction_for(agora, receipt["id"]))
        return None

    def status(self, args):
        receipt = self._read_receipt(self._path(args.get("id")))
        return {"ok": receipt is not None, "receipt": receipt,
                "runtime": self.runtime.status() if self.runtime else None}

    def reconcile(self, args):
        path = self._path(args.get("id"))
        agora = self._agora(args)
        with self.lock, agora.db.tx():
            receipt = read_json(path, None)
            if not receipt or receipt["space_id"] != agora.space_id:
                raise repo_git.IntegrationError("Unknown integration receipt in this space")
            if receipt["id"] in self.active.values():
                raise repo_git.IntegrationError("Wait for the integration to finish")
            agent = agora._agent(args.get("agent"))
            receipt, inverse = integration_receipt.reconcile(agora, agent, receipt, args)
        result = {"ok": True, "receipt": receipt}
        if inverse:
            result["undo"] = {"tool": "hub_integration_reconcile", "arguments": {
                "id": receipt["id"], "agent": agent, "space_id": agora.space_id, **inverse}}
        return result

    def undo(self, args):
        path = self._path(args.get("id"))
        receipt = self._read_receipt(path)
        if not receipt:
            raise repo_git.IntegrationError("Unknown integration receipt")
        agora = self._agora(args)
        agent = agora._agent(args.get("agent"))
        if (agora.space_id, agent) != (receipt["space_id"], receipt["agent"]):
            raise AgoraError("Only the worker that ran the integration can undo it", 403)
        with self.lock:
            if receipt["id"] in self.active.values():
                if receipt["state"] in ("queued", "waiting_for_merge_lock", "waiting_for_suite_slot", "testing_main", "testing_candidate",
                                        "testing_inference"):
                    self.cancelled.add(receipt["id"])
                    self.queue_changed.notify_all()
                    return {"ok": True, "state": "cancelling"}
                raise repo_git.IntegrationError("Wait for the merge operation to finish before undoing")
            if receipt["state"] == "undone":
                return {"ok": True, "receipt": receipt}
            if "repositories" in receipt:
                self._check_undo_task(agora, receipt)
                try:
                    self._restore_repositories(receipt, agora, agent)
                except (AgoraError, repo_git.IntegrationError, OSError) as exc:
                    receipt["state"] = "needs_attention"
                    receipt["error"] = str(exc)
                    write_json_atomic(path, receipt)
                    raise
                self._restore_task(agora, receipt, agent)
                receipt["state"] = "undone"
                receipt.pop("error", None)
                write_json_atomic(path, receipt)
                return {"ok": True, "receipt": receipt}
            return self._undo_finished(path, receipt, agora, agent)

    def _restore_repositories(self, receipt, agora, agent, *, rollback=False):
        rows = [row for row in reversed(receipt["repositories"]) if row["state"] not in ("undone", "rolled_back")
                and any(event["state"] == "prepared_checkout" for event in row["events"])]
        for row in rows:
            states = {event["state"] for event in row["events"]}
            if "restored_checkout" not in states and ("moved_checkout" in states or receipt["state"] == "interrupted"):
                raise repo_git.IntegrationError(f"Inspect the parked patches in {self._record_path(row).parent}")
            if "moved_checkout" in states and not row.get("undo_checkout"):
                plan = row["plan"]
                location = agora.repo_lookup(plan.get("repo_ref", plan["repo"]))
                if not location or repo_git.git_text(location, "rev-parse", "HEAD") != row["merge_commit"]:
                    raise repo_git.IntegrationError(f"{plan['repo']}: the merged checkout has changed")
        for row in rows:
            self._undo_finished(self._record_path(row), row, agora, agent)
            if rollback:
                row["state"] = "rolled_back"
            write_json_atomic(self._path(receipt["id"]), receipt)

    def _undo_finished(self, path, receipt, agora, agent):
        if receipt["state"] == "interrupted" and not any(
                row["state"] == "restored_checkout" for row in receipt["events"]):
            if any(row["state"] == "prepared_checkout" for row in receipt["events"]):
                raise repo_git.IntegrationError(f"Inspect the checkout and parked patches in {path.parent}")
        moved = any(row["state"] == "moved_checkout" for row in receipt["events"])
        if moved:
            plan = receipt["plan"]
            location = agora.repo_lookup(plan.get("repo_ref", plan["repo"]))
            if location is None:
                raise repo_git.IntegrationError("Register the original repository in the Hub before undoing")
            repo = Path(location).resolve()
            resource = normalize_resource(f"merge:{repo.name}")
            with agora.db.tx():
                existing = next((row for row in agora._live_locks() if row["resource"] == resource), None)
                if not agora._acquire(agent, [resource], None, 900, "Undo integration")["ok"]:
                    raise repo_git.IntegrationError("The merge lock belongs to another worker")
            try:
                self._undo_merge(path, receipt, agora, repo, agent)
            finally:
                if existing is None:
                    agora.unlock({"agent": agent, "resources": [resource]})
        receipt["state"] = "undone"
        write_json_atomic(path, receipt)
        return {"ok": True, "receipt": receipt}

    def _check_undo_task(self, agora, receipt):
        task = agora._task_row(receipt["task_id"])
        closed = receipt.get("close_result")
        if closed and (task["status"] != "done" or any(task[key] != value for key, value in closed.items())):
            raise repo_git.IntegrationError("The Agora task changed after integration")
        if task["submission_revision"] != receipt["revision"]:
            raise repo_git.IntegrationError("The submission revision changed")

    def _undo_merge(self, path, receipt, agora, repo, agent):
        self._check_undo_task(agora, receipt)
        plan = receipt["plan"]
        recovery = path.parent / "undo"
        recovery.mkdir(exist_ok=True)
        checkout_was_restored = any(row["state"] == "restored_checkout" for row in receipt["events"])
        if not checkout_was_restored:
            raise repo_git.IntegrationError(f"Restore the parked patches from {path.parent} before undoing")
        cleanup = receipt.get("cleanup", {})
        worktree = cleanup.get("worktree")
        if cleanup.get("removed_worktree") and Path(worktree).exists() and any(Path(worktree).iterdir()):
            raise repo_git.IntegrationError("The former delivery worktree path is occupied")
        if not receipt.get("undo_checkout"):
            repo_git.move_checkout(repo, receipt["merge_commit"], plan["base"], recovery,
                                   lambda state, data: None)
            receipt["undo_checkout"] = True
            write_json_atomic(path, receipt)
        build = receipt.get("frontend_build")
        if build and build.get("publication_started") and not build.get("restored"):
            frontend.restore_frontend(repo, build, path.parent)
            build["restored"] = True
            write_json_atomic(path, receipt)
        if "app_reload" in receipt:
            self._reload_apps(agora, repo, agent, receipt, "undo_app_reload")
            receipt["reload_pending"] = pending_reloads(repo, receipt["merge_commit"], plan["base"],
                                                        build, receipt["undo_app_reload"])
            write_json_atomic(path, receipt)
        if cleanup.get("removed_branch"):
            branch = repo_git.git(repo, "show-ref", "--verify", "--hash", "refs/heads/" + plan["branch"], check=False)
            if branch.returncode:
                repo_git.git(repo, "update-ref", "refs/heads/" + plan["branch"], plan["head"], "0" * len(plan["head"]))
            elif branch.stdout.decode().strip() != plan["head"]:
                raise repo_git.IntegrationError("The delivery branch was reused after cleanup")
        if cleanup.get("removed_worktree") and repo_git.registered_worktree(repo, plan["branch"]) is None:
            repo_git.git(repo, "worktree", "add", worktree, plan["branch"])
        if read_json(self._baseline(repo), {}).get(receipt["machine"], {}).get("run") == receipt["id"]:
            write_json_atomic(self._baseline(repo), receipt["previous_baseline"])
        self._restore_task(agora, receipt, agent)

    def _restore_task(self, agora, receipt, agent):
        if receipt.get("close_result"):
            with agora.db.tx():
                self._check_undo_task(agora, receipt)
                task = agora._task_row(receipt["task_id"])
                previous = receipt["before_close"]
                restore_closure(agora, task, previous)
                for lock in previous["locks"]:
                    agora._acquire(lock["owner"], [lock["resource"]], task["id"], lock["ttl_s"], lock["note"])
                agora._system(task, f"Se ha deshecho la integración {receipt['id']}.", agent)

    def get(self, req):
        if req.path != API:
            return None
        if run := req.q("id"):
            return self._safe(self.status)({"id": run})
        if req.q("repo"):
            agora = self._agora({"space_id": req.q("space_id")})
            repo = agora.repo_lookup(req.q("repo"))
            baseline = read_json(self._baseline(Path(repo).resolve()), {}) if repo else {}
            for lane, record in baseline.items():
                patch = integration_receipt.correction_for(agora, record.get("run"))
                if patch and record.get("repo") == patch["repo"]:
                    baseline[lane] = integration_receipt.corrected_baseline(record, patch)
            return {"ok": bool(repo), "baseline": baseline}
        receipts = [read_json(path, {}) for path in self.root.glob("*/receipt.json")]
        receipts.sort(key=lambda row: row.get("created", 0), reverse=True)
        return {"ok": True, "receipts": [self._summary(row) for row in receipts[:30] if row],
                "runtime": self.runtime.status() if self.runtime else None}

    @staticmethod
    def _summary(row):
        return {**{key: row[key] for key in ("id", "task_id", "space_id", "revision", "state", "machine", "created")},
                "repo": ", ".join(child["plan"]["repo"] for child in row.get("repositories", [row])),
                "repositories": [{"repo": child["plan"]["repo"], "state": child["state"],
                                  "cleanup": child.get("cleanup"),
                                  "reload_pending": child.get("reload_pending", [])}
                                 for child in row.get("repositories", [])],
                "branch": row["plan"]["branch"],
                "cleanup": row.get("cleanup"), "reload_pending": row.get("reload_pending", [])}

    @staticmethod
    def _safe(method):
        def call(args):
            try:
                return method(args)
            except (AgoraError, SpaceError, repo_git.IntegrationError, OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc), "status": getattr(exc, "status", 400)}
        return call

    @classmethod
    def tools(cls):
        text = {"type": "string", "minLength": 1}
        delivery = {"task_id": {"type": "integer"}, "revision": {"type": "integer"},
                    "space_id": text, "agent": text, "repo": text,
                    "test_paths": {"type": "array", "items": text,
                                   "description": "Add tracked pytest files to the submission and affected tests."},
                    "test_paths_by_repo": {"type": "object", "additionalProperties": {"type": "array", "items": text}}}
        delivery.update(live_test=text, live_tests={"type": "object", "additionalProperties": text},
                        spark_repo=text, spark_repos={"type": "object", "additionalProperties": text})
        write = {**delivery, "merge": {"type": "boolean"}, "inference": {"type": "boolean"}}
        return [{"name": "hub_integration_" + key, "description": description,
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False}, "annotations": {"readOnlyHint": read}}
                for key, description, props, required, read in [
                    ("plan", "Find affected tests. Ver pruebas afectadas.", delivery,
                     ["task_id", "revision", "agent"], True),
                    ("run", "Check or integrate a delivery. Comprobar o fusionar una entrega.", write,
                     ["task_id", "revision", "agent"], False),
                    ("status", "Read an integration receipt. Ver un recibo de integración.", {"id": text}, ["id"], True),
                    ("reconcile", "Record a verified merge and reason. Corregir un recibo con la fusión verificada y el motivo.",
                     {"id": text, "agent": text, "space_id": text, "repo": text, "commit": text, "reason": text,
                      "restore": {"type": "boolean"}}, ["id", "agent"], False),
                    ("undo", "Undo an integration. Deshacer una integración.",
                     {"id": text, "agent": text, "space_id": text}, ["id", "agent"], False),
                ]]

    def handlers(self):
        return {"hub_integration_" + key: self._safe(method) for key, method in
                (("plan", self.inspect), ("run", self.run), ("status", self.status),
                 ("reconcile", self.reconcile), ("undo", self.undo))}
