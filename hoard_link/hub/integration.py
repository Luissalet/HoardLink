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
from threading import Lock
from uuid import uuid4

from ..atomic import read_json, write_json_atomic
from . import agora_ranks
from . import integration_checks as checks
from . import integration_git as repo_git
from .agora import AgoraError, SpaceError, normalize_resource
from .facets import Facet


API = "/api/integration"
INTEGRATION_TOKEN = object()


def submission_tests(summary):
    matches = re.findall(r"^Test-Paths:[ \t]*([^\r\n]+)", summary or "", flags=re.MULTILINE)
    return list(dict.fromkeys(repo_git.test_path(item.strip())
                              for line in matches for item in line.split(",") if item.strip()))


def delivery_contract(repo, branch, commits, files):
    if not files:
        raise repo_git.IntegrationError("Add Test-Paths with the path of the new or updated pytest file")
    tip = repo_git.branch_tip(repo, branch)
    if not commits or any(not re.fullmatch(r"[0-9a-fA-F]{7,64}", item) for item in commits):
        raise repo_git.IntegrationError("The delivery needs commit hashes")
    resolved = [repo_git.git_text(repo, "rev-parse", "--verify", commit + "^{commit}") for commit in commits]
    if tip not in resolved or not all(repo_git.ancestor(repo, commit, tip) for commit in resolved):
        raise repo_git.IntegrationError("The named branch must point to the submitted head and contain its commits")
    if set(files) - repo_git.tracked(repo, tip):
        raise repo_git.IntegrationError("A declared test file is absent from the delivery")
    return tip


def enforce_submission(agora, task, snap, summary):
    job = agora.db.one("SELECT produces FROM agora_order_jobs WHERE task_id=?", (task["id"],))
    if "integration" not in task.get("labels", []) and not (job and "code_branch" in json.loads(job["produces"])):
        return
    from_summary = re.search(r"^Repository:[ \t]*(.+)$", summary, flags=re.MULTILINE)
    name = snap["repo"] or (from_summary.group(1).strip() if from_summary else "")
    repo = agora.repo_lookup(name) if agora.repo_lookup and name else None
    try:
        if repo is None:
            raise repo_git.IntegrationError("An integration task needs a registered repository")
        files = submission_tests(summary)
        tip = delivery_contract(repo, snap["branch"], snap["commits"], files)
        fork = repo_git.git_text(repo, "merge-base", "HEAD", tip)
        if set(files) - repo_git.changed(repo, fork, tip):
            raise repo_git.IntegrationError("Test-Paths must name changed test files from this delivery")
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
        self.active = {}
        self.cancelled = set()

    def start(self):
        running = {"queued", "waiting_for_suite_slot", "testing_main", "testing_candidate", "testing_inference",
                   "integrating", "prepared_checkout", "moved_checkout", "restored_checkout", "checking_live_case",
                   "saving_baseline", "cleaning_delivery", "worktree_removed", "branch_removed"}
        for path in self.root.glob("*/receipt.json"):
            receipt = read_json(path, {})
            if receipt.get("state") in running:
                receipt["state"] = "interrupted"
                receipt["error"] = "The Hub restarted during integration. The receipt preserves the last completed step."
                write_json_atomic(path, receipt)

    def close(self):
        with self.lock:
            self.cancelled.update(self.active.values())
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

    def _delivery(self, agora, task, args):
        repo_name = task["repo"] or args.get("repo")
        if not repo_name or not (repo_path := agora.repo_lookup(repo_name)):
            raise repo_git.IntegrationError("The delivery needs a repository registered in the Hub")
        repo = Path(repo_path).resolve()
        target = repo_git.git_text(repo, "symbolic-ref", "--short", "HEAD")
        if target not in ("main", "master"):
            raise repo_git.IntegrationError("The registered repository must have its main or master branch checked out")
        base = repo_git.git_text(repo, "rev-parse", "HEAD")
        # The stored proposal is timestamped with submit and comes from the task owner.
        row = agora.db.one("SELECT body FROM messages WHERE thread_id=? AND kind='proposal' AND author=? "
                           "AND created>=? ORDER BY id LIMIT 1",
                           (task["thread_id"], task["owner"], task["submitted_at"]))
        declared = submission_tests(row["body"] if row else "")
        head = delivery_contract(repo, task["branch"], task["commits"], declared)
        fork = repo_git.git_text(repo, "merge-base", base, head)
        delta = repo_git.changed(repo, fork, head)
        if not set(declared).issubset(delta):
            raise repo_git.IntegrationError("Test-Paths must name changed test files from this delivery")
        if repo_git.ancestor(repo, head, base):
            raise repo_git.IntegrationError("The delivery is already present in the target")
        config = checks.config_at(repo, base)
        # A submission cannot disable main's configured contract files or silently move them into inference.
        files = checks.tests_for(delta, repo_git.tracked(repo, head) | repo_git.tracked(repo, base), declared, config)
        live = args.get("live_test")
        if live and repo_git.test_path(live.split("::", 1)[0]) not in files["contract"]:
            raise repo_git.IntegrationError("The live case needs a test file in the contract selection")
        return repo, {"repo": repo.name, "base": base, "head": head, "target": target,
                      "branch": task["branch"], "delta": sorted(delta), "tests": files, "live_test": live}

    def _path(self, run):
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{32}", run):
            raise repo_git.IntegrationError("Invalid integration receipt")
        return self.root / run / "receipt.json"

    def _undo_request(self, receipt):
        return {"tool": "hub_integration_undo", "arguments": {"id": receipt["id"],
                "agent": receipt["agent"], "space_id": receipt["space_id"]}}

    def inspect(self, args):
        agora, _, task = self._snapshot(args)
        repo, plan = self._delivery(agora, task, args)
        nearby = agora.db.query("SELECT id, title, status, submission_revision FROM tasks "
                                "WHERE repo=? AND space_id=? AND id<>? AND status IN ('review', 'approved') "
                                "ORDER BY updated DESC LIMIT 10", (task["repo"] or plan["repo"], agora.space_id, task["id"]))
        return {"ok": True, "plan": plan, "baseline": read_json(self._baseline(repo), {}),
                "nearby_deliveries": [dict(row) for row in nearby]}

    def run(self, args):
        merge = args.get("merge", False)
        agora, agent, task = self._snapshot(args, merge=merge)
        repo, plan = self._delivery(agora, task, args)
        if merge and not plan["live_test"]:
            raise repo_git.IntegrationError("Supply the focused live_test that reproduces the original case")
        key = (agora.space_id, task["id"])
        with self.lock:
            if existing := self.active.get(key):
                return self.status({"id": existing})
            run = uuid4().hex
            path = self._path(run)
            path.parent.mkdir(parents=True)
            receipt = {"id": run, "task_id": task["id"], "revision": task["submission_revision"],
                       "space_id": agora.space_id, "agent": agent, "plan": plan, "merge": merge,
                       "state": "queued", "machine": "spark" if args.get("spark_repo") else "local",
                       "created": time.time(), "events": []}
            receipt["undo"] = self._undo_request(receipt)
            write_json_atomic(path, receipt)
            self.active[key] = run
            self.pool.submit(self._work, args, repo, task, receipt)
        return {"ok": True, "receipt": receipt}

    def _baseline(self, repo):
        name = hashlib.sha256(os.fsencode(os.path.normcase(str(repo)))).hexdigest()
        return self.root / "baselines" / (name + ".json")

    def station(self):
        manifest = read_json(Path(__file__).resolve().parents[2] / "faustus-plugin.json", {})
        handoffs = manifest.get("x-family", {}).get("production", {}).get("handoffs", [])
        return {"app": manifest.get("id", "hoardhub"), "name": manifest.get("name", "Hoard Hub"),
                "handoffs": handoffs}

    def _run_checks(self, repo, work, test_files, receipt, *, base, ref, lane="contract", remote=None, empty_ok=False):
        files = [item for item in test_files if (work / item.split("::", 1)[0]).is_file()]
        report = self._path(receipt["id"]).parent / f"{lane}-{ref}.xml"
        return checks.suite(self.hub, repo, work, files, report, lane, base=base, ref=ref,
                            remote=remote, empty_ok=empty_ok)

    def _work(self, args, repo, task, receipt):
        run = receipt["id"]
        path = self._path(run)
        folder = path.parent
        plan = receipt["plan"]
        base, head = plan["base"], plan["head"]
        remote = args.get("spark_repo")
        agora = self._agora(args)
        agent = receipt["agent"]
        merge_resource = normalize_resource(f"merge:{repo.name}")
        took_lock = False

        def update(state, details=None):
            receipt["state"] = state
            receipt["events"].append({"state": state, "at": time.time(), **(details or {})})
            write_json_atomic(path, receipt)

        def cancelled():
            return run in self.cancelled

        try:
            slot = test_slot(checks.SUITE_LOCK, update, cancelled) if not remote else test_slot(
                folder.parent / "remote-slot.json", update, cancelled)
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
                    raise repo_git.IntegrationError("Main or candidate could not collect or set up its tests")
                if proposed["counts"]["passed"] + proposed["counts"]["failed"] == 0:
                    raise repo_git.IntegrationError("No contract test actually ran")
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
                self._snapshot(args, merge=True)
                with agora.db.tx():
                    existing = next((row for row in agora._live_locks() if row["resource"] == merge_resource), None)
                    got = agora._acquire(agent, [merge_resource], None, 900, f"Integration {run}")
                    if not got["ok"]:
                        raise repo_git.IntegrationError(f"Merge lock held by {got['conflicts'][0]['owner']}")
                    took_lock = existing is None
                if repo_git.branch_tip(repo, plan["branch"]) != head:
                    raise repo_git.IntegrationError("Delivery branch changed after its tests")
                update("integrating")
                repo_git.move_checkout(repo, base, merged, folder, update)
                update("checking_live_case")
                try:
                    with repo_git.worktree(repo, folder / "live", merged) as live_tree:
                        receipt["live"] = self._run_checks(repo, live_tree, [plan["live_test"]], receipt,
                                                           base=base, ref=merged, lane="live", remote=remote)
                    if receipt["live"]["failures"] or not receipt["live"]["counts"]["passed"]:
                        raise repo_git.IntegrationError("The post-merge case failed")
                except (repo_git.IntegrationError, OSError, ValueError) as exc:
                    self._undo_merge(path, receipt, agora, repo, agent)
                    receipt["error"] = f"{exc}. The previous checkout was restored."
                    update("rolled_back")
                    return
                prev_baseline = read_json(self._baseline(repo), {})
                receipt["previous_baseline"] = prev_baseline
                lane = receipt["machine"]
                previous = prev_baseline.get(lane, {}).get("failures", {})
                known = {key: row for key, row in previous.items() if row.get("file") not in tests}
                known.update({key: {**row, "checked_at_commit": merged} for key, row in proposed["failures"].items()})
                record = {"repo": plan["repo"], "commit": merged, "machine": receipt["machine"],
                          "tests": tests, "failures": known, "run": run}
                receipt["baseline"] = record
                update("saving_baseline")
                write_json_atomic(self._baseline(repo), {**prev_baseline, lane: record})
                self._finish(agora, args, repo, task, receipt, update)
                update("integrated")
        except Exception as exc:
            receipt["error"] = str(exc)
            moved = any(event["state"] == "moved_checkout" for event in receipt["events"])
            update("needs_attention" if moved else "failed")
        finally:
            if took_lock:
                agora.unlock({"agent": agent, "resources": [merge_resource]})
            with self.lock:
                self.active.pop((receipt["space_id"], receipt["task_id"]), None)
                self.cancelled.discard(run)

    def _finish(self, agora, args, repo, task, receipt, update):
        _, agent, approved = self._snapshot(args, merge=True)
        if approved["commits"] != task["commits"]:
            raise repo_git.IntegrationError("The approved commits changed")
        branch = receipt["plan"]["branch"]
        head = receipt["plan"]["head"]
        source = repo_git.registered_worktree(repo, branch)
        cleanup = {"worktree": str(source) if source else None, "removed_worktree": False,
                   "removed_branch": False, "head": head}
        receipt["cleanup"] = cleanup
        receipt["before_close"] = {"task": {key: approved[key] for key in
                                   ("status", "commits", "result", "review_state", "closed_at")},
                                   "thread": dict(agora.db.one(
                                       "SELECT status,resolution,resolved_by FROM threads WHERE id=?",
                                       (approved["thread_id"],))),
                                   "locks": [dict(row) for row in agora.db.query(
                                       "SELECT * FROM locks WHERE task_id=?", (task["id"],))]}
        update("cleaning_delivery")
        if source and source.resolve() == repo:
            raise repo_git.IntegrationError("The delivery checkout is the destination")
        if repo_git.branch_tip(repo, branch) != head:
            raise repo_git.IntegrationError("The delivery branch moved before cleanup")
        if source and repo_git.git(source, "status", "--porcelain", "--untracked-files=all", "--ignored").stdout:
            cleanup["retained"] = "The delivery worktree contains uncommitted or ignored files"
        else:
            if source:
                repo_git.git(repo, "worktree", "remove", str(source))
                cleanup["removed_worktree"] = True
                update("worktree_removed")
            repo_git.git(repo, "update-ref", "-d", "refs/heads/" + branch, head)
            cleanup["removed_branch"] = True
            update("branch_removed")
        note = (f"Integración {receipt['id']} en {receipt['plan']['repo']}. "
                f"Rama {branch}, entrega r{receipt['revision']}, resultado {receipt['merge_commit']}.\n\n"
                f"Pruebas de contrato con línea base. Caso repetido tras fusionar: {receipt['plan']['live_test']}.\n"
                f"{cleanup.get('retained') or 'Rama de entrega retirada; árbol limpiado si existía.'}")
        result = agora.task_done({"agent": agent, "task_id": task["id"], "result": note,
                                  "commits": [receipt["merge_commit"]], "force": True,
                                  "reason": "El commit de integración contiene íntegramente la entrega aprobada.",
                                  "_integration": INTEGRATION_TOKEN,
                                  "_integration_revision": receipt["revision"]})
        receipt["close_result"] = {key: result["task"][key] for key in ("commits", "result", "closed_at")}

    def status(self, args):
        receipt = read_json(self._path(args.get("id")), None)
        return {"ok": receipt is not None, "receipt": receipt}

    def undo(self, args):
        path = self._path(args.get("id"))
        receipt = read_json(path, None)
        if not receipt:
            raise repo_git.IntegrationError("Unknown integration receipt")
        agora = self._agora(args)
        agent = agora._agent(args.get("agent"))
        if (agora.space_id, agent) != (receipt["space_id"], receipt["agent"]):
            raise AgoraError("Only the worker that ran the integration can undo it", 403)
        with self.lock:
            if receipt["id"] in self.active.values():
                if receipt["state"] in ("queued", "waiting_for_suite_slot", "testing_main", "testing_candidate",
                                        "testing_inference"):
                    self.cancelled.add(receipt["id"])
                    return {"ok": True, "state": "cancelling"}
                raise repo_git.IntegrationError("Wait for the merge operation to finish before undoing")
            if receipt["state"] == "undone":
                return {"ok": True, "receipt": receipt}
            return self._undo_finished(path, receipt, agora, agent)

    def _undo_finished(self, path, receipt, agora, agent):
        if receipt["state"] == "interrupted" and not any(
                row["state"] == "restored_checkout" for row in receipt["events"]):
            if any(row["state"] == "prepared_checkout" for row in receipt["events"]):
                raise repo_git.IntegrationError(f"Inspect the checkout and parked patches in {path.parent}")
        moved = any(row["state"] == "moved_checkout" for row in receipt["events"])
        if moved:
            repo = Path(agora.repo_lookup(receipt["plan"]["repo"])).resolve()
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

    def _undo_merge(self, path, receipt, agora, repo, agent):
        plan = receipt["plan"]
        task = agora._task_row(receipt["task_id"])
        closed = receipt.get("close_result")
        if closed and (task["status"] != "done" or any(task[key] != value for key, value in closed.items())):
            raise repo_git.IntegrationError("The Agora task changed after integration")
        if task["submission_revision"] != receipt["revision"]:
            raise repo_git.IntegrationError("The submission revision changed")
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
        if closed:
            with agora.db.tx():
                previous = receipt["before_close"]
                values = previous["task"]
                agora.db.execute("UPDATE tasks SET status=?, commits=?, result=?, review_state=?, closed_at=?, updated=? "
                                 "WHERE id=?", (values["status"], json.dumps(values["commits"]),
                                 values["result"], values["review_state"], values["closed_at"], agora.clock(), task["id"]))
                thread = previous["thread"]
                agora.db.execute("UPDATE threads SET status=?, resolution=?, resolved_by=? WHERE id=?",
                                 (thread["status"], thread["resolution"], thread["resolved_by"], task["thread_id"]))
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
            return {"ok": bool(repo), "baseline": read_json(self._baseline(Path(repo).resolve()), {}) if repo else {}}
        receipts = [read_json(path, {}) for path in self.root.glob("*/receipt.json")]
        receipts.sort(key=lambda row: row.get("created", 0), reverse=True)
        return {"ok": True, "receipts": [self._summary(row) for row in receipts[:30] if row]}

    @staticmethod
    def _summary(row):
        return {**{key: row[key] for key in ("id", "task_id", "space_id", "revision", "state", "machine", "created")},
                "repo": row["plan"]["repo"], "branch": row["plan"]["branch"]}

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
                    "space_id": text, "agent": text, "repo": text}
        write = {**delivery, "merge": {"type": "boolean"}, "inference": {"type": "boolean"},
                 "spark_repo": text, "live_test": text}
        return [{"name": "hub_integration_" + key, "description": description,
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False}, "annotations": {"readOnlyHint": read}}
                for key, description, props, required, read in [
                    ("plan", "Find affected tests. Ver pruebas afectadas.", delivery,
                     ["task_id", "revision", "agent"], True),
                    ("run", "Check or integrate a delivery. Comprobar o fusionar una entrega.", write,
                     ["task_id", "revision", "agent"], False),
                    ("status", "Read an integration receipt. Ver un recibo de integración.", {"id": text}, ["id"], True),
                    ("undo", "Undo an integration. Deshacer una integración.",
                     {"id": text, "agent": text, "space_id": text}, ["id", "agent"], False),
                ]]

    def handlers(self):
        return {"hub_integration_" + key: self._safe(method) for key, method in
                (("plan", self.inspect), ("run", self.run), ("status", self.status), ("undo", self.undo))}
