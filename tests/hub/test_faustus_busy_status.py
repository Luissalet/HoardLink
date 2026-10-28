"""A Faustus that holds its port but misses the health deadline is busy, never offered a second start."""
from hoard_link.hub import core, procs
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub


def _hub(tmp_path):
    cfg = HubConfig(port=0, data_dir=str(tmp_path / "data"), roots=[], icon_dirs=[], jobs_enabled=False,
                    faustus_urls=["http://127.0.0.1:7999"], faustus_dir=str(tmp_path / "none"),
                    repos={"roots": [], "extra": [], "ci": False})
    return Hub(cfg)


def test_listening_but_silent_faustus_is_busy(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    try:
        monkeypatch.setattr(procs, "fetch_json", lambda *a, **k: (None, None))
        monkeypatch.setattr(procs, "listening_pids", lambda: {7999: 4242})
        monkeypatch.setattr(hub.faustus_runtime, "available", lambda: True)
        st = hub._refresh_faustus()
        assert st["reachable"] is False and st["listening"] is True and st["pid"] == 4242
        assert st["startable"] is False and st["stoppable"] is True
        monkeypatch.setattr(procs, "listening_pids", lambda: {})
        st = hub._refresh_faustus()
        assert not st.get("listening") and st["startable"] is True
    finally:
        hub.close()
