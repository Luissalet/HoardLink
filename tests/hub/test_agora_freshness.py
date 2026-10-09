"""A1/A2: say when an agent acts on a task without having read its thread, and an inbox of metadata only.

The Ágora knows what each agent has seen (thread acks, the inbox read mark), so an answer can say "others wrote
here since you last read it". It only informs: nothing is ever refused for it (Luis, 09-10: no more obstacles for
the agents' autonomy).
"""

from __future__ import annotations

import pytest

from hoard_link.hub.agora import Agora


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def ag(tmp_path):
    events = []
    a = Agora(tmp_path / "agora.db", clock=Clock(), emit=lambda t, d: events.append((t, d)), freshness="warn")
    a.events = events
    yield a
    a.close()


def _submitted(a):
    """claude owns a task in review (revision 1); codex is the reviewer-to-be."""
    tid = a.task_add({"agent": "claude", "title": "Agora UI", "repo": "HoardLink", "paths": ["x.js"],
                      "claim": True})["task"]["id"]
    a.task_submit({"agent": "claude", "task_id": tid, "summary": "done, 12 tests", "commits": ["abc123"]})
    return tid, a.task({"task_id": tid})["task"]["thread_id"]


def _review(a, tid, agent="codex", **extra):
    return a.task_review({"agent": agent, "task_id": tid, "expected_submission_revision": 1,
                          "verdict": "approve", "body": "checked", **extra})


def test_a_review_of_an_unread_submission_goes_through_and_says_so(ag):
    tid, thread = _submitted(ag)
    before = len(ag.task({"task_id": tid})["messages"])
    r = _review(ag, tid)
    assert r["ok"] and r["task"]["status"] == "approved"
    u = r["unseen"]
    assert u["count"] == 1 and u["action"] == "review"
    assert u["messages"][0]["author"] == "claude" and u["messages"][0]["kind"] == "proposal"
    msgs = ag.task({"task_id": tid})["messages"]
    assert len(msgs) == before + 1 and msgs[-1]["kind"] == "approve"     # only the vote: nothing else written
    ev = [d for t, d in ag.events if t == "agora.task.unseen"]
    assert ev and ev[-1]["agent"] == "codex" and ev[-1]["count"] == 1


def test_no_notice_once_the_task_or_thread_was_read(ag):
    tid, thread = _submitted(ag)
    ag.task({"task_id": tid, "agent": "codex"})              # reading the task as codex marks its thread read
    assert "unseen" not in _review(ag, tid)
    tid2, thread2 = _submitted(ag)
    ag.thread({"thread_id": thread2, "agent": "codex"})
    assert "unseen" not in _review(ag, tid2)


def test_a_non_peek_inbox_counts_as_read_and_a_peek_does_not(ag):
    tid, thread = _submitted(ag)
    ag.task({"task_id": tid, "agent": "codex"})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                    "verdict": "changes", "body": "one thing"})
    ag.inbox({"agent": "claude", "peek": True})
    assert ag.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed"})["unseen"]["count"] == 1
    ag.task({"task_id": tid, "agent": "codex"})
    ag.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 2,
                    "verdict": "changes", "body": "and another"})
    ag.inbox({"agent": "claude"})                            # delivers the bodies: read
    assert "unseen" not in ag.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed again"})


def test_the_task_description_own_messages_and_bookkeeping_never_count(ag):
    tid = ag.task_add({"agent": "claude", "title": "Open work", "body": "details"})["task"]["id"]
    assert "unseen" not in ag.task_claim({"agent": "codex", "task_id": tid})
    mine = ag.task_add({"agent": "claude", "title": "Mine", "claim": True})["task"]["id"]
    ag.task_submit({"agent": "claude", "task_id": mine, "summary": "first"})
    assert "unseen" not in ag.task_submit({"agent": "claude", "task_id": mine, "summary": "second"})


def test_done_and_post_say_so_too_but_other_threads_do_not(ag):
    tid, thread = _submitted(ag)
    ag.task({"task_id": tid, "agent": "codex"})
    _review(ag, tid)
    assert ag.post({"agent": "codex", "thread_id": thread, "body": "note"}).get("unseen") is None
    done = ag.task_done({"agent": "claude", "task_id": tid, "result": "merged"})
    assert done["ok"] and done["unseen"]["count"] == 2 and done["unseen"]["action"] == "done"
    other = ag.thread_open({"agent": "claude", "title": "Free debate", "body": "what do you think?"})["thread"]["id"]
    assert "unseen" not in ag.post({"agent": "codex", "thread_id": other, "body": "a thought"})


def test_the_person_and_off_mode_get_no_notice(ag, tmp_path):
    tid, thread = _submitted(ag)
    assert "unseen" not in _review(ag, tid, agent="luis", _person=True)
    off = Agora(tmp_path / "off.db", freshness="off")
    tid2, _ = _submitted(off)
    assert "unseen" not in _review(off, tid2)
    off.close()


def test_modes_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_AGORA_FRESHNESS", raising=False)
    d = Agora(tmp_path / "default.db")
    assert d.freshness == "warn"
    d.close()
    monkeypatch.setenv("HOARD_AGORA_FRESHNESS", "enforce")          # no blocking mode exists: falls back to warn
    e = Agora(tmp_path / "env.db")
    assert e.freshness == "warn"
    e.close()


def test_metadata_only_inbox_has_no_bodies_and_keeps_the_read_mark(ag):
    tid, thread = _submitted(ag)
    meta = ag.inbox({"agent": "codex", "metadata_only": True})
    n = meta["counts"]["messages"]
    assert meta["metadata_only"] and n >= 1
    assert all("body" not in m and "body_chars" in m for m in meta["messages"])
    m = meta["messages"][-1]                              # the submission
    assert m["author"] == "claude" and m["thread_id"] == thread and m["kind"] == "proposal" and m["body_chars"] > 0
    assert ag.inbox({"agent": "codex", "metadata_only": True})["counts"]["messages"] == n
    assert _review(ag, tid)["unseen"]["count"] == 1      # nothing was delivered: still unread
