"""Import measured suite failures without running suites or exposing receipt internals."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from ..atomic import read_json, write_json_atomic
from .config import REPO_DIR


def case_key(node):
    """Match pytest node IDs and JUnit class/name pairs, including test classes."""
    identifier, bracket, parameters = node.partition("[")
    *scope, name = identifier.replace("\\", "/").split("::")
    return ".".join(part.removesuffix(".py").replace("/", ".") for part in scope) + "::" + name + bracket + parameters


def observation_date(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None
    except (TypeError, ValueError):
        return None


def import_registry(registry, comparison):
    repos = {}
    for repo in registry["repositories"]:
        rows = {}
        commits = set()
        for run in repo.get("results", {}).values():
            commit = run.get("commit") or repo.get("commit")
            if not commit:
                continue
            if run.get("status") in ("passed", "failed"):
                commits.add(commit)
            stamp = observation_date(run.get("started_at"))
            for issue in run.get("issues", []):
                if issue["kind"] not in ("failure", "error") or not issue.get("case"):
                    continue
                test = issue["case"]
                key = case_key(test)
                prior = rows.get(key)
                dates = [date for date in (stamp, prior and prior["first_seen"]) if date]
                first = min(dates, default=None)
                if prior and (prior["last_seen"] or "") > (stamp or ""):
                    prior["first_seen"] = first
                    continue
                rows[key] = {"test": test, "commit": commit, "first_seen": first, "last_seen": stamp}
        repos[repo["name"].casefold()] = {
            "repo": repo["name"], "commit": repo.get("commit"), "status": repo.get("status"),
            "measured": bool(commits), "failures": rows,
        }
    comparison = list(comparison)
    current_comparisons = {row["repo"].casefold() for row in comparison
                           if row.get("historical_commit") and
                           ((repos.get(row["repo"].casefold()) or {}).get("commit") or "")
                           .startswith(row["historical_commit"])}
    for row in comparison:
        name = row["repo"].casefold()
        repo = repos.get(name)
        node = row.get("local_matched_node") or row["test"]
        case = case_key(node)
        if not repo or not row.get("local_sha") or row["local_code"] not in ("pass", "fail", "error"):
            continue
        historical = case_key(row["test"])
        prior = repo["failures"].get(historical)
        if prior and row["historical_commit"] and prior["commit"].startswith(row["historical_commit"]):
            del repo["failures"][historical]
        elif not prior and name in current_comparisons and repo["status"] in ("partial", "incomplete"):
            # A partial run may omit older batches that the focused comparison measured.
            prior = repo["failures"].get(case, {"first_seen": None})
        else:
            continue
        if row["local_code"] in ("fail", "error"):
            repo["failures"][case] = {**prior, "test": node, "commit": row["local_sha"], "last_seen": None}
            repo["measured"] = True
    return repos


class SuiteBaseline:
    def __init__(self, data_dir):
        self.cache = Path(data_dir) / "suite-baseline.json"
        self.lock = Lock()
        self.version = None
        self.data = None
        self.first_seen = {}

    @staticmethod
    def sources(data_dir):
        registry = os.environ.get("HOARD_HUB_SUITE_BASELINE")
        comparison = os.environ.get("HOARD_HUB_SUITE_COMPARISON")
        if registry:
            return Path(registry), Path(comparison) if comparison else None
        if data_dir == REPO_DIR / "data":
            for parent in REPO_DIR.parents:
                qa = parent / "Research" / "_qa"
                candidate = qa / "lineabase" / "lineabase.json"
                if candidate.is_file():
                    return candidate, qa / "publicado3733" / "comparativa.csv"
        return None, None

    def read(self, repo=None):
        with self.lock:
            data = self._load()
        rows = [row for name, row in sorted(data.items()) if repo is None or repo.casefold() == name]
        return [{**row, "failures": sorted(row["failures"].values(), key=lambda case: case["test"])}
                for row in rows]

    def _load(self):
        paths = self.sources(self.cache.parent.resolve())
        if self.data is None:
            saved = read_json(self.cache, {})
            self.data = saved.get("repositories", {})
            self.first_seen = saved.get("first_seen", {})
        try:
            if not paths[0] or not paths[0].is_file():
                return self.data
            version = tuple((path, path.stat().st_mtime_ns, path.stat().st_size) if path and path.is_file() else None
                            for path in paths)
            if version == self.version:
                return self.data
            registry = json.loads(paths[0].read_text(encoding="utf-8-sig"))
            if version[1]:
                with paths[1].open(encoding="utf-8-sig", newline="") as source:
                    repos = import_registry(registry, csv.DictReader(source))
            else:
                repos = import_registry(registry, ())
        except (OSError, ValueError, TypeError, AttributeError, KeyError, csv.Error):
            return self.data
        for name, row in repos.items():
            previous = self.first_seen.setdefault(name, {})
            for key, case in row["failures"].items():
                dates = [date for date in (case["first_seen"], previous.get(key)) if date]
                case["first_seen"] = min(dates, default=None)
                if case["first_seen"]:
                    previous[key] = case["first_seen"]
        write_json_atomic(self.cache, {"repositories": repos, "first_seen": self.first_seen})
        self.version, self.data = version, repos
        return repos


def focal_failures(result, declared, baseline):
    """Split only failures covered by the submission's declared files."""
    known = {case_key(row["test"]) for row in baseline["failures"]}
    reds = {}
    for key, failure in result["failures"].items():
        file = failure.get("file")
        prefix = next((root + "/" for root in result.get("roots", {}) if key.startswith(root + "/")), "")
        node = key.removeprefix(prefix)
        if file in declared or (not file and any(
                node.startswith(Path(path).with_suffix("").as_posix().replace("/", ".") + separator)
                for path in declared for separator in (".", "::"))):
            reds[key] = (case_key(node) in known, node)
    return reds


def closure_report(repo, reds):
    return {"repo": repo, "new": sorted({node for known, node in reds.values() if not known}),
            "known": sorted({node for known, node in reds.values() if known})}


def runner_failed(result, reds):
    """Collection errors always invalidate the run. Known individual errors are measured reds."""
    errors = result.get("case_errors", [])
    return result.get("runner_error") and (not errors or any(not reds.get(key, (False,))[0] for key in errors))
