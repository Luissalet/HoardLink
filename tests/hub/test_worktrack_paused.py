from hoard_link.hub.worktrack import WorkFacet
from ._hub_fakes import make_hub, RecordingNotify


def test_paused_import_survives_stale_sweep_and_resume_without_failure_notice(tmp_path):
    hub = make_hub(tmp_path)
    try:
        now = [100.0]
        work = WorkFacet(hub, now=lambda: now[0])
        try:
            notify = RecordingNotify()
            hub._facets_by_id["notify"] = notify
            def event(state):
                return {"type": "hub.job." + state, "ts": now[0], "data": {"job_id": "j", "title": "Import", "error": "restart"}}
            work.handle_event(event("started"))
            work.handle_event(event("paused"))
            now[0] += 10 * 86400
            paused = work.active()
            assert paused[0]["status"] == "paused" and not paused[0]["stale"]
            assert not work.recent() and not notify.sent
            work.handle_event(event("started"))
            assert work.active()[0]["status"] == "running" and not work.active()[0]["error"]
            work.handle_event(event("done"))
            assert not work.active() and work.recent()[0]["status"] == "done"
        finally:
            work.close()
    finally:
        hub.close()
