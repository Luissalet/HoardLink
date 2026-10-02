"""fam_media: download (Links), transcribe (Funes, else faster-whisper here), speak (Prospero, else Link.tts) — against a fake hub."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link import _famsvc, family, fam_media
from hoard_link.media import stt
from tests.commons.fam_hub import TOKEN, FakeHub, ToolError, sequence
from tests.hub.conftest import free_port


def configure(tmp_path, url, token=TOKEN):
    tf = tmp_path / "mcp-token"
    tf.write_text(token, encoding="utf-8")
    family.configure("lumiere", token_file=str(tf), hub=url)


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = dict(family._state)
    _famsvc.forget_availability()
    fam_media._reset_local()
    yield
    family._state.clear()
    family._state.update(saved)
    _famsvc.forget_availability()
    fam_media._reset_local()


@pytest.fixture
def hub(tmp_path):
    h = FakeHub()
    for app in ("links", "funes", "prospero"):
        h.app(app)
    configure(tmp_path, h.url)
    yield h
    h.close()


def done_view(**extra):
    return {"id": "m1", "ok": True, "status": "done", "kind": "video", "title": "T", "dir": "/dl",
            "files": [{"path": "/dl/a.mp4", "name": "a.mp4", "size": 10, "kind": "video"}], **extra}


# ---- download ---------------------------------------------------------------------------------------------------

def test_download_sends_every_argument_and_returns_the_path(hub, tmp_path):
    hub.on("links", "media_download", lambda a, c: done_view())
    res = fam_media.download("https://youtu.be/x", format="mp3", quality=720, dest_dir=str(tmp_path), sections=[[30, 75.5]], max_duration_s=600,
                             max_height=720, save_link=False, playlist=True, max_items=5, cookies="firefox")
    assert res["ok"] is True and res["path"] == "/dl/a.mp4" and res["via"] == "links" and res["media_kind"] == "video"
    assert "kind" not in res or res["kind"] != "video"
    (call,) = hub.of("media_download")
    a = call["args"]
    assert a["url"] == "https://youtu.be/x" and a["format"] == "audio" and a["quality"] == "720"
    assert a["dir"] == a["dest_dir"] == str(tmp_path)
    assert a["sections"] == [[30.0, 75.5]] and a["max_duration_s"] == 600 and a["max_height"] == 720
    assert a["save_link"] is False and a["playlist"] is True and a["max_items"] == 5 and a["cookies_browser"] == "firefox"
    assert a["wait"] is True and a["timeout_s"] == 150 and call["timeout_s"] == 170
    assert hub.of("media_status") == []


def test_download_accepts_one_section_given_as_a_pair(hub):
    hub.on("links", "media_download", lambda a, c: done_view())
    fam_media.download("https://x.test/v", sections=[10, 20])
    assert hub.of("media_download")[0]["args"]["sections"] == [[10.0, 20.0]]


@pytest.mark.parametrize("bad", [[[5, 5]], [[-1, 4]], [[1]], "abc", [["a", "b"]], [[1, float("inf")]]])
def test_download_refuses_bad_sections_without_calling_the_hub(hub, bad):
    res = fam_media.download("https://x.test/v", sections=bad)
    assert res["ok"] is False and res["kind"] == "client_error" and res["via"] == "links"
    assert hub.calls == []


def test_download_requires_a_url(hub):
    assert fam_media.download("  ")["error"] == "url is required"
    assert hub.calls == []


def test_download_polls_media_status_until_it_finishes(hub):
    running = {"id": "m1", "ok": False, "status": "downloading", "kind": "video", "progress": 40, "files": []}
    hub.on("links", "media_download", running)
    hub.on("links", "media_status", sequence(running, running, done_view()))
    res = fam_media.download("https://x.test/v", timeout_s=30)
    assert res["ok"] is True and res["path"] == "/dl/a.mp4"
    polls = hub.of("media_status")
    assert len(polls) == 3 and all(p["args"]["id"] == "m1" and "wait_s" in p["args"] for p in polls)


def test_download_past_its_deadline_reports_still_running_with_the_id(hub):
    running = {"id": "m9", "ok": False, "status": "processing", "kind": "video", "progress": 80, "files": []}
    hub.on("links", "media_download", running)
    hub.on("links", "media_status", running)
    t0 = time.monotonic()
    res = fam_media.download("https://x.test/v", timeout_s=1)
    assert time.monotonic() - t0 < 5
    assert res["ok"] is False and res["still_running"] is True and res["kind"] == "timeout" and res["id"] == "m9" and res["via"] == "links"
    assert res["progress"] == 80 and "still running" in res["error"]


def test_download_wait_false_returns_the_queued_download_at_once(hub):
    hub.on("links", "media_download", {"id": "m2", "ok": False, "status": "queued", "kind": "video", "files": []})
    res = fam_media.download("https://x.test/v", wait=False)
    assert res["id"] == "m2" and res["status"] == "queued" and res["still_running"] is True and res["ok"] is False
    assert hub.of("media_download")[0]["args"]["wait"] is False and hub.of("media_status") == []


def test_download_failed_keeps_links_error(hub):
    hub.on("links", "media_download", {"id": "m3", "ok": False, "status": "failed", "error": "Video unavailable", "files": []})
    res = fam_media.download("https://x.test/v")
    assert res["ok"] is False and res["error"] == "Video unavailable" and res["kind"] == "tool_error"


def test_download_done_without_files_is_not_ok(hub):
    hub.on("links", "media_download", {"id": "m4", "ok": True, "status": "done", "files": []})
    res = fam_media.download("https://x.test/v")
    assert res["ok"] is False and "without files" in res["error"]


def test_download_tool_error_from_links(hub):
    def refuse(a, c):
        raise ToolError(400, "El enlace no es válido")
    hub.on("links", "media_download", refuse)
    res = fam_media.download("https://x.test/v")
    assert res == {"ok": False, "error": "El enlace no es válido", "via": "links", "kind": "tool_error"}


def test_status_cancel_and_tools(hub):
    hub.on("links", "media_status", done_view(progress=100))
    hub.on("links", "media_cancel", {"id": "m1", "status": "cancelled", "error": "cancelled", "files": []})
    hub.on("links", "media_tools", {"ytdlp": {"found": True}, "update": None})
    st = fam_media.status("m1", wait_s=500)
    assert st["ok"] and hub.of("media_status")[0]["args"] == {"id": "m1", "wait_s": 150}
    assert fam_media.cancel("m1")["status"] == "cancelled"
    t = fam_media.tools(update=True)
    assert t["ok"] and t["ytdlp"]["found"] and hub.of("media_tools")[0]["args"] == {"update": True}


def test_info_uses_media_info_and_falls_back_to_media_probe(hub):
    hub.on("links", "media_info", {"title": "T", "description": "d", "uploader": "u", "duration": 61, "thumbnail": "http://t/x.jpg", "subtitle_langs": ["es"]})
    res = fam_media.info("https://x.test/v")
    assert res["ok"] and res["thumbnail"] and res["subtitle_langs"] == ["es"] and "partial" not in res
    del hub.apps["links"]["tools"]["media_info"]                                   # an older Links
    hub.on("links", "media_probe", {"title": "T", "duration": 61, "heights": [720], "uploader": "u"})
    res = fam_media.info("https://x.test/v")
    assert res["ok"] and res["partial"] is True and res["title"] == "T" and res["subtitle_langs"] == [] and res["thumbnail"] == ""


def test_subtitles_and_audio_for_asr(hub, tmp_path):
    hub.on("links", "media_subtitles", {"text": "hola", "lang": "es", "cues": [{"start_s": 0, "end_s": 1, "text": "hola"}]})
    res = fam_media.subtitles("https://x.test/v", langs=["es"])
    assert res["ok"] and res["text"] == "hola" and hub.of("media_subtitles")[0]["args"]["langs"] == ["es"]
    hub.on("links", "media_audio_for_asr", {"id": "a1", "ok": True, "status": "done", "kind": "audio", "files": [{"path": "/w/a.wav", "name": "a.wav", "size": 9, "kind": "audio"}]})
    res = fam_media.audio_for_asr("https://x.test/v", sections=[[0, 60]])
    assert res["ok"] and res["path"] == "/w/a.wav" and res["via"] == "links" and res["id"] == "a1"
    assert hub.of("media_audio_for_asr")[0]["args"] == {"url": "https://x.test/v", "sections": [[0.0, 60.0]], "wait": True, "timeout_s": 150}
    hub.on("links", "media_audio_for_asr", {"path": "/w/b.wav"})                    # an owner that answers just the path
    assert fam_media.audio_for_asr("https://x.test/v")["path"] == "/w/b.wav"
    hub.on("links", "media_audio_for_asr", {"id": "a2", "ok": False, "status": "processing", "files": []})
    hub.on("links", "media_status", {"id": "a2", "ok": True, "status": "done", "files": [{"path": "/w/c.wav"}]})
    slow = fam_media.audio_for_asr("https://x.test/v", timeout_s=30)                # a long video: followed with media_status
    assert slow["ok"] and slow["path"] == "/w/c.wav" and hub.of("media_status")[-1]["args"]["id"] == "a2"
    del hub.apps["links"]["tools"]["media_audio_for_asr"]                          # an older Links: the MP3 download instead
    hub.on("links", "media_download", done_view(files=[{"path": "/dl/a.mp3", "name": "a.mp3", "size": 5, "kind": "audio"}]))
    res = fam_media.audio_for_asr("https://x.test/v")
    assert res["ok"] and res["path"] == "/dl/a.mp3" and res["converted"] is False
    assert hub.of("media_download")[0]["args"]["format"] == "audio"


def test_subtitles_without_captions_is_an_error_answer(hub):
    def none(a, c):
        raise ToolError(404, "no subtitles in es, en")
    hub.on("links", "media_subtitles", none)
    res = fam_media.subtitles("https://x.test/v")
    assert res["ok"] is False and "no subtitles" in res["error"]


# ---- the error shapes ---------------------------------------------------------------------------------------------

def test_hub_down_says_hub_unreachable(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    t0 = time.monotonic()
    for res in (fam_media.download("https://x.test/v"), fam_media.status("m1"), fam_media.info("https://x.test/v"),
                fam_media.cancel("m1"), fam_media.tools()):
        assert res["ok"] is False and res["error"] == "hub unreachable" and res["via"] == "links" and res["kind"] == "hub_down"
    assert time.monotonic() - t0 < 5


def test_app_down_and_unknown_app_have_their_own_errors(hub):
    hub.set_state("links", "stopped")
    res = fam_media.download("https://x.test/v")
    assert res["ok"] is False and res["kind"] == "app_down" and res["error"] == "links unreachable" and res["via"] == "links"
    del hub.apps["links"]
    res = fam_media.download("https://x.test/v")
    assert res["kind"] == "app_missing" and "not registered" in res["error"]


def test_a_token_the_hub_refuses_is_reported_and_not_hidden_by_a_fallback(tmp_path, hub):
    configure(tmp_path, hub.url, token="wrong")
    res = fam_media.transcribe("/a.wav")
    assert res["ok"] is False and res["kind"] == "auth" and "wrong" not in str(res) and res["via"] == "funes"


def test_a_missing_tool_is_named(hub):
    res = fam_media.subtitles("https://x.test/v")
    assert res["ok"] is False and res["kind"] == "tool_missing" and "media_subtitles" in res["error"]


def test_a_call_that_runs_into_the_timeout_is_a_timeout(monkeypatch, hub):
    def stalled(app, tool, args, *, timeout):
        time.sleep(1.0)
        return {"ok": False, "app": app, "tool": tool, "status": None, "error": "hub not reachable at x"}
    monkeypatch.setattr(_famsvc._f, "call", stalled)
    res = _famsvc.call_tool("links", "media_info", {}, timeout_s=1)
    assert res["ok"] is False and res["kind"] == "timeout" and "timeout after 1s" in res["error"]


def test_the_hub_timeout_is_clamped_to_900_seconds(hub):
    hub.on("links", "media_status", done_view())
    _famsvc.call_tool("links", "media_status", {"id": "x"}, timeout_s=5000)
    assert hub.calls[-1]["timeout_s"] == 900


def test_nothing_raises_even_for_junk(hub):
    for res in (fam_media.download(None), fam_media.status(None), fam_media.transcribe(None), fam_media.speak(None)):
        assert res["ok"] is False and "error" in res


def test_available_follows_the_apps_state_and_is_cached(hub):
    assert fam_media.available("media") is True and fam_media.available("stt") is True
    hub.set_state("links", "stopped")
    assert fam_media.available("media") is True                      # cached for 30 s
    fam_media.forget_availability()
    assert fam_media.available("media") is False
    assert fam_media.available("nope") is False
    assert hub.gets.count("/api/apps/funes") == 1


# ---- transcribe ---------------------------------------------------------------------------------------------------

FUNES_DONE = {"job_id": "j1", "status": "done", "language": "es", "text": "hola mundo",
              "segments": [{"start": 0.0, "end": 1.5, "text": " hola ", "words": [{"start": 0.0, "end": 0.7, "word": "hola", "probability": 0.9}]},
                           {"start": 1.5, "end": 2.5, "text": "mundo"}], "model": "small"}


def test_transcribe_through_funes_normalises_the_transcript(hub, tmp_path):
    hub.on("funes", "transcribe_file", FUNES_DONE)
    seen = []
    res = fam_media.transcribe(tmp_path / "clase.mp3", language="es", model="small", initial_prompt="Luis", progress=seen.append)
    assert res["ok"] is True and res["via"] == "funes" and res["language"] == "es" and res["text"] == "hola mundo" and res["job_id"] == "j1"
    assert res["duration_s"] == 2.5 and res["model"] == "small" and res["stats"] == {} and res["device"] == ""
    seg = res["segments"][0]
    assert seg["start_s"] == 0.0 and seg["end_s"] == 1.5 and seg["text"] == "hola" and "start" not in seg
    assert seg["words"] == [{"start_s": 0.0, "end_s": 0.7, "word": "hola", "p": 0.9}]
    assert res["segments"][1]["words"] == []
    (call,) = hub.of("transcribe_file")
    a = call["args"]
    assert a["path"] == str(tmp_path / "clase.mp3") and a["language"] == "es" and a["model"] == "small" and a["initial_prompt"] == "Luis"
    assert a["word_timestamps"] is True and a["vad"] is True and a["wait_s"] == 150
    assert seen[-1] == 1.0


def test_transcribe_makes_the_path_absolute(hub):
    hub.on("funes", "transcribe_file", FUNES_DONE)
    fam_media.transcribe("rel/clase.mp3")
    assert Path(hub.of("transcribe_file")[0]["args"]["path"]).is_absolute()


def test_transcribe_polls_the_job_and_reports_progress(hub):
    hub.on("funes", "transcribe_file", {"job_id": "j2", "status": "running", "progress": 0.1})
    hub.on("funes", "transcribe_status", sequence({"job_id": "j2", "status": "running", "progress": 40}, {"job_id": "j2", "status": "running", "progress": 0.8},
                                                  {**FUNES_DONE, "job_id": "j2"}))
    seen = []
    res = fam_media.transcribe("/a.wav", timeout_s=60, progress=seen.append)
    assert res["ok"] and res["job_id"] == "j2"
    assert seen == [0.1, 0.4, 0.8, 1.0]
    polls = hub.of("transcribe_status")
    assert len(polls) == 3 and all(p["args"]["job_id"] == "j2" for p in polls)


def test_transcribe_still_running_at_the_deadline_gives_the_job_id_and_no_local_run(hub, monkeypatch):
    ran = []
    monkeypatch.setattr(stt, "available", lambda: ran.append(1) or True)
    hub.on("funes", "transcribe_file", {"job_id": "j3", "status": "running"})
    hub.on("funes", "transcribe_status", {"job_id": "j3", "status": "running"})
    res = fam_media.transcribe("/a.wav", timeout_s=1)
    assert res["ok"] is False and res["kind"] == "timeout" and res["job_id"] == "j3" and res["still_running"] is True and res["via"] == "funes"
    assert ran == []


def test_transcribe_status_and_cancel(hub):
    hub.on("funes", "transcribe_status", sequence({"job_id": "j4", "status": "running", "progress": 0.5}, {**FUNES_DONE, "job_id": "j4"},
                                                  {"job_id": "j5", "status": "error", "error": "no audio stream"}))
    hub.on("funes", "transcribe_cancel", {"job_id": "j4", "status": "cancelled"})
    first = fam_media.transcribe_status("j4", wait_s=999)
    assert first["ok"] is False and first["still_running"] is True and first["progress"] == 0.5
    assert hub.of("transcribe_status")[0]["args"]["wait_s"] == 150
    assert fam_media.transcribe_status("j4")["text"] == "hola mundo"
    failed = fam_media.transcribe_status("j5")
    assert failed["ok"] is False and failed["error"] == "no audio stream" and failed["kind"] == "tool_error"
    assert fam_media.transcribe_cancel("j4") == {"ok": True, "job_id": "j4", "status": "cancelled", "via": "funes"}


def test_a_failed_funes_job_is_reported_and_not_redone_locally(hub, monkeypatch):
    monkeypatch.setattr(stt, "available", lambda: pytest.fail("the local model must not run"))
    hub.on("funes", "transcribe_file", {"job_id": "j6", "status": "error", "error": "unsupported codec"})
    res = fam_media.transcribe("/a.wav")
    assert res["ok"] is False and res["error"] == "unsupported codec" and res["via"] == "funes" and res["job_id"] == "j6"


def test_a_tool_error_from_funes_is_not_redone_locally(hub, monkeypatch):
    monkeypatch.setattr(stt, "available", lambda: pytest.fail("the local model must not run"))

    def refuse(a, c):
        raise ToolError(400, "No such file: /a.wav")
    hub.on("funes", "transcribe_file", refuse)
    res = fam_media.transcribe("/a.wav")
    assert res == {"ok": False, "error": "No such file: /a.wav", "via": "funes", "kind": "tool_error"}


class FakeTranscriber:
    instances: list = []

    def __init__(self, size="small", **kw):
        self.size, self.calls, self.closed = size, [], False
        FakeTranscriber.instances.append(self)

    def transcribe(self, audio, *, language=None, word_timestamps=True, vad=True, initial_prompt="", progress=None, **kw):
        self.calls.append({"audio": audio, "language": language, "word_timestamps": word_timestamps, "vad": vad, "initial_prompt": initial_prompt})
        if progress:
            progress(0.5)
            progress(1.0)
        return stt.Transcript(language="es", duration_s=2.0, text="hola local", language_probability=0.97,
                              segments=[{"start_s": 0.0, "end_s": 2.0, "text": "hola local", "words": []}], model=self.size, device="cpu")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeTranscriber.instances.clear()
    monkeypatch.setattr(stt, "available", lambda: True)
    monkeypatch.setattr(stt, "Transcriber", FakeTranscriber)
    return FakeTranscriber


@pytest.mark.parametrize("why", ["hub_down", "app_down", "tool_missing"])
def test_transcribe_falls_back_to_the_local_model_when_funes_is_not_there(tmp_path, hub, fake_whisper, why):
    if why == "hub_down":
        configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    elif why == "app_down":
        hub.set_state("funes", "stopped")                                  # tool_missing: funes running with no transcribe_file
    seen = []
    res = fam_media.transcribe("/a.wav", language="auto", model="medium", word_timestamps=False, initial_prompt="x", progress=seen.append)
    assert res["ok"] is True and res["via"] == "local" and res["text"] == "hola local" and res["model"] == "medium" and res["device"] == "cpu"
    assert res["segments"][0]["end_s"] == 2.0 and res["language_probability"] == 0.97
    (t,) = fake_whisper.instances
    assert t.size == "medium" and t.calls == [{"audio": str(Path("/a.wav").resolve()), "language": None, "word_timestamps": False, "vad": True, "initial_prompt": "x"}]
    assert seen == [0.5, 1.0]
    fam_media.transcribe("/b.wav", model="medium", language="en")
    assert len(fake_whisper.instances) == 1 and t.calls[-1]["language"] == "en"          # the model stays loaded


def test_without_the_local_fallback_the_hub_error_stands(tmp_path, fake_whisper):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    res = fam_media.transcribe("/a.wav", local_fallback=False)
    assert res == {"ok": False, "error": "hub unreachable", "via": "funes", "kind": "hub_down"}
    assert fake_whisper.instances == []


def test_when_nothing_can_transcribe_both_reasons_are_given(tmp_path, monkeypatch):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(stt, "available", lambda: False)
    res = fam_media.transcribe("/a.wav")
    assert res["ok"] is False and res["via"] == "local" and res["kind"] == "hub_down" and res["hub_error"] == "hub unreachable"
    assert "hub unreachable" in res["error"] and "faster_whisper" in res["error"]


def test_a_local_model_that_crashes_does_not_raise(tmp_path, monkeypatch, fake_whisper):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")

    def boom(self, audio, **kw):
        raise RuntimeError("CUDA out of memory")
    monkeypatch.setattr(FakeTranscriber, "transcribe", boom)
    res = fam_media.transcribe("/a.wav")
    assert res["ok"] is False and "CUDA out of memory" in res["error"] and res["via"] == "local"


# ---- speak --------------------------------------------------------------------------------------------------------

def test_speak_through_prospero(hub, tmp_path):
    wav = tmp_path / "t.wav"
    wav.write_bytes(b"RIFFxxxxWAVE" + b"0" * 20)
    hub.on("prospero", "voice_tts", {"ok": True, "path": str(wav), "engine_id": "piper", "bytes": 32})
    res = fam_media.speak("  Hola mundo ", voice="es_ES-davefx-medium", engine="piper", lang="es", speed=1.1)
    assert res == {"ok": True, "path": str(wav), "bytes": 32, "engine_id": "piper", "via": "prospero"}
    assert hub.of("voice_tts")[0]["args"] == {"text": "Hola mundo", "voice": "es_ES-davefx-medium", "engine": "piper", "lang": "es", "speed": 1.1}
    only_text = fam_media.speak("Hola")
    assert hub.of("voice_tts")[1]["args"] == {"text": "Hola"} and only_text["ok"]


def test_speak_reads_the_size_from_the_file_when_prospero_omits_it(hub, tmp_path):
    wav = tmp_path / "t.wav"
    wav.write_bytes(b"x" * 77)
    hub.on("prospero", "voice_tts", {"ok": True, "path": str(wav), "engine_id": "piper"})
    assert fam_media.speak("hola")["bytes"] == 77


def test_speak_bytes_returns_the_audio(hub, tmp_path):
    wav = tmp_path / "t.wav"
    wav.write_bytes(b"RIFFdata")
    hub.on("prospero", "voice_tts", {"ok": True, "path": str(wav), "engine_id": "piper", "bytes": 8})
    res = fam_media.speak_bytes("hola")
    assert res["ok"] and res["data"] == b"RIFFdata" and res["bytes"] == 8 and res["via"] == "prospero"


def test_speak_refuses_empty_and_oversized_text_without_calling_the_hub(hub):
    assert fam_media.speak("   ")["error"] == "empty text"
    res = fam_media.speak("a" * 20001)
    assert res["ok"] is False and "too long" in res["error"] and hub.calls == []


def test_a_tts_error_from_prospero_is_not_hidden_by_the_fallback(hub):
    def refuse(a, c):
        raise ToolError(400, "tts_not_installed: no engine")
    hub.on("prospero", "voice_tts", refuse)
    link = SimpleNamespace(sync=SimpleNamespace(tts=lambda *a: pytest.fail("fallback must not run")))
    res = fam_media.speak("hola", link=link)
    assert res["ok"] is False and res["via"] == "prospero" and res["kind"] == "tool_error" and "tts_not_installed" in res["error"]


class FakeLink:
    def __init__(self, audio=b"RIFF....WAVEdata"):
        self.audio, self.calls = audio, []
        self.sync = SimpleNamespace(tts=self._tts)

    def _tts(self, text, voice=None):
        self.calls.append((text, voice))
        if isinstance(self.audio, Exception):
            raise self.audio
        return self.audio


def test_speak_falls_back_to_link_tts_and_writes_a_file(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    link = FakeLink()
    res = fam_media.speak("Hola mundo", voice="es", lang="es", link=link, out_dir=str(tmp_path / "out"))
    assert res["ok"] is True and res["via"] == "local" and res["engine_id"] == "link" and res["bytes"] == 16 and res["temp"] is False
    assert Path(res["path"]).read_bytes() == b"RIFF....WAVEdata" and Path(res["path"]).suffix == ".wav" and Path(res["path"]).parent == tmp_path / "out"
    assert link.calls == [("Hola mundo", "es")]
    mp3 = fam_media.speak("x", link=FakeLink(b"ID3\x04"), out_dir=str(tmp_path / "out"))
    assert Path(mp3["path"]).suffix == ".mp3"


def test_speak_bytes_removes_the_temporary_fallback_file(tmp_path, monkeypatch):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    res = fam_media.speak_bytes("hola", link=FakeLink())
    assert res["ok"] and res["data"] == b"RIFF....WAVEdata" and "temp" not in res and res["via"] == "local"
    assert not Path(res["path"]).exists() and Path(res["path"]).parent == tmp_path / "home" / "tmp" / "tts"


def test_speak_when_the_fallback_fails_says_why(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    res = fam_media.speak("hola", link=FakeLink(RuntimeError("no voice")))
    assert res["ok"] is False and res["via"] == "local" and "hub unreachable" in res["error"] and "no voice" in res["error"]
    res = fam_media.speak("hola", link=FakeLink(), local_fallback=False)
    assert res == {"ok": False, "error": "hub unreachable", "via": "prospero", "kind": "hub_down"}
