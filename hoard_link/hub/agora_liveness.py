"""Turn liveness for the Agora team console: did a provider turn actually move work?

Pure functions, no I/O, so every rule can be unit-tested. The console gathers
``evidence`` (counts of what the leader's Agora identity did during the turn)
and this module decides two things:

1. ``classify_turn`` - what kind of turn it was.
2. ``decide_continuation`` - whether the console should relaunch the leader.

Classification rules, applied in this order (first match wins):

* ``failed``          the CLI failed (state not ``completed``, non-zero exit
                      code, or a provider error).
* ``empty_response``  the answer is empty/whitespace and there is no concrete
                      evidence.
* ``completed``       evidence of ``done`` or ``submits`` (a task was closed or
                      handed to review), whatever the answer says.
* ``blocked``         the answer says the agent is blocked or waits for the
                      owner ("blocked", "bloqueado", "waiting for Luis",
                      "necesito que Luis", "need the owner", ...).
* ``advanced``        any other concrete evidence: claims, task status
                      changes, reviews given, commits reported, messages
                      posted (the console's own auto-post of the answer is
                      never counted, and a message that only announces future
                      work without a commit reference - ``plan_messages`` -
                      does not count either).
* ``plan_only``       the answer only announces future work ("next I will",
                      "I'll now", "voy a", "a continuación", "siguiente paso",
                      "I will start", "plan:") and there is no concrete
                      evidence.
* ``needs_followup``  anything else: the turn ended without evidence and
                      without a recognisable plan or block.

A heartbeat with a ``doing`` text is *weak* evidence: every console turn is told
to send one first, so it never counts as progress on its own. It is reported in
the reasons but does not move a turn out of ``plan_only``/``empty_response``.

Continuation rules (``decide_continuation``):

* Only ``plan_only`` and ``empty_response`` continue.
* A leader whose provider quota is exhausted is never continued (it would burn
  turns that cannot run); it is picked up again by real events after its reset.
* At most ``MAX_CONTINUATIONS`` (2) continuations per originating turn chain.
* Throttle: after ``THROTTLE_AFTER`` (2) consecutive no-progress turns the next
  continuation waits 120 s, doubling for every further no-progress turn up to
  1800 s (time already elapsed since the last continuation is credited).
* A real event for the agent (a new message, assignment or review since the
  last turn) bypasses the throttle.
* The idempotency key is ``liveness:<root_turn_id>:<liveness>:<n>``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

LIVENESS_CLASSES = ("completed", "advanced", "plan_only", "empty_response", "blocked", "failed", "needs_followup")
CONTINUING = ("plan_only", "empty_response")
NO_PROGRESS = ("plan_only", "empty_response", "needs_followup")
MAX_CONTINUATIONS = 2
THROTTLE_AFTER = 2
THROTTLE_BASE_S = 120.0
THROTTLE_MAX_S = 1800.0

# Evidence counts that prove the agent touched Agora work. ``heartbeats_with_doing`` is deliberately absent.
STRONG_EVIDENCE = ("messages_posted", "task_status_changes", "submits", "done", "claims", "reviews", "commits_reported")
WEAK_EVIDENCE = ("heartbeats_with_doing",)

_BLOCKED_RE = re.compile(
    r"\b(?:blocked|bloquead[oa]s?|bloqueo)\b"
    r"|waiting\s+(?:for|on)\s+(?:luis|the\s+owner|the\s+user|your\s+(?:decision|answer|input))"
    r"|esperando\s+(?:a\s+|que\s+)?(?:luis|el\s+due[ñn]o|tu\s+(?:respuesta|decisi[oó]n))"
    r"|necesito\s+que\s+(?:luis|el\s+due[ñn]o|me\s+(?:confirm|aclar|respond|diga|des))"
    r"|need(?:s)?\s+(?:the\s+owner|luis|your\s+(?:decision|answer|input|confirmation))"
    r"|(?:i|we)\s+need\s+(?:a\s+)?(?:decision|answer)\s+from",
    re.IGNORECASE)

_PLAN_RE = re.compile(
    r"\bnext,?\s+i(?:'ll|\s+will|\s+am\s+going\s+to)\b"
    r"|\bi(?:'ll|\s+will)\s+(?:now|start|begin|proceed|next|then)\b"
    r"|\bi(?:'m|\s+am)\s+(?:about\s+to|going\s+to)\b"
    r"|\bnext\s+steps?\b"
    r"|\bplan\s*:"
    r"|\bvoy\s+a\s+\w+"
    r"|\ba\s+continuaci[oó]n\b"
    r"|\b(?:el\s+)?siguiente\s+paso\b"
    r"|\bluego\s+(?:voy|har[eé]|empezar[eé]|abrir[eé])\b"
    r"|\b(?:ahora\s+)?(?:har[eé]|empezar[eé]|proceder[eé]|comenzar[eé])\b",
    re.IGNORECASE)


def is_plan_text(text: str) -> bool:
    """True when ``text`` only announces future work (a plan phrase and no commit reference)."""
    body = text or ""
    return bool(_PLAN_RE.search(body)) and not _COMMIT_RE.search(body)


_COMMIT_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def _count(evidence: Optional[Mapping[str, Any]], key: str) -> int:
    try:
        return max(0, int((evidence or {}).get(key) or 0))
    except (TypeError, ValueError):
        return 0


def classify_turn(*, state: str, exit_code: Optional[int], answer: str, evidence: Optional[Mapping[str, Any]],
                  provider_error: str = "") -> dict[str, Any]:
    """Classify one finished console turn. See the module docstring for the rules."""
    text = (answer or "").strip()
    strong = {key: _count(evidence, key) for key in STRONG_EVIDENCE}
    # Messages that only announce future work are not progress: "voy a hacer X" posted to the Agora is still a plan.
    strong["messages_posted"] = max(0, strong["messages_posted"] - _count(evidence, "plan_messages"))
    weak = {key: _count(evidence, key) for key in WEAK_EVIDENCE}
    has_evidence = any(strong.values())
    reasons: list[str] = []

    if state != "completed":
        return {"liveness": "failed", "reasons": [f"turn state is {state or 'unknown'}"]}
    if exit_code not in (None, 0):
        return {"liveness": "failed", "reasons": [f"CLI exit code {exit_code}"]}
    if provider_error:
        return {"liveness": "failed", "reasons": ["provider error: " + str(provider_error)[:200]]}

    if not text and not has_evidence:
        reasons.append("empty answer and no Agora activity")
        if any(weak.values()):
            reasons.append("only a heartbeat was sent")
        return {"liveness": "empty_response", "reasons": reasons}

    if strong["done"] or strong["submits"]:
        reasons = [f"{key}={strong[key]}" for key in ("done", "submits") if strong[key]]
        return {"liveness": "completed", "reasons": reasons}

    match = _BLOCKED_RE.search(text)
    if match:
        return {"liveness": "blocked", "reasons": [f"answer states a block: '{match.group(0)[:60]}'"]}

    if has_evidence:
        return {"liveness": "advanced", "reasons": [f"{key}={value}" for key, value in strong.items() if value]}

    match = _PLAN_RE.search(text)
    if match:
        reasons = [f"answer announces future work: '{match.group(0)[:60]}'", "no task or message evidence"]
        if any(weak.values()):
            reasons.append("only a heartbeat was sent")
        return {"liveness": "plan_only", "reasons": reasons}

    reasons = ["answer has no evidence and no recognisable plan or block"]
    if any(weak.values()):
        reasons.append("only a heartbeat was sent")
    return {"liveness": "needs_followup", "reasons": reasons}


def continuation_key(root_turn_id: str, liveness: str, n: int) -> str:
    return f"liveness:{root_turn_id}:{liveness}:{n}"


def throttle_wait(consecutive_no_progress: int) -> float:
    """Seconds to wait before a continuation given the run of no-progress turns (0 below the threshold)."""
    if consecutive_no_progress < THROTTLE_AFTER:
        return 0.0
    return min(THROTTLE_BASE_S * (2 ** (consecutive_no_progress - THROTTLE_AFTER)), THROTTLE_MAX_S)


def decide_continuation(*, liveness: str, prior_continuations: int, consecutive_no_progress: int,
                        last_continuation_at: Optional[float], now: float, real_event_since: bool,
                        root_turn_id: str = "", quota_exhausted: bool = False) -> dict[str, Any]:
    """Decide whether to relaunch the leader after a turn classified as ``liveness``."""
    n = max(0, int(prior_continuations or 0)) + 1
    key = continuation_key(root_turn_id, liveness, n)
    if liveness not in CONTINUING:
        return {"continue": False, "delay_s": 0.0, "key": key, "reason": f"{liveness} turns are not continued"}
    if quota_exhausted:
        return {"continue": False, "delay_s": 0.0, "key": key, "reason": "provider quota exhausted; not relaunched until it resets"}
    if n > MAX_CONTINUATIONS:
        return {"continue": False, "delay_s": 0.0, "key": key,
                "reason": f"already continued {MAX_CONTINUATIONS} times for this turn chain"}
    delay = 0.0
    reason = "continue immediately"
    if real_event_since:
        reason = "continue: a real event for the agent bypasses the throttle"
    else:
        wait = throttle_wait(int(consecutive_no_progress or 0))
        if wait > 0:
            elapsed = (now - last_continuation_at) if last_continuation_at else 0.0
            delay = max(0.0, wait - max(0.0, elapsed))
            reason = f"continue after throttle: {int(consecutive_no_progress)} no-progress turns, wait {int(wait)}s"
    return {"continue": True, "delay_s": float(delay), "key": key, "reason": reason}


def continuation_prompt(liveness: str, reasons: list[str], original_prompt: str) -> str:
    """Prompt for an automatic continuation turn (resumes the leader's session)."""
    why = "; ".join(reasons) if reasons else "no reason recorded"
    head = ("Your previous Agora console turn ended with " + liveness + " (" + why + "). "
            "Do the next concrete step now: claim/advance a task, post evidence, or say exactly what blocks you. "
            "Do not answer with a plan or an announcement of future work; act on Agora and report what you did.\n\n"
            "Original context of this turn chain:\n")
    return head + (original_prompt or "")[-max(0, 30000 - len(head)):]
