"""Local regression checks for the migration's voice evidence gate."""

import hashlib
import json

import pytest

from scripts.rename_handover_voice import validate_voice_benchmark


COMMIT = "b" * 40
GPU = "GPU-12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def synthetic_report():
    kinds = ("importe", "iban", "fecha", "cifra", "pausa")
    return {
        "sha": COMMIT, "verdict": "pass", "gpu_uuid": GPU, "service_pid": 12,
        "initial_child_pid": 13, "final_child_pid": 13,
        "phrases": [
            {"n": i, "kind": kinds[i % len(kinds)],
             "text": f"Caso {i} para evaluar si esta frase nueva tiene palabras suficientes en el banco de voz local.",
             "ok": True, "state": "succeeded", "cached": False, "silent": False, "pcm_mismatches": [],
             "first_audio_ms": 1200, "wav_frames": 100, "stream_frames": 100,
             "first_frames": 19, "buffered_frames": 19}
            for i in range(1, 21)
        ],
        "cancels": [
            {"n": i, "ok": True, "requested": True, "accepted": True, "terminal_state": "cancelled",
             "position_pct": 50, "same_pid": True, "pid": 13, "recovery_state": "succeeded",
             "recovery_cached": False, "recovery_first_audio_ms": 500}
            for i in range(1, 21)
        ],
    }


def write_synthetic_receipt(folder, report, gpu_index=2):
    (folder / "gpu2-before.csv").write_text(f"{gpu_index}, {GPU}, pci, 0, 16000", encoding="utf-8")
    path = folder / "result.json"
    data = json.dumps(report).encode("utf-8")
    path.write_bytes(data)
    return {"result": str(path), "sha256": hashlib.sha256(data).hexdigest(), "gpu_uuid": GPU}


def test_exact_commit_and_inclusive_latency_boundary(tmp_path, synthetic_report):
    receipt = write_synthetic_receipt(tmp_path, synthetic_report)
    summary = validate_voice_benchmark(receipt, COMMIT)
    assert (summary["phrases"], summary["cancellations"], summary["worst_first_audio_ms"]) == (20, 20, 1200)
    with pytest.raises(ValueError, match="exact voice candidate"):
        validate_voice_benchmark(receipt, "a" * 40)
    with pytest.raises(ValueError, match="pinned SHA-256"):
        validate_voice_benchmark({**receipt, "sha256": "0" * 64}, COMMIT)


def test_gpu_inventory_has_to_match_the_test_device(tmp_path, synthetic_report):
    receipt = write_synthetic_receipt(tmp_path, synthetic_report, gpu_index=1)
    with pytest.raises(ValueError, match="approved GPU UUID and run index"):
        validate_voice_benchmark(receipt, COMMIT)
    synthetic_report["gpu_index"] = 1
    receipt = write_synthetic_receipt(tmp_path, synthetic_report, gpu_index=1)
    assert validate_voice_benchmark(receipt, COMMIT)["gpu_index"] == 1

    with pytest.raises(ValueError, match="pin the UUID"):
        validate_voice_benchmark({**receipt, "gpu_uuid": ""}, COMMIT)
    other_gpu = "GPU-87654321-1234-1234-1234-123456789abc"
    with pytest.raises(ValueError, match="approved GPU UUID and run index"):
        validate_voice_benchmark({**receipt, "gpu_uuid": other_gpu}, COMMIT)

    synthetic_report["gpu_index"] = 0
    receipt = write_synthetic_receipt(tmp_path, synthetic_report, gpu_index=0)
    with pytest.raises(ValueError, match="approved GPU UUID and run index"):
        validate_voice_benchmark(receipt, COMMIT)


@pytest.mark.parametrize(("group", "field", "value", "message"), [
    ("phrases", "first_audio_ms", 1201, "time or frame limit"),
    ("phrases", "first_frames", 20, "time or frame limit"),
    ("phrases", "buffered_frames", 20, "time or frame limit"),
    ("phrases", "silent", True, "audible PCM"),
    ("phrases", "cached", True, "uncached"),
    ("cancels", "pid", 14, "original child"),
    ("cancels", "terminal_state", "succeeded", "stop mid-utterance"),
])
def test_a_passing_label_cannot_override_measurements(tmp_path, synthetic_report, group, field, value, message):
    synthetic_report[group][0][field] = value
    receipt = write_synthetic_receipt(tmp_path, synthetic_report)
    with pytest.raises(ValueError, match=message):
        validate_voice_benchmark(receipt, COMMIT)


def test_measured_duration_guard_can_end_live_audio_before_the_retry_wav(tmp_path, synthetic_report):
    row = synthetic_report["phrases"][0]
    row.update(
        stream_frames=80, truncated=True, truncate_reason="duration_guard", parity_ok=True,
        parity_reason="duration_guard", duration_ok=True, sample_rate=24000, stream_sample_rate=24000,
    )
    receipt = write_synthetic_receipt(tmp_path, synthetic_report)
    assert validate_voice_benchmark(receipt, COMMIT)["phrases"] == 20

    for invalid in (
        {"truncated": False}, {"truncate_reason": "other"}, {"parity_reason": "unmarked_difference"},
        {"duration_ok": False}, {"stream_sample_rate": 16000}, {"stream_frames": 0},
    ):
        changed = {**synthetic_report, "phrases": [{**row, **invalid}, *synthetic_report["phrases"][1:]]}
        receipt = write_synthetic_receipt(tmp_path, changed)
        with pytest.raises(ValueError, match="time or frame limit"):
            validate_voice_benchmark(receipt, COMMIT)
