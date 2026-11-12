"""A hung ssh step is retried instead of ending the move, and code already sent still owes its restart.

Two failures seen on 12-11 while moving the stack to the Spark: a trivial ssh step ("test -d ~/faustus-stack/prometheus")
hung for 120 s and failed the whole job, and that job died after sending Faustus's code but before restarting it, so the
next switch said "the code is already up to date" and left the old process running.
"""

from __future__ import annotations

import io
import subprocess
import types

import pytest

from hoard_link.hub import placement as pl


class FakeHub:
    def __init__(self, data_dir):
        self.config = types.SimpleNamespace(data_dir=str(data_dir))

    def invalidate_snapshot(self):
        pass

    def emit(self, *args, **kwargs):
        pass


def _placement(tmp_path, monkeypatch):
    placement = pl.Placement(FakeHub(tmp_path / "data"))
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)          # no real backoff in the tests
    return placement


def _answers(*results):
    """An ``_ssh`` that returns or raises each of ``results`` in turn, and counts the calls."""
    calls = []

    def ssh(command, timeout=120, input=None):
        calls.append((command, timeout))
        result = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result

    return ssh, calls


def test_a_hung_ssh_step_is_retried_and_the_move_goes_on(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    placement._ssh, calls = _answers(subprocess.TimeoutExpired(cmd="ssh", timeout=pl.SSH_STEP_TIMEOUT_S), "ok\n")
    assert placement._ssh_step("test -d ~/faustus-stack/prometheus && echo ok", what="comprobando prometheus") == "ok\n"
    assert len(calls) == 2
    assert calls[0][1] == pl.SSH_STEP_TIMEOUT_S                     # short timeout, not the 120 s default
    assert "reintento 2/3" in " ".join(placement.state()["job"]["log"])


def test_a_dropped_connection_is_retried_too(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    placement._ssh, calls = _answers(pl.PlacementError("ssh Spark3: Connection timed out during banner exchange"), "ok\n")
    assert placement._ssh_step("true") == "ok\n"
    assert len(calls) == 2


def test_a_real_failure_is_not_retried(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    placement._ssh, calls = _answers(pl.PlacementError("ssh Spark3: cat: /nope: No such file or directory"))
    with pytest.raises(pl.PlacementError, match="No such file"):
        placement._ssh_step("cat /nope")
    assert len(calls) == 1                                          # a command that says no is an answer, not a hang


def test_a_step_that_never_answers_fails_after_the_last_attempt(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    placement._ssh, calls = _answers(subprocess.TimeoutExpired(cmd="ssh", timeout=pl.SSH_STEP_TIMEOUT_S))
    with pytest.raises(pl.PlacementError, match="sin respuesta"):
        placement._ssh_step("test -d ~/faustus-stack/prometheus", what="comprobando prometheus")
    assert len(calls) == pl.SSH_STEP_ATTEMPTS


def test_a_long_step_is_not_retried(tmp_path, monkeypatch):
    """Installing dependencies or unpacking a tar may legitimately take minutes: its timeout is not a hang."""
    placement = _placement(tmp_path, monkeypatch)
    placement._ssh, calls = _answers(subprocess.TimeoutExpired(cmd="ssh", timeout=600))
    with pytest.raises(pl.PlacementError, match="1 intento"):
        placement._ssh_step("venv/bin/pip install -r requirements.txt", timeout=600)
    assert len(calls) == 1


def test_sending_code_owes_a_restart_until_one_succeeds(tmp_path, monkeypatch):
    folder = tmp_path / "faustus"
    folder.mkdir()
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one", "--allow-empty"]):
        subprocess.run(args, cwd=folder, check=True, capture_output=True)
    placement = _placement(tmp_path, monkeypatch)
    placement._stream = lambda command: types.SimpleNamespace(stdin=io.BytesIO(), returncode=0,
                                                              communicate=lambda *a, **k: (b"", b""), kill=lambda: None)
    deployed = {}
    placement._deploy_code("faustus", folder, deployed)
    assert deployed["faustus"] and deployed["faustus:restart"] is True


def _refresh(placement, monkeypatch, deployed):
    """``_refresh_sparks`` with every remote step faked; returns the apps it restarted."""
    restarted = []
    placement._update(deployed=dict(deployed), location="sparks", saved_paths={})
    monkeypatch.setattr(placement, "local_dirs", lambda: {a: placement.path.parent for a in pl.APPS})
    monkeypatch.setattr(placement, "_upload_helper", lambda: None)
    monkeypatch.setattr(placement, "_ensure_node", lambda: [])
    monkeypatch.setattr(placement, "_ensure_node_runtime", lambda: None)
    monkeypatch.setattr(placement, "_deploy_code", lambda app_id, folder, dep: None)   # the code is already there
    monkeypatch.setattr(placement, "_ensure_runtime", lambda app_id, folder, dep: None)
    monkeypatch.setattr(placement, "_remote_patch", lambda patches: None)
    monkeypatch.setattr(placement, "_wait_remote_health", lambda timeout=300: {a: True for a in pl.APPS})
    monkeypatch.setattr(placement, "_tailnet_urls", lambda: {})

    def remote(args, timeout=600):
        if args.startswith("svc restart"):
            restarted.extend(args.split()[2:])
        return []

    monkeypatch.setattr(placement, "_remote", remote)
    placement._refresh_sparks()
    return restarted


def test_code_already_sent_still_restarts_the_app_that_owes_it(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    restarted = _refresh(placement, monkeypatch, {"faustus": "stamp", "faustus:restart": True,
                                                  "hermes": "h", "prometheus": "p"})
    assert "faustus" in restarted                                    # the bug: it used to say "already up to date" and skip
    assert placement.state()["deployed"].get("faustus:restart") is None   # restarted: nothing owed any more


def test_nothing_changed_restarts_only_hermes(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    restarted = _refresh(placement, monkeypatch, {"faustus": "stamp", "hermes": "h", "prometheus": "p"})
    assert restarted == ["hermes"]                                   # Hermes rereads its settings at start


def test_a_move_to_the_spark_clears_the_pending_restarts(tmp_path, monkeypatch):
    placement = _placement(tmp_path, monkeypatch)
    placement._update(deployed={"faustus": "stamp", "faustus:restart": True, "hermes:restart": True})
    monkeypatch.setattr(placement, "local_dirs", lambda: {a: placement.path.parent for a in pl.APPS})
    for name in ("_upload_helper", "_check_node", "_ensure_node", "_ensure_node_runtime", "_stop_local",
                 "_stop_local_whatsapp", "_remote_patch", "_markers"):
        monkeypatch.setattr(placement, name, lambda *a, **k: None)
    monkeypatch.setattr(placement, "_ensure_node", lambda: [])
    monkeypatch.setattr(placement, "_deploy_code", lambda app_id, folder, dep: None)
    monkeypatch.setattr(placement, "_ensure_runtime", lambda app_id, folder, dep: None)
    monkeypatch.setattr(placement, "_read_local_paths", lambda dirs: {})
    monkeypatch.setattr(placement, "_pairs", lambda dirs: [])
    monkeypatch.setattr(placement, "_wait_remote_health", lambda timeout=300: {a: True for a in pl.APPS})
    monkeypatch.setattr(placement, "_tailnet_urls", lambda: {})
    monkeypatch.setattr(placement, "_mail", lambda *a, **k: None)
    monkeypatch.setattr(placement, "_remote", lambda args, timeout=600: [])
    placement._to_sparks()
    deployed = placement.state()["deployed"]
    assert not [k for k in deployed if k.endswith(":restart")]       # they were restarted with the new code
    assert placement.state()["location"] == "sparks"


def test_a_job_that_dies_after_sending_code_leaves_the_restart_owed(tmp_path, monkeypatch):
    """The 12-11 case: the code went over, the job failed before restarting, and the next switch said "already up to date"."""
    placement = _placement(tmp_path, monkeypatch)
    placement._update(deployed={}, location="sparks")
    monkeypatch.setattr(placement, "local_dirs", lambda: {a: placement.path.parent for a in pl.APPS})
    monkeypatch.setattr(placement, "_upload_helper", lambda: None)
    monkeypatch.setattr(placement, "_ensure_node", lambda: [])
    monkeypatch.setattr(placement, "_ensure_node_runtime", lambda: None)
    monkeypatch.setattr(placement, "_deploy_code", lambda app_id, folder, dep: dep.update({app_id: "stamp",
                                                                                          app_id + ":restart": True}))

    def boom(app_id, folder, dep):
        raise pl.PlacementError("ssh Spark3: sin respuesta en 20 s tras 3 intentos")

    monkeypatch.setattr(placement, "_ensure_runtime", boom)
    with pytest.raises(pl.PlacementError):
        placement._refresh_sparks()
    assert placement.state()["deployed"]["faustus:restart"] is True   # saved before the step that failed