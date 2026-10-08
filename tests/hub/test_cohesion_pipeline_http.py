"""Real loopback HTTP between Hub and three isolated provider stand-ins."""
from hoard_link.hub import tools
from ._hub_fakes import FakeApp, make_hub, serve, http


def test_download_transcript_library_share_auth_jobs_provenance_and_dedup(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"synthetic audio")
    handlers = {
        "links": {"media_download": lambda a: {"id": "d1", "status": "done", "files": [{"path": str(audio)}]},
                  "media_status": lambda a: {"id": "d1", "status": "done", "files": [{"path": str(audio)}]}},
        "funes": {"transcribe_file": lambda a: {"job_id": "t1", "status": "done", "text": "Synthetic citable speech."},
                  "transcribe_status": lambda a: {"job_id": "t1", "status": "done", "text": "Synthetic citable speech."}},
        "borges": {"library_add_collection": lambda a: {"collection": {"id": 7}},
                   "library_reindex": lambda a: {"queued": True}},
    }
    fakes = [FakeApp(owner, tools=[{"name": name} for name in functions], handlers=functions)
             for owner, functions in handlers.items()]
    hub = make_hub(tmp_path / "family", fakes)
    server = serve(hub)
    try:
        base = hub.config.url
        headers = {"Authorization": "Bearer " + hub.token}
        state = hub.facet("services").status()
        assert state["workflows"][0]["available"]
        status, started = http(base + "/api/agent/call", {"name": "hub_media_import", "arguments": {
            "url": "https://example.com/audio", "folder": str(tmp_path)}}, headers)
        assert status == 200
        job = started.get("result", started)
        done = tools.call(hub, "hub_media_import_status", {"job_id": job["job_id"], "wait_s": 5})
        assert done["status"] == "done" and done["stage"] == "index_requested"
        assert hub.facet("worktrack").wait_idle(5)
        work = hub.facet("worktrack").recent()
        assert any(j["job_id"] == job["job_id"] and j["status"] == "done" for j in work)
        assert [len(fake.calls) for fake in fakes] == [1, 1, 2]
        duplicate = tools.call(hub, "hub_media_import", {"download_id": "d1", "folder": str(tmp_path)})
        assert duplicate["existing"] and duplicate["job_id"] == job["job_id"]
        assert [len(fake.calls) for fake in fakes] == [1, 1, 2]
        code, view = http(base + "/api/services/imports", headers=headers)
        assert code == 200 and len(view["jobs"]) == 1
        assert all("arguments" not in j for j in view["jobs"])
        # A sibling cannot reconcile another workflow through operator routes.
        code, _ = http(base + "/api/services/imports", headers={"Authorization": "Bearer " + fakes[0].token})
        assert code == 403
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
        for fake in fakes:
            fake.stop()
