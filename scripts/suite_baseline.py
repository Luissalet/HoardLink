"""Run configured test suites sequentially and record their exact Git revision."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from uuid import uuid4
from xml.etree import ElementTree


COUNTS = ("tests", "passed", "failed", "errors", "skipped")


def git(cwd, *args):
    result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed in {cwd}")
    return result.stdout.strip()


def revision(cwd):
    if git(cwd, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"Tracked files have local changes in {cwd}")
    return git(cwd, "rev-parse", "HEAD")


def batch_definition(entry, batch):
    return {"cwd": entry["cwd"], "env": entry.get("env", {}), "batch": batch}


def junit(path):
    root = ElementTree.parse(path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError(f"Expected a JUnit report, got <{root.tag}>")
    counts = dict.fromkeys(COUNTS, 0)
    issues = []
    for case in root.iter("testcase"):
        counts["tests"] += 1
        # A test can have both a call failure and a teardown error. Count the
        # case once and retain both diagnostics.
        outcome = "passed"
        for tag, status in (("skipped", "skipped"), ("failure", "failed"), ("error", "errors")):
            for detail in case.findall(tag):
                outcome = status
                if tag != "skipped":
                    name = "::".join(filter(None, (case.get("classname"), case.get("name"))))
                    diagnostic = (detail.text or "").strip()
                    issues.append({
                        "case": name, "kind": tag, "message": (detail.get("message") or diagnostic).strip(),
                        "file": case.get("file"), "line": case.get("line"), "detail": diagnostic,
                    })
        counts[outcome] += 1
    if not counts["tests"]:
        raise ValueError("JUnit report contains no test cases")
    if root.get("name") == "pytest tests":
        suite = root.find("testsuite")
        if suite is not None and all(
            int(suite.get(attribute, "-1")) == counts[outcome]
            for attribute, outcome in (("failures", "failed"), ("errors", "errors"), ("skipped", "skipped"))
        ):
            # Pytest can include successful unittest subtests in its totals
            # without writing separate testcase elements for them.
            extra_passes = int(suite.get("tests", "0")) - counts["tests"]
            if extra_passes > 0:
                counts["tests"] += extra_passes
                counts["passed"] += extra_passes
    return counts, issues


def record_batch(batch, repo, cwd, artifacts):
    artifacts.mkdir()
    report = artifacts / "junit.xml"
    log = artifacts / "output.log"
    replacements = {"{repo}": str(cwd), "{report}": str(report), "{artifacts}": str(artifacts)}

    def expand(value):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value

    working_dir = (cwd / batch.get("cwd", ".")).resolve()
    if not working_dir.is_relative_to(cwd):
        raise ValueError(f"Batch directory is outside the worktree: {working_dir}")
    command = [expand(value) for value in batch["command"]]
    env = {**repo.get("env", {}), **batch.get("env", {})}
    overrides = {key: None if value is None else expand(value) for key, value in env.items()}
    process_env = os.environ.copy()
    for key, value in overrides.items():
        if value is None:
            process_env.pop(key, None)
        else:
            process_env[key] = value

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    clock = perf_counter()
    attempt = {
        "command": command, "cwd": str(working_dir), "env": overrides,
        "started_at": started, "exit_code": None, "counts": None,
        "report": str(report), "log": str(log), "issues": [],
        "definition": batch_definition(repo, batch),
    }
    with log.open("wb") as output:
        try:
            process = subprocess.run(
                command, cwd=working_dir, env=process_env, stdout=output, stderr=subprocess.STDOUT,
            )
            attempt["exit_code"] = process.returncode
        except OSError as error:
            attempt["problem"] = str(error)
    attempt["seconds"] = round(perf_counter() - clock, 3)

    if attempt["exit_code"] is not None:
        try:
            attempt["counts"], attempt["issues"] = junit(report)
        except (OSError, ValueError, ElementTree.ParseError) as error:
            attempt["problem"] = f"No usable JUnit report: {error}"
        code = attempt["exit_code"]
        if attempt["counts"] and code not in batch.get("result_exit_codes", [0, 1]):
            attempt["problem"] = f"Suite did not complete (exit {code}); reported cases may be partial"
        elif attempt["counts"] and (code == 0) == bool(attempt["issues"]):
            attempt["problem"] = f"Exit {code} disagrees with the reported failures or errors"
    attempt["status"] = "incomplete" if "problem" in attempt else (
        "failed" if attempt["exit_code"] else "passed"
    )
    return attempt


def summarize(entry):
    results = entry.get("results", {})
    attempts = list(results.values())
    reports = [attempt["counts"] for attempt in attempts if attempt["counts"] is not None]
    entry["counts"] = (
        {key: sum(counts[key] for counts in reports) for key in COUNTS} if reports else None
    )
    entry["seconds"] = round(sum(attempt["seconds"] for attempt in attempts), 3)
    if entry.get("problem") or any(attempt["status"] == "incomplete" for attempt in attempts):
        entry["status"] = "incomplete"
    elif not attempts:
        entry["status"] = "pending"
    elif len(attempts) < len(entry["batches"]):
        entry["status"] = "partial"
    elif any(attempt["status"] == "failed" for attempt in attempts):
        entry["status"] = "failed"
    else:
        entry["status"] = "passed"


def markdown(entries):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Línea base de suites",
        "",
        "Las cifras de una suite parcial o incompleta corresponden a los informes disponibles.",
        "",
        "| Repositorio | Commit | Estado | Pruebas | Pasan | Fallan | Errores | Omitidas | Segundos |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    states = {
        "pending": "Pendiente", "partial": "Parcial", "incomplete": "Incompleta",
        "passed": "Pasa", "failed": "Falla",
    }
    for entry in entries.values():
        counts = entry["counts"]
        columns = [
            cell(entry["name"]), entry.get("commit", "—")[:12], states[entry["status"]],
            *(counts[key] if counts else "—" for key in COUNTS),
            entry["seconds"] if entry.get("results") else "—",
        ]
        lines.append("| " + " | ".join(map(str, columns)) + " |")
    for entry in entries.values():
        lines.extend(("", f"## {entry['name']}", "", f"- Worktree · `{entry['cwd']}`"))
        if entry.get("source"):
            lines.append(f"- Repositorio de origen · `{entry['source']}`")
        if entry.get("configured_commit"):
            lines.append(f"- Commit previsto · `{entry['configured_commit']}`")
        if entry.get("note"):
            lines.append(f"- {entry['note']}")
        if entry.get("problem"):
            lines.append(f"- {entry['problem']}")
        for batch in entry["batches"]:
            result = entry.get("results", {}).get(batch["name"])
            if not result:
                lines.append(f"- {batch['name']} · pendiente · `{json.dumps(batch['command'], ensure_ascii=False)}`")
                continue
            command = json.dumps(result["command"], ensure_ascii=False)
            lines.append(f"- {batch['name']} · {result['seconds']} s · código {result['exit_code']}")
            lines.append(f"  - `{command}`")
            lines.append(f"  - Salida · `{result['log']}`")
            if result.get("problem"):
                lines.append(f"  - {result['problem']}")
            for issue in result["issues"]:
                first_line = issue["message"].splitlines()[:1]
                location = f" · `{issue['file']}`" if issue.get("file") else ""
                lines.append(f"  - `{issue['case']}`{location} · {issue['kind']} · {' '.join(first_line)}")
    return "\n".join(lines) + "\n"


def save(path, entries):
    for destination, value in (
        (path, json.dumps({"repositories": list(entries.values())}, ensure_ascii=False, indent=2) + "\n"),
        (path.with_suffix(".md"), markdown(entries)),
    ):
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(destination)


def run(path, repo_name, batch_name):
    entries = {}
    for entry in json.loads(path.read_text(encoding="utf-8-sig"))["repositories"]:
        name = entry["name"]
        batch_names = [batch["name"] for batch in entry["batches"]]
        if name in entries or len(set(batch_names)) != len(batch_names):
            raise ValueError(f"Duplicate repository or batch name in {name}")
        results = entry.get("results", {})
        entry["results"] = {
            batch["name"]: results[batch["name"]]
            for batch in entry["batches"]
            if results.get(batch["name"], {}).get("definition") == batch_definition(entry, batch)
        }
        summarize(entry)
        entries[name] = entry
    if repo_name is not None and repo_name not in entries:
        raise ValueError(f"Unknown repository: {repo_name}")
    if not entries:
        raise ValueError("No repositories configured")
    selected = [entries[repo_name]] if repo_name else list(entries.values())
    if batch_name and not any(batch["name"] == batch_name for batch in selected[0]["batches"]):
        raise ValueError(f"Unknown batch in {repo_name}: {batch_name}")

    failed = False
    for entry in selected:
        results = entry.setdefault("results", {})
        if not batch_name:
            results.clear()
        previous_commit = entry.pop("commit", None)
        entry.pop("problem", None)
        try:
            cwd = Path(entry["cwd"]).resolve(strict=True)
            if Path(git(cwd, "rev-parse", "--show-toplevel")).resolve() != cwd:
                raise ValueError(f"Expected a worktree root: {cwd}")
            commit = revision(cwd)
            expected = git(cwd, "rev-parse", "--verify", entry["ref"] + "^{commit}")
            if commit != expected:
                raise ValueError(f"Worktree at {commit}; expected {entry['ref']} at {expected}")
            if not entry["batches"]:
                raise ValueError("No suite batches defined")
            if commit != previous_commit:
                results.clear()
            entry["commit"] = commit
            for index, batch in enumerate(entry["batches"], start=1):
                if batch_name and batch["name"] != batch_name:
                    continue
                if "{report}" not in "".join(batch["command"]):
                    raise ValueError(f"Command must send JUnit output to {{report}}: {batch['name']}")
                print(f"{entry['name']} / {batch['name']} / {commit[:12]}", flush=True)
                artifacts = path.parent / "runs" / uuid4().hex / str(index)
                artifacts.parent.mkdir(parents=True)
                results[batch["name"]] = record_batch(batch, entry, cwd, artifacts)
                if revision(cwd) != commit:
                    raise ValueError("Worktree HEAD changed while tests were running")
                summarize(entry)
                save(path, entries)
        except (OSError, ValueError) as error:
            entry["problem"] = str(error)
            if "commit" not in entry:
                results.clear()
        summarize(entry)
        save(path, entries)
        if batch_name:
            failed |= bool(entry.get("problem")) or results.get(batch_name, {}).get("status") != "passed"
        else:
            failed |= entry["status"] in {"failed", "incomplete"}
        print(f"{entry['name']}: {entry['status']}", flush=True)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path, help="External JSON definition and result registry")
    parser.add_argument("--repo", help="Run one configured repository")
    parser.add_argument("--batch", help="Run one named batch (requires --repo)")
    args = parser.parse_args()
    if args.batch and not args.repo:
        parser.error("--batch requires --repo")
    path = args.registry.resolve()
    try:
        git(path.parent, "rev-parse", "--show-toplevel")
    except ValueError:
        pass
    else:
        parser.error("The registry and run artifacts must be outside Git worktrees")
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
    except OSError as error:
        parser.error(str(error))
    try:
        return run(path, args.repo, args.batch)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    finally:
        lock.unlink()


if __name__ == "__main__":
    sys.exit(main())
