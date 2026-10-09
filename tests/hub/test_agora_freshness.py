"""A1/A2: acting on a task only after reading what others wrote there, and an inbox of metadata only.

Idea from a multi-agent workspace study (#130): an agent that votes, submits or closes a task while the
other agent's last message there is still unread works on a stale picture. The Ágora knows what each agent
has seen (thread acks, the inbox read mark), so it can say so — and, in "enforce" mode, refuse.
"""

from __future__ import annotations

import pytest

from hoard_link.hub.agora import Agora, AgoraError


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1
        return self.t


def _agora(tmp_path, mode):
    events = []
    a = Agora(tmp_path / f"agora-{mode}.db", clock=Clock(), emit=lambda t, d: events.append((t, d)), freshness=mode)
    a.events = events
    return a


@pytest.fixture
def enforce(tmp_path):
    a = _agora(tmp_path, "enforce")
    yield a
    a.close()


@pytest.fixture
def warn(tmp_path):
    a = _agora(tmp_path, "warn")
    yield a
    a.close()


def _submitted(a):
    """claude owns a task in review (revision 1); codex is the reviewer-to-be."""
    tid = a.task_add({"agent": "claude", "title": "Agora UI", "repo": "HoardLink", "paths": ["x.js"],
                      "claim": True})["task"]["id"]
    a.task_submit({"agent": "claude", "task_id": tid, "summary": "done, 12 tests", "commits": ["abc123"]})
    return tid, a.task({"task_id": tid})["task"]["thread_id"]


def test_the_task_description_alone_never_blocks_a_claim(enforce):
    tid = enforce.task_add({"agent": "claude", "title": "Open work", "body": "details"})["task"]["id"]
    assert enforce.task_claim({"agent": "codex", "task_id": tid})["ok"]


def test_enforce_refuses_a_review_of_an_unread_submission_and_lists_it(enforce):
    tid, thread = _submitted(enforce)
    with pytest.raises(AgoraError) as exc:
        enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                             "verdict": "approve", "body": "looks fine"})
    err = exc.value
    assert err.status == 409 and err.extra["error_code"] == "unseen_messages"
    assert err.extra["unseen_count"] == 1 and err.extra["unseen"][0]["author"] == "claude"
    assert err.extra["unseen"][0]["kind"] == "proposal"
    assert "unseen_ok" in str(err)
    # Reading the task with the agent counts as reading its thread.
    enforce.task({"task_id": tid, "agent": "codex"})
    assert enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                                "verdict": "approve", "body": "read and checked"})["ok"]


def test_reading_the_thread_or_the_inbox_counts_as_seen(enforce):
    tid, thread = _submitted(enforce)
    enforce.thread({"thread_id": thread, "agent": "codex"})
    assert enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                                "verdict": "changes", "body": "one thing"})["ok"]
    # claude has not seen the verdict; a (non-peek) inbox delivers it with its body.
    with pytest.raises(AgoraError):
        enforce.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed"})
    assert enforce.inbox({"agent": "claude", "peek": True})["counts"]["messages"] == 1
    with pytest.raises(AgoraError):                      # a peek delivers nothing: still unseen
        enforce.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed"})
    enforce.inbox({"agent": "claude"})
    assert enforce.task_submit({"agent": "claude", "task_id": tid, "summary": "fixed"})["ok"]


def test_unseen_ok_overrides_and_is_recorded_in_the_thread(enforce):
    tid, thread = _submitted(enforce)
    with pytest.raises(AgoraError):                      # a reason that says nothing is not a reason
        enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                             "verdict": "approve", "body": "ok", "unseen_ok": "x"})
    r = enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                             "verdict": "approve", "body": "ok",
                             "unseen_ok": "the diff was read in the worktree, not here"})
    assert r["ok"]
    lines = [m for m in enforce.task({"task_id": tid})["messages"] if m["kind"] == "system"]
    assert any("codex hace «review» sin haber leído 1 mensaje(s)" in m["body"]
               and "the diff was read in the worktree" in m["body"] for m in lines)


def test_posting_in_a_task_thread_is_guarded_but_other_threads_are_not(enforce):
    tid, thread = _submitted(enforce)
    with pytest.raises(AgoraError):
        enforce.post({"agent": "codex", "thread_id": thread, "body": "quick question"})
    other = enforce.thread_open({"agent": "claude", "title": "Free debate", "body": "what do you think?"})["thread"]["id"]
    assert enforce.post({"agent": "codex", "thread_id": other, "body": "a thought"})["ok"]


def test_own_messages_and_bookkeeping_lines_never_count(enforce):
    tid = enforce.task_add({"agent": "claude", "title": "Mine", "claim": True})["task"]["id"]
    # claude's own submission and the system lines of its own claim are not "unread" for claude
    enforce.task_submit({"agent": "claude", "task_id": tid, "summary": "first"})
    assert enforce.task_submit({"agent": "claude", "task_id": tid, "summary": "second"})["ok"]


def test_done_after_an_unread_review_is_refused(enforce):
    tid, thread = _submitted(enforce)
    enforce.task({"task_id": tid, "agent": "codex"})
    enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                         "verdict": "approve", "body": "approved, but see the note about the cache"})
    with pytest.raises(AgoraError) as exc:
        enforce.task_done({"agent": "claude", "task_id": tid, "result": "merged"})
    assert exc.value.extra["unseen"][0]["kind"] == "approve"
    enforce.task({"task_id": tid, "agent": "claude"})
    assert enforce.task_done({"agent": "claude", "task_id": tid, "result": "merged"})["ok"]


def test_the_person_is_never_blocked(enforce):
    tid, thread = _submitted(enforce)
    assert enforce.task_review({"agent": "luis", "task_id": tid, "expected_submission_revision": 1,
                                "verdict": "approve", "body": "fine", "_person": True})["ok"]


def test_warn_mode_acts_and_says_so_without_writing_in_the_thread(warn):
    tid, thread = _submitted(warn)
    before = len(warn.task({"task_id": tid})["messages"])
    r = warn.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                          "verdict": "approve", "body": "fine"})
    assert r["ok"] and r["unseen"]["count"] == 1 and r["unseen"]["action"] == "review"
    msgs = warn.task({"task_id": tid})["messages"]
    assert len(msgs) == before + 1 and msgs[-1]["kind"] == "approve"     # only the vote, no extra line
    ev = [d for t, d in warn.events if t == "agora.task.unseen"]
    assert ev and ev[-1]["agent"] == "codex" and ev[-1]["count"] == 1


def test_off_mode_and_default(tmp_path, monkeypatch):
    off = _agora(tmp_path, "off")
    tid, _ = _submitted(off)
    r = off.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                         "verdict": "approve", "body": "fine"})
    assert r["ok"] and "unseen" not in r
    off.close()
    monkeypatch.delenv("HOARD_AGORA_FRESHNESS", raising=False)
    d = Agora(tmp_path / "default.db")
    assert d.freshness == "warn"
    d.close()
    monkeypatch.setenv("HOARD_AGORA_FRESHNESS", "enforce")
    e = Agora(tmp_path / "env.db")
    assert e.freshness == "enforce"
    e.close()


def test_metadata_only_inbox_has_no_bodies_and_keeps_the_read_mark(enforce):
    tid, thread = _submitted(enforce)
    meta = enforce.inbox({"agent": "codex", "metadata_only": True})
    n = meta["counts"]["messages"]
    assert meta["metadata_only"] and n >= 1
    assert all("body" not in m and "body_chars" in m for m in meta["messages"])
    m = meta["messages"][-1]                              # the submission
    assert m["author"] == "claude" and m["thread_id"] == thread and m["kind"] == "proposal" and m["body_chars"] > 0
    # nothing was delivered: the same messages are still new, and still unseen for the guard
    assert enforce.inbox({"agent": "codex", "metadata_only": True})["counts"]["messages"] == n
    with pytest.raises(AgoraError):
        enforce.task_review({"agent": "codex", "task_id": tid, "expected_submission_revision": 1,
                             "verdict": "approve", "body": "fine"})
