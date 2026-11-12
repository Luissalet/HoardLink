"""Publishing the apps on the Spark's tailnet: a port already served is skipped, and a serve that hangs or fails
leaves a warning in the job log instead of failing a move whose apps are answering."""

from __future__ import annotations

import json
import subprocess
import types

from hoard_link.hub import placement as pl

STATUS_JSON = json.dumps({"BackendState": "Running", "Self": {"DNSName": "spark3.tailnet.ts.net."}})
SERVED_443_AND_HERMES = """https://spark3.tailnet.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:7000

https://spark3.tailnet.ts.net:5221 (tailnet only)
|-- / proxy http://127.0.0.1:5221
"""


class FakeHub:
    def __init__(self, data_dir):
        self.config = types.SimpleNamespace(data_dir=str(data_dir))

    def invalidate_snapshot(self):
        pass

    def emit(self, *args, **kwargs):
        pass


def _placement(tmp_path, answers):
    """A Placement whose SSH answers come from ``answers`` (command -> text, or an exception to raise)."""
    placement = pl.Placement(FakeHub(tmp_path / "data"))
    calls = []

    def ssh(command, timeout=120, input=None):
        calls.append((command, timeout))
        for needle, answer in answers.items():
            if needle in command:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""

    placement._ssh = ssh
    return placement, calls


def _serve_calls(calls):
    return [(command, timeout) for command, timeout in calls if "tailscale serve --bg" in command]


def test_serve_status_ports_are_read_from_its_own_output():
    assert pl.Placement._parse_served(SERVED_443_AND_HERMES) == {443, 5221}
    assert pl.Placement._parse_served("") == set()
    assert pl.Placement._parse_served("No serve config\n") == set()


def test_a_port_already_served_is_not_published_again(tmp_path):
    placement, calls = _placement(tmp_path, {"tailscale status --json": STATUS_JSON,
                                             "tailscale serve status": SERVED_443_AND_HERMES})
    urls = placement._tailnet_urls()
    assert urls == {"faustus": "https://spark3.tailnet.ts.net",
                    "hermes": "https://spark3.tailnet.ts.net:5221/products/",
                    "prometheus": "https://spark3.tailnet.ts.net:5205/"}
    served = _serve_calls(calls)
    assert [command for command, _ in served] == [
        f"sudo -n tailscale serve --bg --https={pl.PORTS['prometheus']} http://127.0.0.1:{pl.PORTS['prometheus']} >/dev/null 2>&1 || true"]
    assert "ya estaba publicado" in " ".join(placement.state()["job"]["log"])


def test_a_serve_that_hangs_only_leaves_a_warning(tmp_path):
    placement, calls = _placement(tmp_path, {
        "tailscale status --json": STATUS_JSON,
        "tailscale serve status": "",
        "tailscale serve --bg": subprocess.TimeoutExpired(cmd="ssh", timeout=pl.SERVE_TIMEOUT_S)})
    urls = placement._tailnet_urls()                       # no exception: the links are still handed out
    assert set(urls) == {"faustus", "hermes", "prometheus"}
    assert len(_serve_calls(calls)) == 3 * pl.SSH_STEP_ATTEMPTS       # each port is retried before giving up
    assert all(timeout <= 20 for _, timeout in _serve_calls(calls))   # a short timeout, never the old 60 s
    log = " ".join(placement.state()["job"]["log"])
    assert log.count("aviso: no he podido publicar el puerto") == 3   # one warning per port, not one per attempt


def test_an_unreadable_serve_status_still_tries_to_publish(tmp_path):
    placement, calls = _placement(tmp_path, {"tailscale status --json": STATUS_JSON,
                                             "tailscale serve status": pl.PlacementError("ssh Spark3: timeout")})
    assert placement._served_ports() == set()
    placement._tailnet_urls()
    assert len(_serve_calls(calls)) == 3                    # publishing answers: one call per port


def test_a_move_whose_apps_answer_ends_well_even_if_serve_fails(tmp_path):
    placement, _ = _placement(tmp_path, {
        "tailscale status --json": STATUS_JSON,
        "tailscale serve status": "",
        "tailscale serve --bg": subprocess.TimeoutExpired(cmd="ssh", timeout=pl.SERVE_TIMEOUT_S)})

    def to_sparks():                                        # the tail of the real move: apps up, then the links
        urls = placement._tailnet_urls()
        placement._update(location="sparks", since=1.0, urls=urls)

    placement._to_sparks = to_sparks
    placement._run_switch("sparks")
    job = placement.state()["job"]
    assert job["running"] is False and job["ok"] is True and job["step"] == "hecho"
    assert placement.state()["urls"]["faustus"] == "https://spark3.tailnet.ts.net"
