"""Hermes identification, sent-copy lookup and non-fatal Gmail labelling."""

from types import SimpleNamespace

import pytest

from hoard_link import mail_helper as mh, notify_channels as nc


class IMAP:
    def __init__(self, *, exists=False, delay=False, fail="", localized=True):
        self.exists, self.delay, self.fail, self.localized = exists, delay, fail, localized
        self.calls, self.searches, self.closed = [], 0, False

    def list(self, *args):
        self.calls.append(("LIST", *args))
        if args:
            return "OK", [b'(\\HasNoChildren) "/" "Hermes"'] if self.exists else [None]
        return "OK", [b'(\\All \\HasNoChildren) "/" "[Gmail]/Todos los mensajes"'] if self.localized else []

    def select(self, folder, readonly=False):
        self.calls.append(("SELECT", folder, readonly))
        if self.fail == "select":
            return "NO", [b"not found"]
        return "OK", [b"1"]

    def create(self, folder):
        self.calls.append(("CREATE", folder))
        return "OK", []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if self.fail == "raise":
            raise OSError("IMAP unavailable")
        if command == "SEARCH":
            self.searches += 1
            return "OK", [b"" if self.fail == "missing" or (self.delay and self.searches == 1) else b"42 43"]
        return ("NO" if self.fail == "store" else "OK"), []

    def logout(self):
        self.closed = True


class SMTP:
    def __init__(self):
        self.sent = []

    def login(self, *args):
        pass

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.mark.parametrize("subject", ["Stock", "[Hermes] Stock", "a" * 200])
def test_message_marks_only_subject_and_headers(subject):
    msg = mh.build_message({"subject": subject, "text": "unchanged\nbody", "html": "<p>unchanged</p>",
                            "hermes_kind": "products.price_drop\r\nBcc: bad"}, "me@example.org", ["you@example.org"])
    assert str(msg["Subject"]).startswith("[Hermes] ") and str(msg["Subject"]).count("[Hermes]") == 1
    assert msg["X-Hermes"] == "1" and msg["X-Hermes-Kind"] == "products.price_drop-Bcc:-bad"
    assert msg["Bcc"] is None and msg["To"] == "you@example.org" and msg["Message-ID"]
    assert msg.get_body(("plain",)).get_content() == "unchanged\nbody\n"
    assert msg.get_body(("html",)).get_content() == "<p>unchanged</p>\n"


@pytest.mark.parametrize("exists", [True, False])
@pytest.mark.parametrize("localized", [True, False])
def test_label_by_exact_message_id_preserves_other_labels(exists, localized):
    imap = IMAP(exists=exists, localized=localized)
    assert mh.label_hermes(lambda: imap, "<unique@example.org>")
    folder = '"[Gmail]/Todos los mensajes"' if localized else '"[Gmail]/All Mail"'
    assert ("SELECT", folder, False) in imap.calls
    assert ("SEARCH", None, "HEADER", "Message-ID", '"<unique@example.org>"') in imap.calls
    assert ("STORE", b"42,43", "+X-GM-LABELS", '("Hermes")') in imap.calls
    assert (("CREATE", '"Hermes"') in imap.calls) is (not exists)
    assert imap.closed


@pytest.mark.parametrize("failure", ["select", "missing", "store", "raise"])
def test_label_failure_is_bounded_and_closes_connection(monkeypatch, failure):
    monkeypatch.setattr(mh.time, "sleep", lambda _: None)
    imap = IMAP(fail=failure)
    assert not mh.label_hermes(lambda: imap, "<unique@example.org>")
    assert imap.closed and imap.searches <= 3


def test_eventual_sent_copy(monkeypatch):
    monkeypatch.setattr(mh.time, "sleep", lambda _: None)
    imap = IMAP(delay=True)
    assert mh.label_hermes(lambda: imap, "<unique@example.org>") and imap.searches == 2


@pytest.mark.parametrize("gmail", [True, False])
def test_helper_labels_actual_resolved_smtp_account_after_sending(monkeypatch, gmail):
    smtp, imap = SMTP(), IMAP(fail="raise")
    connected = []
    cfg = {"smtp_host": "smtp.gmail.com" if gmail else "smtp.example.org", "smtp_user": "me@example.org"}
    def connect(selector):
        assert smtp.sent
        connected.append(selector)
        return imap
    server = SimpleNamespace(_resolve_send_config=lambda account: ("actual-fallback-account", cfg), _imap_connect=connect)
    monkeypatch.setattr(mh, "_connect", lambda cfg: smtp)
    assert mh.send(server, {"subject": "Stock", "text": "body"})["ok"]
    assert connected == (["actual-fallback-account"] if gmail else [])
    assert smtp.sent[0]["X-Hermes-Kind"] == "notification"


def test_direct_smtp_marks_and_labels_after_delivery_even_if_imap_fails(monkeypatch):
    import imaplib
    smtp = SMTP()
    def unavailable(*args, **kwargs):
        assert smtp.sent and smtp.sent[0]["X-Hermes"] == "1"
        raise OSError("unavailable")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", unavailable)
    assert nc.send_smtp({"host": "smtp.gmail.com", "user": "me@example.org"}, ["me@example.org"],
                        "Stock", "body", smtp_factory=lambda _: smtp, hermes_kind="products.stock") == {"ok": True}
    assert smtp.sent[0]["Subject"] == "[Hermes] Stock"
    assert smtp.sent[0]["X-Hermes-Kind"] == "products.stock"


def test_shared_notification_smtp_also_identifies_hermes():
    smtp = SMTP()
    assert nc.send_smtp({"host": "smtp.example.org"}, ["me@example.org"], "Stock", "body",
                        smtp_factory=lambda _: smtp)["ok"]
    assert smtp.sent[0]["Subject"] == "[Hermes] Stock" and smtp.sent[0]["X-Hermes"] == "1"


def test_helper_receives_notification_kind():
    calls = []
    def run(action, payload, timeout):
        calls.append(payload)
        return {"ok": True}
    assert nc.send_via_helper(run, "Stock", "body", hermes_kind="products.digest")["ok"]
    assert calls[0]["hermes_kind"] == "products.digest" and calls[0]["text"] == "body"
