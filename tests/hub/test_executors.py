"""The «executors» facet: which command-line agents this PC has, running one with the task on stdin, following its output
in pieces, stopping it, the operator rule and the agent tools. The program is the Python that runs the tests."""

from __future__ import annotations

import pytest

from hoard_link.hub import facets
from hoard_link.hub.facets import FACET_MODULES

from ._hub_fakes import http, make_hub, serve, wait_for

PY = "python"


@pytest.fixture()
def hub(tmp_path):
    hub = make_hub(tmp_path)
    server = serve(hub)
    yield hub
    server.shutdown()
    hub.close()


def _auth(hub):
    return {"Authorization": f"Bearer {hub.token}"}


def _url(hub):
    return f"http://127.0.0.1:{hub.config.port}"


def _finished(hub, rid):
    return wait_for(lambda: (lambda r: r if r["status"] != "running" else None)(
        http(_url(hub) + f"/api/executors/runs/{rid}", headers=_auth(hub))[1]), timeout=15)


def test_the_facet_is_registered_and_lists_what_is_installed(hub):
    assert "executors" in FACET_MODULES
    status, body = http(_url(hub) + "/api/executors?names=python,not-a-real-agent,..%2Fx", headers=_auth(hub))
    assert status == 200 and body["host"]
    assert body["executors"]["python"]["installed"] is True
    assert body["executors"]["not-a-real-agent"] == {"installed": False, "path": ""}
    assert body["executors"]["../x"]["installed"] is False


def test_a_run_takes_stdin_and_is_read_in_pieces(hub):
    code = "import sys; print('got:' + sys.stdin.read().strip())"
    status, started = http(_url(hub) + "/api/executors/runs", {"argv": [PY, "-c", code], "stdin": "hello", "label": "t"},
                           headers=_auth(hub))
    assert status == 200 and started["ok"] and started["cwd"]
    done = _finished(hub, started["id"])
    assert done["status"] == "done" and done["exit_code"] == 0
    assert "got:hello" in done["output"]
    again = http(_url(hub) + f"/api/executors/runs/{started['id']}?since={done['offset']}", headers=_auth(hub))[1]
    assert again["output"] == "" and again["more"] is False


def test_a_failing_run_reports_its_exit_code(hub):
    _, started = http(_url(hub) + "/api/executors/runs", {"argv": [PY, "-c", "import sys; print('bad'); sys.exit(3)"]},
                      headers=_auth(hub))
    done = _finished(hub, started["id"])
    assert done["status"] == "error" and done["exit_code"] == 3 and "bad" in done["output"]


def test_cancel_kills_the_process_tree(hub):
    _, started = http(_url(hub) + "/api/executors/runs",
                      {"argv": [PY, "-c", "import time; print('up', flush=True); time.sleep(120)"]}, headers=_auth(hub))
    rid = started["id"]
    assert wait_for(lambda: "up" in http(_url(hub) + f"/api/executors/runs/{rid}", headers=_auth(hub))[1]["output"])
    assert http(_url(hub) + f"/api/executors/runs/{rid}/cancel", {}, headers=_auth(hub))[1]["ok"]
    assert _finished(hub, rid)["status"] == "cancelled"


def test_unknown_program_and_missing_folder_are_refused_and_runs_listed(hub):
    s1, b1 = http(_url(hub) + "/api/executors/runs", {"argv": ["no-such-agent-binary"]}, headers=_auth(hub))
    assert s1 == 404 and "not installed" in b1["error"]
    s2, _ = http(_url(hub) + "/api/executors/runs", {"argv": [PY, "-c", "1"], "cwd": "/no/such/folder"}, headers=_auth(hub))
    assert s2 == 400
    s3, _ = http(_url(hub) + "/api/executors/runs", {"argv": []}, headers=_auth(hub))
    assert s3 == 400
    assert http(_url(hub) + "/api/executors/runs", headers=_auth(hub))[1]["runs"] == []


def test_only_the_operator_may_use_it(hub):
    assert http(_url(hub) + "/api/executors?names=python", headers={"Authorization": "Bearer nope"})[0] == 403
    assert http(_url(hub) + "/api/executors/runs", {"argv": [PY, "-c", "1"]}, headers={"Authorization": "Bearer nope"})[0] == 403


def test_the_tools_are_in_the_catalogue_and_work(hub):
    names = {t["name"] for t in facets.catalogue()}
    assert {"hub_executors", "hub_executor_run", "hub_executor_status", "hub_executor_cancel"} <= names
    handlers = hub.facet("executors").handlers()
    run = handlers["hub_executor_run"]({"argv": [PY, "-c", "print('tool')"]})
    done = wait_for(lambda: (lambda r: r if r["status"] != "running" else None)(
        handlers["hub_executor_status"]({"run_id": run["id"]})))
    assert "tool" in done["output"]
    assert handlers["hub_executors"]({"names": ["python"]})["executors"]["python"]["installed"]
