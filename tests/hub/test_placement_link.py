"""The tunnel that lets a Faustus on a Spark reach this Hub: when it opens, what the Spark is told, how it ends."""

from __future__ import annotations

import json
import types

from hoard_link.hub import placement_link as pl
from hoard_link.hub import placement_remote as pr


class FakeProc:
    def __init__(self, code=None, err=b""):
        self.code, self.err, self.closed, self.killed, self.written = code, err, False, False, b""
        self.stdin = types.SimpleNamespace(close=self._close_stdin, write=self._write, flush=lambda: None)
        self.stderr = types.SimpleNamespace(read=lambda: self.err)

    def _write(self, data):
        self.written += data

    def _close_stdin(self):
        self.closed = True
        self.code = 0                      # the remote command read end-of-input and finished

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.killed = True
        self.code = -15

    kill = terminate


class FakePlacement:
    def __init__(self, location="sparks"):
        self.data = {"location": location, "node": {"ssh": "Spark3", "name": "Spark3"}}
        self.hub = types.SimpleNamespace(token="tok-1", config=types.SimpleNamespace(port=8810))
        self.sent: list[tuple[str, str]] = []
        self.fail = ""
        self.probe = ""

    def state(self):
        return dict(self.data)

    def _ssh(self, command, timeout=120, input=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append((command, input))
        if "ss -ltnpH" in command:
            return self.probe
        return ""


def _link(placement, procs):
    opened = []

    def popen(argv, **kwargs):
        opened.append((argv, kwargs))
        return procs.pop(0)

    link = pl.HubLink(placement, popen=popen, clock=lambda: 1000.0 + len(opened) * 0, settle_s=0.0)
    return link, opened


def test_no_tunnel_while_faustus_is_on_the_pc():
    link, opened = _link(FakePlacement("pc"), [])
    assert link.tick() == {"wanted": False, "up": False, "since": None, "error": "", "port": pl.LINK_PORT, "node": "",
                           "freed": ""}
    assert opened == []


def test_on_the_spark_it_tells_where_the_hub_is_and_opens_the_tunnel():
    placement = FakePlacement()
    proc = FakeProc()
    link, opened = _link(placement, [proc])
    state = link.tick()
    assert state["wanted"] and state["up"] and state["node"] == "Spark3" and state["error"] == ""
    token_cmd, token = placement.sent[0]
    assert "umask 077" in token_cmd and "hub-token" in token_cmd and token == "tok-1"
    link_cmd, body = placement.sent[1]
    told = json.loads(body)
    assert "hub-link.json" in link_cmd
    assert told["url"] == f"http://127.0.0.1:{pl.LINK_PORT}" and told["token_file"].endswith("/faustus-stack/hub-token")
    argv, kwargs = opened[0]
    assert argv[0] == "ssh" and f"127.0.0.1:{pl.LINK_PORT}:127.0.0.1:8810" in argv and argv[-2:] == ["Spark3", pl.REMOTE_CMD]
    assert "ExitOnForwardFailure=yes" in argv and kwargs["stdin"] is not None       # our pipe: the child ends with us
    assert f"-t {pl.REMOTE_IDLE_S}" in pl.REMOTE_CMD                                # and lets go of the port if we die
    link.tick()                                                                    # still up: nothing is sent again
    assert len(placement.sent) == 2 and len(opened) == 1
    assert proc.written == b"\n"                                                    # the remote command is kept waiting


def test_a_tunnel_that_ends_is_reported_and_opened_again_later():
    placement = FakePlacement()
    first, second = FakeProc(), FakeProc()
    now = [1000.0]
    opened = []
    link = pl.HubLink(placement, popen=lambda argv, **kw: (opened.append(argv), [first, second][len(opened) - 1])[1],
                      clock=lambda: now[0], settle_s=0.0)
    assert link.tick()["up"]
    first.code, first.err = 255, b"Warning: something\nremote port forwarding failed for listen port 8811\n"
    state = link.tick()
    assert not state["up"] and "remote port forwarding failed" in state["error"]
    assert not link.tick()["up"] and len(opened) == 1                               # not before the wait is over
    now[0] += pl.RETRY_S + 1
    assert link.tick()["up"] and len(opened) == 2
    assert len(placement.sent) == 4                              # told again: its folder may have been wiped meanwhile


def test_an_unreachable_spark_is_a_state_not_a_crash():
    placement = FakePlacement()
    placement.fail = "ssh Spark3: connect timed out"
    link, opened = _link(placement, [])
    state = link.tick()
    assert state["wanted"] and not state["up"] and "timed out" in state["error"] and opened == []


def test_status_never_waits_for_the_spark_and_two_looks_open_one_tunnel():
    import threading

    placement = FakePlacement()
    entered, release = threading.Event(), threading.Event()
    plain = placement._ssh

    def slow(command, timeout=120, input=None):
        entered.set()
        assert release.wait(10)
        return plain(command, timeout=timeout, input=input)

    placement._ssh = slow
    link, opened = _link(placement, [FakeProc(), FakeProc()])
    first = threading.Thread(target=link.tick)
    first.start()
    assert entered.wait(5)                                       # the first look is talking to the Spark right now
    answered = threading.Event()
    threading.Thread(target=lambda: (link.status(), answered.set())).start()
    assert answered.wait(2)                                      # and the state is still read at once
    assert link.tick()["up"] is False and opened == []           # a second look does not open another tunnel
    release.set()
    first.join(10)
    assert link.status()["up"] and len(opened) == 1


def test_a_stopped_link_does_not_open_again():
    placement = FakePlacement()
    proc = FakeProc()
    link, opened = _link(placement, [proc, FakeProc()])
    assert link.tick()["up"]
    link.stop()
    assert proc.closed
    link.poke()
    assert link.tick()["up"] is False and len(opened) == 1


def test_coming_back_to_the_pc_closes_it():
    placement = FakePlacement()
    proc = FakeProc()
    link, _ = _link(placement, [proc])
    assert link.tick()["up"]
    placement.data["location"] = "pc"
    state = link.tick()
    assert not state["wanted"] and not state["up"] and proc.closed and not proc.killed


def test_a_new_token_is_told_again():
    placement = FakePlacement()
    link, _ = _link(placement, [FakeProc(), FakeProc()])
    link.tick()
    placement.hub.token = "tok-2"
    link._close()
    link.tick()
    assert [body for _cmd, body in placement.sent if body in ("tok-1", "tok-2")] == ["tok-1", "tok-2"]


def test_the_unit_of_faustus_points_at_the_link_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pr.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    stack = tmp_path / "stack"
    assert "faustus" in pr.units(str(stack))
    unit = (tmp_path / ".config" / "systemd" / "user" / "faustus.service").read_text(encoding="utf-8")
    assert f"Environment=FAUSTUS_HUB_LINK={stack}/hub-link.json" in unit
    assert "HOARD_HUB_URL" not in unit                    # only the Agora and coordination use it, not every Hub service


def test_the_placement_status_carries_the_link(tmp_path):
    from hoard_link.hub.config import HubConfig
    from hoard_link.hub.core import Hub
    hub = Hub(HubConfig(port=0, data_dir=str(tmp_path / "data"), roots=[], icon_dirs=[], jobs_enabled=False,
                        faustus_urls=["http://127.0.0.1:1"]))
    try:
        assert "hub_link" not in hub.placement.status()                            # on the PC there is nothing to link
        hub.placement._refresh_remote_health = lambda: None
        hub.placement._update(location="sparks", urls={"faustus": "https://spark3.example.ts.net"})
        status = hub.placement.status()
        assert status["hub_link"] == {"wanted": False, "up": False, "since": None, "error": "", "port": pl.LINK_PORT,
                                      "node": "", "freed": ""}
        assert hub.placement.link._thread is None                                  # a Hub built in a test never opens ssh
    finally:
        hub.close()


def test_a_port_held_by_the_tunnel_of_a_dead_hub_is_freed_and_the_tunnel_opens_at_once():
    """The 12-11 blue screen: the Spark kept listening on 8811 from the sshd of the Hub that was gone, so every new
    tunnel failed with ExitOnForwardFailure for ever. The one session that owns that listener is ended and the tunnel
    opens in the same look, without waiting RETRY_S."""
    placement = FakePlacement()
    busy = FakeProc(255, b"Warning: remote port forwarding failed for listen port 8811\n")
    placement.probe = (f"LISTEN 0 128 127.0.0.1:{pl.LINK_PORT} 0.0.0.0:* users:((\"sshd\",pid=4242,fd=9))\n--\n"
                       "4242 900 3600 sshd: luissalet@notty\n"
                       "900 1 3600 sshd: luissalet [priv]\n")
    link, opened = _link(placement, [busy, FakeProc()])
    state = link.tick()
    assert state["up"] and state["error"] == "" and state["freed"]
    assert f"kill 4242" in [cmd for cmd, _ in placement.sent][-1]
    assert len(opened) == 2                                                       # freed, then opened again at once
    assert "4242" in state["freed"] and str(pl.LINK_PORT) in state["freed"]


def test_a_port_held_by_anything_else_is_reported_and_left_alone():
    placement = FakePlacement()
    busy = FakeProc(255, b"Warning: remote port forwarding failed for listen port 8811\n")
    placement.probe = (f"LISTEN 0 128 127.0.0.1:{pl.LINK_PORT} 0.0.0.0:* users:((\"python3\",pid=77,fd=3))\n--\n"
                       "4242 900 3600 sshd: luissalet@notty\n")
    link, opened = _link(placement, [busy])
    state = link.tick()
    assert not state["up"] and "remote port forwarding failed" in state["error"]
    assert "no lo toco" in state["freed"] and len(opened) == 1
    assert not any(cmd.startswith("kill") for cmd, _ in placement.sent)


def test_a_session_with_a_command_behind_it_is_never_ended():
    placement = FakePlacement()
    busy = FakeProc(255, b"Warning: remote port forwarding failed for listen port 8811\n")
    placement.probe = (f"LISTEN 0 128 127.0.0.1:{pl.LINK_PORT} 0.0.0.0:* users:((\"sshd\",pid=4242,fd=9))\n--\n"
                       "4242 900 30 sshd: luissalet@pts/0\n")
    link, opened = _link(placement, [busy])
    state = link.tick()
    assert not state["up"] and "no la toco" in state["freed"] and len(opened) == 1
    assert not any(cmd.startswith("kill") for cmd, _ in placement.sent)


def test_a_plain_ssh_step_hands_ssh_no_pipe_to_wait_on(tmp_path):
    """The orphan "sshd: …@notty" sessions of #3102: a step with nothing to send must give ssh /dev/null, not a pipe
    that stays open until the timeout."""
    import subprocess
    from hoard_link.hub import placement as plp

    class FakeHub:
        def __init__(self, data_dir):
            self.config = types.SimpleNamespace(data_dir=str(data_dir))

    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    placement = plp.Placement(FakeHub(tmp_path / "data"), runner=run)
    assert placement._ssh("true") == "ok\n"
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert placement._ssh("cat > x", input="body") == "ok\n"
    assert calls[1]["stdin"] is None and calls[1]["input"] == b"body"
    assert "ServerAliveCountMax=3" in plp.SSH_OPTS and "TCPKeepAlive=yes" in plp.SSH_OPTS
