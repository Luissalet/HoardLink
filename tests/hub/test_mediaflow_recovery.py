import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from hoard_link.hub.mediaflow import MediaFlow
from hoard_link.hub import contract


def hub(tmp_path):
    return SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path / "hub")), get=lambda app: app)


def test_restart_preserves_ids_and_resume_does_not_resubmit_jobs(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"sample")
    first = MediaFlow(hub(tmp_path))
    calls = []
    def call(app, tool, args, **kwargs):
        calls.append(tool)
        if tool == "media_download":
            return {"ok": True, "result": {"id": "download", "status": "done", "files": [{"path": str(audio)}]}}
        if tool == "transcribe_file":
            first.close()  # reply received just as the operator closes the Hub
            return {"ok": True, "result": {"job_id": "speech", "status": "running"}}
        if tool == "media_status":
            return {"ok": True, "result": {"id": "download", "status": "done", "files": [{"path": str(audio)}]}}
        if tool == "transcribe_status":
            return {"ok": True, "result": {"job_id": "speech", "status": "done", "text": "Recovered evidence.", "model": "fixture"}}
        if tool == "library_add_collection":
            return {"ok": True, "result": {"collection": {"id": 7}}}
        if tool == "library_reindex":
            return {"ok": True, "result": {"queued": True}}
        pytest.fail(tool)
    monkeypatch.setattr(contract, "call_app", call)
    started = first.start({"url": "https://example.com/audio?token=private#secret", "folder": str(tmp_path)})
    paused = first.status({"job_id": started["job_id"], "wait_s": 5})
    assert paused["status"] == "paused" and paused["transcribe_job_id"] == "speech"
    second = MediaFlow(hub(tmp_path))
    assert second.list()["jobs"][0]["status"] == "paused"
    assert len(calls) == 2  # construction never resumes work automatically
    second.resume({"job_id": started["job_id"]})
    done = second.status({"job_id": started["job_id"], "wait_s": 5})
    assert done["status"] == "done" and done["result"]["index_requested"]
    assert calls.count("media_download") == calls.count("transcribe_file") == 1
    path = Path(done["transcript_path"])
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert provenance["transcript_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "private" not in path.read_text() and "secret" not in json.dumps(provenance)
    assert "arguments" not in done
    second.close()


def test_lost_reply_requires_reconciliation_and_never_duplicates_work(tmp_path, monkeypatch):
    def lost(app, tool, args, **kwargs):
        raise TimeoutError("response lost after submission")
    monkeypatch.setattr(contract, "call_app", lost)
    flow = MediaFlow(hub(tmp_path))
    job = flow.start({"url": "https://example.com/audio", "folder": str(tmp_path)})
    result = flow.status({"job_id": job["job_id"], "wait_s": 5})
    assert result["status"] == "paused" and result["pending"] == "media_download"
    flow.close()
    restored = MediaFlow(hub(tmp_path))
    with pytest.raises(ValueError, match="provide download_id"):
        restored.resume({"job_id": job["job_id"]})
    assert restored.status({"job_id": job["job_id"]})["status"] == "paused"
    restored.close()


def test_restart_during_index_request_does_not_requeue_it(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"sample")
    flow = MediaFlow(hub(tmp_path))
    calls = []
    def call(app, tool, args, **kwargs):
        calls.append(tool)
        if tool == "media_status":
            value = {"id": "download", "status": "done", "files": [{"path": str(audio)}]}
        elif tool == "transcribe_file":
            value = {"job_id": "speech", "status": "done", "text": "Transcript."}
        elif tool == "library_add_collection":
            value = {"collection": {"id": 7}}
        else:
            flow.close()
            value = {"queued": True}
        return {"ok": True, "result": value}
    monkeypatch.setattr(contract, "call_app", call)
    job = flow.start({"download_id": "download", "folder": str(tmp_path)})
    paused = flow.status({"job_id": job["job_id"], "wait_s": 5})
    assert paused["status"] == "paused" and paused["index_requested"] is True
    recovered = MediaFlow(hub(tmp_path))
    recovered.resume({"job_id": job["job_id"]})
    assert recovered.status({"job_id": job["job_id"], "wait_s": 5})["status"] == "done"
    assert calls.count("library_reindex") == 1
    recovered.close()


def test_saved_transcript_modified_after_pause_is_not_silently_indexed(tmp_path, monkeypatch):
    folder = tmp_path / "transcripts"
    folder.mkdir()
    path = folder / "sample.transcript.txt"
    path.write_text("reviewed by human")
    state = {"job_id": "fixture", "created_at": 0, "status": "running", "stage": "indexing", "arguments": {},
             "folder": str(folder), "transcript_path": str(path), "transcript_sha256": "wrong"}
    data = tmp_path / "hub"
    data.mkdir()
    (data / "media-imports.json").write_text(json.dumps({"schema": 1, "jobs": [state]}))
    monkeypatch.setattr(contract, "call_app", lambda *a, **k: pytest.fail("must preserve edited evidence"))
    flow = MediaFlow(hub(tmp_path))
    flow.resume({"job_id": "fixture"})
    assert "changed" in flow.status({"job_id": "fixture", "wait_s": 5})["error"]
    assert path.read_text() == "reviewed by human"
    flow.close()


@pytest.mark.parametrize("state", ["failed", "error", "cancelled", "interrupted"])
def test_all_common_terminal_states_stop_polling(state):
    flow = MediaFlow(SimpleNamespace())
    with pytest.raises(RuntimeError, match=state):
        flow._finish_job("funes", "transcribe_status", "job_id", {"status": state, "job_id": "x"})


def test_corrupt_journal_is_preserved_and_not_replaced(tmp_path):
    data = tmp_path / "hub"
    data.mkdir()
    path = data / "media-imports.json"
    path.write_text("{bad")
    with pytest.raises(ValueError):
        MediaFlow(hub(tmp_path))
    assert path.read_text() == "{bad"


def test_repeated_download_event_reuses_paused_import_even_after_restart(tmp_path, monkeypatch):
    flow = MediaFlow(hub(tmp_path))
    monkeypatch.setattr(flow, "_launch", lambda job: None)
    arguments = {"download_id": "download", "folder": str(tmp_path), "language": "es"}
    first = flow.start(arguments)
    second = flow.start(arguments)
    assert second["existing"] and first["job_id"] == second["job_id"]
    flow.close()
    restored = MediaFlow(hub(tmp_path))
    third = restored.start(arguments)
    assert third["existing"] and third["job_id"] == first["job_id"] and third["status"] == "paused"
    restored.close()


def test_known_library_refusal_can_resume_without_inventing_collection_id(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"sample")
    flow = MediaFlow(hub(tmp_path))
    calls = []
    rejected = True

    def call(app, tool, args, **kwargs):
        calls.append(tool)
        if tool == "media_status":
            value = {"id": "download", "status": "done", "files": [{"path": str(audio)}]}
        elif tool == "transcribe_file":
            value = {"job_id": "speech", "status": "done", "text": "Saved evidence."}
        elif tool == "library_add_collection":
            if rejected:
                return {"ok": False, "status": 403, "error": "permission refused"}
            value = {"collection": {"id": 7}}
        else:
            value = {"queued": True}
        return {"ok": True, "result": value}

    monkeypatch.setattr(contract, "call_app", call)
    job = flow.start({"download_id": "download", "folder": str(tmp_path)})
    result = flow.status({"job_id": job["job_id"], "wait_s": 5})
    assert result["status"] == "failed" and result["pending"] == ""
    rejected = False
    flow.resume({"job_id": job["job_id"]})
    assert flow.status({"job_id": job["job_id"], "wait_s": 5})["status"] == "done"
    assert calls.count("transcribe_file") == 1
    assert calls.count("library_add_collection") == 2
    assert calls.count("library_reindex") == 1
    flow.close()
