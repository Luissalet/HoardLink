from pathlib import Path
from types import SimpleNamespace
import pytest
from hoard_link.hub.mediaflow import MediaFlow
from hoard_link.hub import contract
from hoard_link.hub.rules import example_rules


def test_chain_waits_for_owners_and_saves_only_the_transcript(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture")
    calls = []
    def call(app, tool, args, **kwargs):
        calls.append((app, tool, args))
        results = {
            "media_download": {"id": "download", "status": "downloading"},
            "media_status": {"id": "download", "status": "done", "files": [{"path": str(audio)}]},
            "transcribe_file": {"job_id": "speech", "status": "running"},
            "transcribe_status": {"job_id": "speech", "status": "done", "text": "A citable transcript."},
            "library_add_collection": {"collection": {"id": 7}},
            "library_reindex": {"queued": True},
        }
        return {"ok": True, "result": results[tool]}
    monkeypatch.setattr(contract, "call_app", call)
    flow = MediaFlow(SimpleNamespace(get=lambda app: app))
    job = flow.start({"url": "https://example.com/audio", "folder": str(tmp_path)})
    done = flow.status({"job_id": job["job_id"], "wait_s": 5})
    assert done["status"] == "done" and done["stage"] == "index_requested"
    assert "A citable transcript." in Path(done["transcript_path"]).read_text()
    assert [c[1] for c in calls] == ["media_download", "media_status", "transcribe_file", "transcribe_status", "library_add_collection", "library_reindex"]
    assert calls[-1][2] == {"collection_id": 7}
    assert calls[-2][2]["include"] == ["*.transcript.txt"]
    flow.close()


def test_failed_transcription_never_indexes_or_claims_success(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture")
    calls = []
    def call(app, tool, args, **kwargs):
        calls.append(tool)
        return {"ok": True, "result": {"status": "done", "files": [{"path": str(audio)}]}} if tool == "media_status" else {"ok": False, "error": "speech owner unavailable"}
    monkeypatch.setattr(contract, "call_app", call)
    flow = MediaFlow(SimpleNamespace(get=lambda app: app))
    job = flow.start({"download_id": "d", "folder": str(tmp_path)})
    done = flow.status({"job_id": job["job_id"], "wait_s": 5})
    assert not done["ok"] and done["status"] == "failed" and "unavailable" in done["error"]
    assert calls == ["media_status", "transcribe_file"] and not list(tmp_path.glob("*.transcript.txt"))
    flow.close()


def test_collection_policy_and_default_rule_require_opt_in():
    flow = MediaFlow(SimpleNamespace())
    with pytest.raises(ValueError, match="choose a transcript folder"):
        flow.start({"folder": "", "url": "https://example.com"})
    with pytest.raises(ValueError):
        flow.start({"folder": str(Path.home()), "url": "https://example.com"})
    rule = next(r for r in example_rules() if r["id"] == "rule-download-transcribe-index")
    assert rule["enabled"] is False and rule["when"]["where"]["data.media_kind"] == "audio"


def test_funes_error_status_finishes_without_polling_forever(monkeypatch):
    flow = MediaFlow(SimpleNamespace())
    monkeypatch.setattr(flow, "_call", lambda *a: pytest.fail("terminal errors must not be polled"))
    with pytest.raises(RuntimeError, match="decoder failed"):
        flow._finish_job("funes", "transcribe_status", "job_id", {"status": "error", "error": "decoder failed"})
