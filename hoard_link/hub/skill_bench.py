"""Test a skill in a fresh Hub worker and compare observed tool replies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from tempfile import gettempdir
import time
from uuid import uuid4

from .. import skills


TEMPLATE = "skill-bench"
ACTIVE = {"starting", "running", "stopping"}
HUB_CMD = r"E:\Development\IA\Hoards\Faustusables\Hoard Hub\hub.cmd"
_MISSING = object()


def clean_worker(command, prompt, env, run_id):
    """Keep the provider login while starting the worker outside any checkout."""
    from .order_receipts import ENV_CONTEXT

    directory = Path(gettempdir()) / ("hoard-skill-bench-" + run_id)
    directory.mkdir()
    instructions = directory / "benchmark.md"
    instructions.write_text(prompt, encoding="utf-8")
    flags = [
        "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-session-persistence",
        "--system-prompt-file", str(instructions),
    ]
    return [*command, *flags, "--", "Perform the described production test now."], directory, {
        **env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", ENV_CONTEXT: json.dumps({"run_id": run_id}),
    }


def slug(value):
    if not isinstance(value, str) or not skills._SLUG.fullmatch(value):
        raise ValueError("Supply a valid skill slug")
    return value


def _lines(fields):
    # JSON string literals are also valid in the library's inline front matter.
    return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in fields) + "]"


def draft_text(name, station, accepts, produces, check, receipts):
    steps, failures = [], []
    for r in receipts:
        label = (r["app"] + " / " if r["app"] else "") + r["tool"]
        keys = ", ".join(sorted(r["arguments"]))
        link = f"receipt #{r['id']}"
        if r["ok"]:
            steps.append(f"{len(steps) + 1}. Call `{label}` using this job's inputs"
                         + (f" for `{keys}`" if keys else "") + f" ({link}).")
        else:
            failures.append(f"- Check what went wrong before repeating `{label}` ({link}).")
    return "\n".join([
        "---", f"name: {name}", "description: Draft procedure from recorded production calls.",
        f"apps: {_lines([station])}", f"accepts: {_lines(accepts)}", f"produces: {_lines(produces)}",
        "status: draft", "---", "", "## When to Use", "", f"For a job at the {station} station.",
        "", "## Procedure", "", *steps, "", "## Pitfalls", "",
        *(failures or ["- Use identifiers and paths from the current job."]), "", "## Verification", "",
        str(check or "Check the produced piece against the job's requirements."), "",
    ])


def skill_matches(library, app, accepts, produces, declared):
    """Only attach a published skill when its types cover this declared step."""
    return [r["slug"] for r in library if (app in r["apps"] or declared == r["slug"])
            and accepts in r["accepts"] and produces in r["produces"]]


def link_catalogue(hub, result):
    """Annotate declared station handoffs and their individual chain edges."""
    config = getattr(hub, "config", None)
    location = skills.find_library(getattr(config, "faustus_dir", None)) if config is not None else None
    library = [row for row in skills.library_rows(location) if row["accepts"] and row["produces"]] if location else []
    for station in result["stations"]:
        for handoff in station["handoffs"]:
            handoff["skills"] = sorted({slug for piece in handoff["produces"]
                                        for slug in skill_matches(library, station["app"], handoff["accepts"],
                                                                  piece, handoff["skill"])})
    for chain in result.get("chains", []):
        for step in chain:
            step["skills"] = skill_matches(library, step["app"], step["accepts"], step["produces"], step["skill"])
    return result


def _at(value, pointer):
    if pointer == "":
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Comparison fields must be JSON pointers")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif (isinstance(value, list) and part.isascii() and part.isdecimal()
              and len(part) <= 10 and int(part) < len(value)):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare(expected, actual, fields):
    differences = []
    for pointer in fields:
        want, got = _at(expected, pointer), _at(actual, pointer)
        if got is _MISSING or _encoded(want) != _encoded(got):
            differences.append({"path": pointer, "expected": want,
                                **({"missing": True} if got is _MISSING else {"actual": got})})
    return differences


def _prompt(run_id, step, inputs, text):
    return (
        "You are testing one production skill in a fresh session. Use the skill below to perform the given step. "
        "The Hub compares the recorded tool reply. Use only the supplied test inputs. "
        "For each Hub or app tool call use the Hub launcher at this absolute path:\n"
        f"{HUB_CMD}\n"
        "Run it as: tool hub_order_call --args-file FILE --json. In FILE put a UTF-8 JSON object with "
        f"`run_id` set to `{run_id}`, `tool` set to the tool name, `arguments` holding the tool arguments, "
        "and `app` when calling an app tool. Without `app`, the call is a Hub tool. "
        "Use your shell to write the argument file in the current directory and invoke the launcher. "
        "Stop when the step finishes and report what you observed.\n\n"
        f"Step:\n{step}\n\nInputs:\n{_encoded(inputs)}\n\nSkill:\n{text}"
    )


def start(facet, args):
    from .order_receipts import _json

    baseline = facet.read(args)["receipt"]
    if not baseline["ok"] or baseline["run_id"]:
        raise ValueError("Select a successful production receipt as the comparison")
    step = str(args.get("step") or "").strip()
    if not step:
        raise ValueError("Describe the step for the worker")
    fields = args.get("compare", [""])
    if not isinstance(fields, list) or not fields or any(
        not isinstance(field, str) or _at(baseline["reply"], field) is _MISSING for field in fields
    ):
        raise ValueError("Every comparison pointer must name a field in the reference reply")
    inputs = args.get("inputs", baseline["arguments"])
    if not isinstance(inputs, dict):
        raise ValueError("Inputs must be an object")
    draft_id = args.get("draft_id")
    if bool(draft_id) == bool(args.get("slug")):
        raise ValueError("Select one draft or published skill")
    if draft_id:
        draft = facet.read_draft(args)["draft"]
        if draft["task_id"] != baseline["task_id"]:
            raise ValueError("Select a receipt from the draft's job")
        name, text = draft["slug"], draft["text"]
    else:
        name = slug(args["slug"])
        library = skills.find_library(facet.hub.config.faustus_dir)
        row = next((r for r in skills.library_rows(library) if r["slug"] == name), None) if library else None
        if row is None:
            raise ValueError("Unknown published skill")
        text = row["text"]
    console = facet.hub.facet("agora_console").for_space(baseline["space_id"])
    run_id = str(uuid4())
    prompt = _prompt(run_id, step, inputs, text)
    undo_request = {"tool": "hub_skill_bench_undo", "arguments": {"run_id": run_id}}
    with facet.db() as db:
        db.execute("INSERT INTO benchmarks VALUES(?,?,?,?,?,?,?,?,?,'running',NULL,?)",
                   (run_id, baseline["order_id"], baseline["space_id"], baseline["task_id"], name, draft_id,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(), _json(baseline), _json(fields), time.time()))
    try:
        launch = console._create_worker({"leader_id": "claude", "model": "haiku", "effort": "low",
                                         "rank": "support", "task": step},
                                        actor="skill-bench", worker_id=run_id, benchmark_prompt=prompt)
    except Exception as exc:
        launch = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not launch.get("ok"):
        with facet.db() as db:
            db.execute("UPDATE benchmarks SET state='failed',result=? WHERE id=?",
                       (_json({"detail": launch.get("error", "Worker could not start")}), run_id))
    return {**history(facet, {"run_id": run_id}), "undo": undo_request}


def _worker(facet, row):
    console = facet.hub.facet("agora_console").for_space(row["space_id"])
    with console._db_lock, console._db() as db:
        worker = db.execute("SELECT agent_id,model,effort,state,detail,result,process_exit_code,provider_error "
                            "FROM workers WHERE id=?", (row["id"],)).fetchone()
    return console, dict(worker) if worker else None


def status(facet, row):
    from .order_receipts import _json, _receipt

    info = {key: row[key] for key in ("id", "order_id", "space_id", "task_id", "slug", "draft_id", "skill_sha256",
                                     "state", "created")}
    baseline = json.loads(row["baseline"])
    info.update(reference_receipt=baseline["id"], compare=json.loads(row["fields"]))
    if row["state"] != "running":
        return {**info, "result": json.loads(row["result"]) if row["result"] else None}
    _, worker = _worker(facet, row)
    if worker and worker["state"] in (ACTIVE | {"unknown"}):
        return {**info, "worker": worker}
    if not worker and time.time() - row["created"] < 120:
        return info
    with facet.db() as db:
        receipts = db.execute("SELECT * FROM receipts WHERE run_id=? AND app=? AND tool=? ORDER BY id DESC",
                              (info["id"], baseline["app"], baseline["tool"])).fetchall()
        observed = _receipt(receipts[0]) if receipts else None
        result = {"worker": worker, "actual_receipt": observed["id"] if observed else None, "differences": []}
        if not worker or worker["state"] != "completed":
            state = "failed"
            result["reason"] = "worker_failed"
        elif not observed or not observed["ok"]:
            state = "failed"
            result["reason"] = "missing_call" if observed is None else "call_failed"
        else:
            result["differences"] = compare(baseline["reply"], observed["reply"], info["compare"])
            state = "different" if result["differences"] else "passed"
        db.execute("UPDATE benchmarks SET state=?,result=? WHERE id=? AND state='running'",
                   (state, _json(result), info["id"]))
    return {**info, "state": state, "result": result}


def history(facet, args):
    run_id, name = args.get("run_id"), args.get("slug")
    if bool(run_id) == bool(name):
        raise ValueError("Supply one run id or a skill slug")
    with facet.db() as db:
        if run_id:
            row = db.execute("SELECT * FROM benchmarks WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown skill test")
            rows = [row]
        else:
            rows = db.execute("SELECT * FROM benchmarks WHERE slug=? ORDER BY created DESC", (slug(name),)).fetchall()
    return {"ok": True, "benchmarks": [status(facet, row) for row in rows]}


def finish(facet, run_id):
    """Persist the comparison when the console confirms the worker exited."""
    with facet.db() as db:
        row = db.execute("SELECT * FROM benchmarks WHERE id=? AND state='running'", (run_id,)).fetchone()
    if row is not None:
        status(facet, row)


def undo(facet, args):
    run_id = args.get("run_id")
    with facet.db() as db:
        row = db.execute("SELECT * FROM benchmarks WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "Unknown or already undone skill test"}
    console, worker = _worker(facet, row)
    if worker is None and row["state"] == "running":
        return {"ok": False, "status": 409, "error": "The worker is still starting"}
    if worker and worker["state"] in (ACTIVE | {"unknown"}) and worker["state"] != "stopping":
        stop = console._stop_worker(run_id)
        if not stop["ok"]:
            return stop
    with facet.db() as db:
        db.execute("DELETE FROM benchmarks WHERE id=?", (run_id,))
    return {"ok": True, "run_id": run_id}
