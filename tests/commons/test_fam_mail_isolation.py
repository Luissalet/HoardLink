"""The Faustus mail helper only runs in the real install: a test hub or test data has no mail without a gateway.

Every scenario counts the calls of a stand-in for ``subprocess.run``; for test hubs and test data the count must stay at zero."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hoard_link import fam_mail, family

REPO = Path(fam_mail.__file__).resolve().parent.parent
REAL_HUB = "http://127.0.0.1:8810"
OK_ANSWER = '{"ok": true, "error": "", "messages": [{"subject": "Factura", "message_id": "<a@x>", "ts": 1}], "accounts": []}\n'


class CountingRunner:
    """A ``subprocess.run`` stand-in that records the helper invocations."""

    def __init__(self):
        self.calls = 0
        self.requests = []

    def __call__(self, cmd, **kw):
        self.calls += 1
        self.requests.append(kw)
        return subprocess.CompletedProcess(cmd, 0, OK_ANSWER, "")


class NoGateway:
    """The hub side of a router: no mail gateway answers."""

    def available(self, *a, **k):
        return False


class Gateway:
    """The hub side of a router: a gateway that serves one message."""

    def available(self, *a, **k):
        return True

    def register_interest(self, spec, sphere=None):
        return {"ok": True}

    def messages(self, since_id=0, limit=100, full=True, fields=None):
        row = {"id": 1, "message_id": "<g@x>", "subject": "Factura", "text": "", "ts": time.time(), "from_address": "a@b.es"}
        return {"ok": True, "messages": [row] if since_id < 1 else [], "last_id": 1}


@pytest.fixture
def faustus(tmp_path):
    root = tmp_path / "faustus"
    (root / "mcp_servers").mkdir(parents=True)
    (root / "mcp_servers" / "email_server.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def configure(monkeypatch):
    """Point the family client at a hub and a token file, and put the state back afterwards."""
    saved = dict(family._state)
    for key in ("HOARD_HUB_URL", "HOARD_TOKEN_FILE", "HOARD_HUB_DATA_DIR",
                "HOARD_MAIL_REAL_HUB_URLS", "HOARD_MAIL_REAL_DATA_DIRS"):
        monkeypatch.delenv(key, raising=False)
    fam_mail.forget_availability()

    def apply(hub, token_file):
        family.configure("probe", None, token_file=token_file, hub=hub)

    yield apply
    family._state.clear()
    family._state.update(saved)
    fam_mail.forget_availability()


def helper_for(faustus, runner):
    return fam_mail.FaustusHelper(str(faustus), python=sys.executable, runner=runner, ask_hub=False)


def test_helper_uses_configured_identity_without_exporting_a_validator(configure, faustus, tmp_path):
    hub = "https://hub.example.test:9831"
    data = tmp_path / "mail-app"
    data.mkdir()
    token_file = data / "mcp-token"
    token_file.write_text("synthetic-token\n", encoding="utf-8")
    configure(hub, str(token_file))
    runner = CountingRunner()
    helper = fam_mail.FaustusHelper(str(faustus), python=sys.executable, runner=runner, ask_hub=False,
                                   real_hubs=[hub], real_data=[data])
    assert helper.run("status", {"caller": {"app": "hub", "token": "ignore"}})["ok"]
    assert json.loads(runner.requests[0]["input"])["caller"] == {"app": "probe", "token": "synthetic-token"}
    assert "HOARD_MAIL_REAL_HUB_URLS" not in runner.requests[0]["env"]


def router_for(faustus, runner, mode="auto", hub=None):
    return fam_mail.MailRouter(helper_for(faustus, runner), source_getter=lambda: mode, hub=hub or NoGateway())


# ---------------------------------------------------------------------------------------------------------------------
# isolation()
# ---------------------------------------------------------------------------------------------------------------------

def test_a_hub_that_is_not_the_real_one_isolates(configure, tmp_path):
    for hub in ("http://127.0.0.1:58778", "http://127.0.0.1:80", "http://hub.example.test:8810", "http://10.0.0.2:8810"):
        configure(hub, str(REPO / "data" / "mcp-token"))
        assert fam_mail.isolation() == "hub", hub


def test_test_data_next_to_the_real_hub_isolates(configure, tmp_path):
    configure(REAL_HUB, str(tmp_path / "data" / "apps" / "probe" / "mcp-token"))
    assert fam_mail.isolation() == "data"
    configure(REAL_HUB, str(REPO / "data" / "sub" / "mcp-token"))              # inside the repo's data folder is still not the folder
    assert fam_mail.isolation() == "data"
    configure(REAL_HUB, "")                                                       # an app that never said where its data lives
    assert fam_mail.isolation() == "data"


def test_the_real_install_is_not_isolated(configure):
    configure(REAL_HUB, str(REPO / "data" / "mcp-token"))
    assert fam_mail.isolation() == ""
    configure("http://localhost:8810/", str(REPO / "data" / "mcp-token"))
    assert fam_mail.isolation() == ""


def test_the_state_line_is_spanish_and_english():
    for code in ("hub", "data"):
        text = fam_mail.isolation_text(code)
        assert text.startswith("sin pasarela") and "correo desactivado" in text and "mail is off" in text
    assert fam_mail.isolation_text("") == ""


# ---------------------------------------------------------------------------------------------------------------------
# the helper and the router make no call
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hub, own_data", [("http://127.0.0.1:58778", True), (REAL_HUB, False)], ids=["test-hub", "test-data"])
def test_helper_makes_no_call_in_a_test_install(configure, faustus, tmp_path, hub, own_data):
    configure(hub, str((REPO if own_data else tmp_path) / "data" / "mcp-token"))
    runner = CountingRunner()
    helper = helper_for(faustus, runner)
    assert helper.faustus_dir() is not None and not helper.available()
    for action in ("status", "scan", "read", "send"):
        answer = helper.run(action, {"to": ["x@y.es"]})
        assert answer["ok"] is False and answer["isolated"] in ("hub", "data") and "sin pasarela" in answer["error"]
    assert helper.status()["ok"] is False
    assert runner.calls == 0


@pytest.mark.parametrize("mode", ["auto", "faustus"])
def test_router_without_a_gateway_reads_nothing_in_a_test_install(configure, faustus, tmp_path, mode):
    configure("http://127.0.0.1:58778", str(tmp_path / "data" / "mcp-token"))
    runner = CountingRunner()
    router = router_for(faustus, runner, mode)
    assert router.scan(since_days=30, limit=10, subject_terms=["factura"]) == []
    assert router.last_source == "" and "sin pasarela" in router.last_error and "mail is off" in router.last_error
    assert router.status()["helper_available"] is False
    assert runner.calls == 0


def test_router_still_reads_the_gateway_in_a_test_install(configure, faustus, tmp_path):
    configure("http://127.0.0.1:58778", str(tmp_path / "data" / "mcp-token"))
    runner = CountingRunner()
    router = router_for(faustus, runner, hub=Gateway())
    rows = router.scan(since_days=30, limit=10)
    assert [m["subject"] for m in rows] == ["Factura"] and router.last_source == "hub"
    assert runner.calls == 0


def test_the_real_install_still_uses_the_helper(configure, faustus):
    (faustus / "venv" / "bin").mkdir(parents=True)
    (faustus / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    configure(REAL_HUB, str(REPO / "data" / "mcp-token"))
    runner = CountingRunner()
    assert not fam_mail.isolation() and helper_for(faustus, runner).available()
    rows = router_for(faustus, runner).scan(since_days=30, limit=10)
    assert [m["subject"] for m in rows] == ["Factura"] and runner.calls == 1


@pytest.mark.parametrize("case, code", [
    ("declared", ""), ("other-port", "hub"), ("other-scheme", "hub"),
    ("sibling-data", "data"), ("temp-data", "data"), ("relative-root", "data"),
])
def test_a_declared_install_requires_both_matches(case, code, configure, faustus, tmp_path, monkeypatch):
    install_hub = "https://mail.example.test"
    root = tmp_path / "adopted data"
    other_root = tmp_path / "second root"
    hub = {"other-port": install_hub + ":8810", "other-scheme": "http://mail.example.test"}.get(
        case, install_hub + ":443/"
    )
    folder = {"sibling-data": tmp_path / "adopted data-copy", "temp-data": tmp_path / "tests"}.get(
        case, root / "native" / "shipment" / "data"
    )
    monkeypatch.setenv("HOARD_MAIL_REAL_HUB_URLS", f"invalid, {install_hub}, http://second.example.test:8810")
    roots = [root.name if case == "relative-root" else str(root), str(other_root)]
    monkeypatch.setenv("HOARD_MAIL_REAL_DATA_DIRS", os.pathsep.join(roots))
    monkeypatch.chdir(tmp_path)
    configure(hub, str(folder / "mcp-token"))
    runner = CountingRunner()
    helper = helper_for(faustus, runner)

    assert helper.isolation() == code
    assert helper.available() is (not code)
    router = fam_mail.MailRouter(helper, hub=NoGateway())
    rows = router.scan(since_days=30, limit=10)
    assert len(rows) == runner.calls == (0 if code else 1)


def test_the_default_data_folder_belongs_to_the_install(configure, monkeypatch, tmp_path):
    install = tmp_path / "fleet" / "app"
    (install / "faustus-plugin.json").parent.mkdir(parents=True)
    (install / "faustus-plugin.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fam_mail, "__file__", str(install / "app_pkg" / "hoard_link" / "fam_mail.py"))

    for folder in (install.parent / "data", install / "app_pkg" / "data"):
        configure(REAL_HUB, str(folder / "mcp-token"))
        assert fam_mail.isolation() == "data"
    configure(REAL_HUB, str(install / "data" / "mcp-token"))
    assert fam_mail.isolation() == ""


def test_status_cache_follows_the_installation(configure, faustus, tmp_path):
    runner = CountingRunner()
    helper = helper_for(faustus, runner)
    data = str(REPO / "data" / "mcp-token")
    configure(REAL_HUB, data)
    assert helper.status()["ok"] and runner.calls == 1
    configure("http://127.0.0.1:58778", data)
    assert helper.status()["isolated"] == "hub" and runner.calls == 1
    configure(REAL_HUB, str(tmp_path / "mcp-token"))
    assert helper.status()["isolated"] == "data" and runner.calls == 1
    configure(REAL_HUB, data)
    assert helper.status()["ok"] and runner.calls == 2
