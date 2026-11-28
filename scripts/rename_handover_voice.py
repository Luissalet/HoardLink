"""Validate the shared GPU voice benchmark receipt for an exact release."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


PHRASE_KINDS = {"importe", "iban", "fecha", "cifra", "pausa"}
CASE_COUNT = 20
FIRST_AUDIO_MS = 1200
GPU_UUID = re.compile(r"GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def require(condition, message):
    if not condition:
        raise ValueError(f"Voice benchmark: {message}")


def within(value, lower, upper):
    return type(value) in (int, float) and lower <= value <= upper


def matching_audio(row, wav_frames):
    stream_frames = row.get("stream_frames")
    if type(stream_frames) is not int or stream_frames <= 0:
        return False
    if stream_frames == wav_frames:
        return True
    rate = row.get("sample_rate")
    return (row.get("truncated") is True and row.get("truncate_reason") == "duration_guard"
            and row.get("parity_ok") is True and row.get("parity_reason") == "duration_guard"
            and row.get("duration_ok") is True and type(rate) is int and rate > 0
            and row.get("stream_sample_rate") == rate)


def validate_phrases(rows):
    require(isinstance(rows, list) and len(rows) == CASE_COUNT, "expected twenty new phrases")
    kinds = set()
    texts = set()
    latencies = []
    for index, row in enumerate(rows, 1):
        require(isinstance(row, dict) and row.get("n") == index, f"missing or reordered phrase {index}")
        kind, text = row.get("kind"), row.get("text")
        require(isinstance(kind, str) and kind in PHRASE_KINDS and isinstance(text, str)
                and len(re.findall(r"\b[^\W\d_]+\b", text)) >= 15 and text not in texts,
                f"phrase {index} must be new, have at least fifteen words and cover a corpus category")
        kinds.add(kind)
        texts.add(text)
        require(row.get("ok") is True and row.get("state") == "succeeded"
                and row.get("cached") is False and row.get("silent") is False
                and row.get("pcm_mismatches") == [] and not row.get("live_error"),
                f"phrase {index} must finish with valid, uncached and audible PCM")
        elapsed = row.get("first_audio_ms")
        total = row.get("wav_frames")
        first = row.get("first_frames")
        buffered = row.get("buffered_frames")
        require(within(elapsed, 0, FIRST_AUDIO_MS) and type(total) is int and total > 0
                and matching_audio(row, total)
                and type(first) is int and 0 < 5 * first < total and first <= row["stream_frames"]
                and type(buffered) is int and 0 <= 5 * buffered < total,
                f"phrase {index} exceeded the time or frame limit")
        latencies.append(elapsed)
    require(kinds == PHRASE_KINDS, "cover amounts, IBANs, dates, long numbers and marked pauses")
    return max(latencies)


def validate_cancellations(rows, child):
    require(isinstance(rows, list) and len(rows) == CASE_COUNT, "expected twenty cancellations")
    for index, row in enumerate(rows, 1):
        require(isinstance(row, dict) and row.get("n") == index
                and row.get("requested") is True and row.get("accepted") is True
                and row.get("ok") is True and row.get("terminal_state") == "cancelled"
                and within(row.get("position_pct"), 40, 60) and row.get("same_pid") is True
                and row.get("pid") == child and row.get("recovery_state") == "succeeded"
                and row.get("recovery_cached") is False
                and within(row.get("recovery_first_audio_ms"), 0, FIRST_AUDIO_MS),
                f"cancellation {index} must stop mid-utterance and resume on the original child")


def validate_voice_benchmark(proof, commit):
    require(isinstance(proof, dict) and isinstance(proof.get("result"), str)
            and isinstance(proof.get("sha256"), str),
            "pin the passing result.json and its SHA-256 in the installation plan")
    approved_gpu = proof.get("gpu_uuid")
    require(isinstance(approved_gpu, str) and GPU_UUID.fullmatch(approved_gpu),
            "pin the UUID of the approved voice test device in the installation plan")
    path = Path(proof["result"])
    require(path.is_absolute() and path.name == "result.json", "use the shared bank's absolute result.json path")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    require(digest == proof["sha256"].lower(), "result.json does not match the pinned SHA-256")
    report = json.loads(payload.decode("utf-8-sig"))
    require(isinstance(report, dict) and report.get("sha") == commit and report.get("verdict") == "pass",
            "a passing result for the exact voice candidate is required")
    gpu = report.get("gpu_uuid")
    card = (path.parent / "gpu2-before.csv").read_text(encoding="utf-8-sig").strip().split(",")
    # Receipts from the original bank always used GPU index 2.
    index = report.get("gpu_index", 2)
    require(type(index) is int and index in (1, 2)
            and isinstance(gpu, str) and gpu.lower() == approved_gpu.lower()
            and len(card) >= 2 and card[0].strip() == str(index) and card[1].strip().lower() == gpu.lower(),
            "the benchmark and device inventory must match the approved GPU UUID and run index")
    child = report.get("initial_child_pid")
    service = report.get("service_pid")
    require(type(child) is int and child > 0 and type(service) is int and service > 0 and service != child
            and report.get("final_child_pid") == child, "the resident voice child must be distinct and remain stable")
    slowest = validate_phrases(report.get("phrases"))
    validate_cancellations(report.get("cancels"), child)
    return {"commit": commit, "result": str(path), "sha256": digest, "gpu_uuid": gpu, "gpu_index": index,
            "child_pid": child, "worst_first_audio_ms": slowest,
            "phrases": CASE_COUNT, "cancellations": CASE_COUNT}
