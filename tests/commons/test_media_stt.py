"""hoard_link.media.stt with a fake model: no model download, no GPU, no faster-whisper needed."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace as NS

import pytest

from hoard_link.errors import Unavailable
from hoard_link.media import stt
from hoard_link.proc import Cancelled


def seg(start, end, text, *, nsp=0.01, lp=-0.2, cr=1.2, words=None):
    return NS(start=start, end=end, text=text, no_speech_prob=nsp, avg_logprob=lp, compression_ratio=cr,
              words=words if words is not None else [NS(start=start, end=end, word=text, probability=0.9)])


class FakeModel:
    def __init__(self, segments, *, language="es", duration=10.0, fail_on=None, device="cpu"):
        self.segments, self.language, self.duration, self.fail_on, self.device = segments, language, duration, fail_on, device
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append({"audio": audio, **kw})
        info = NS(language=self.language, language_probability=0.97, duration=self.duration)
        fail_on = self.fail_on

        def gen():
            for i, s in enumerate(self.segments):
                if fail_on is not None and i == fail_on[0]:
                    raise RuntimeError(fail_on[1])
                yield s

        return gen(), info


class Factory:
    """model_factory(size, device, compute, download_root); records every load and decides what each device returns."""

    def __init__(self, models):
        self.models = models  # device -> FakeModel | Exception
        self.loads: list[tuple] = []

    def __call__(self, size, device, compute, download_root):
        self.loads.append((size, device, compute, download_root))
        m = self.models[device]
        if isinstance(m, Exception):
            raise m
        return m


class FakeLease:
    def __init__(self, behaviour="grant"):
        self.behaviour, self.acquired, self.released, self.args = behaviour, 0, 0, None

    def factory(self, vram_mb, purpose, owner, timeout_s):
        self.args = (vram_mb, purpose, owner, timeout_s)
        return self

    def acquire(self):
        if self.behaviour == "timeout":
            raise TimeoutError("queued for 120 s")
        self.acquired += 1
        return self

    def release(self):
        self.released += 1


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    for var in ("HOARD_GPU_LEASE", "SCRIBE_GPU_LEASE", "HOARD_WHISPER_VRAM_MB", "SCRIBE_WHISPER_VRAM_MB", "HOARD_LEASE_TIMEOUT_S", "SCRIBE_LEASE_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stt, "cuda_dll_dirs", lambda **k: [])


GOOD = [seg(0.0, 2.0, "Hola a todos."), seg(2.0, 4.5, "Hoy hablamos de FFmpeg."), seg(4.5, 7.0, "Gracias.")]


# -- the happy path ----------------------------------------------------------------------------------------

def test_cpu_transcription_end_to_end(tmp_path):
    model = FakeModel(GOOD)
    factory = Factory({"cpu": model})
    t = stt.Transcriber(tmp_path / "models", size="small", device="cpu", model_factory=factory)
    assert t.state == "idle" and t.model is None  # lazy
    progress: list[float] = []
    r = t.transcribe("clip.wav", language="es", progress=progress.append, initial_prompt="FFmpeg")
    assert r.text == "Hola a todos. Hoy hablamos de FFmpeg. Gracias." and r.language == "es" and r.duration_s == 10.0
    assert r.device == "cpu" and r.model == "small" and r.language_probability == 0.97
    assert [s["text"] for s in r.segments] == ["Hola a todos.", "Hoy hablamos de FFmpeg.", "Gracias."]
    assert r.segments[0]["words"] == [{"start_s": 0.0, "end_s": 2.0, "word": "Hola a todos.", "p": 0.9}]
    assert r.stats["kept"] == 3 and r.stats["dropped"] == 0
    assert progress[-1] == 1.0 and progress == sorted(progress) and all(0 <= p <= 1 for p in progress)
    call = model.calls[0]
    assert call["audio"] == "clip.wav" and call["language"] == "es" and call["initial_prompt"] == "FFmpeg" and call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False and call["word_timestamps"] is True
    assert factory.loads == [("small", "cpu", "int8", str(tmp_path / "models"))]
    assert t.state == "ready" and t.last_ms is not None
    assert r.as_dict()["segments"] == r.segments


def test_language_auto_and_options():
    model = FakeModel(GOOD)
    t = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": model}))
    t.transcribe("a.wav", language="auto", vad=False, word_timestamps=False, beam_size=1)
    c = model.calls[0]
    assert c["language"] is None and c["vad_filter"] is False and c["vad_parameters"] is None and c["beam_size"] == 1 and c["word_timestamps"] is False


def test_model_is_loaded_once_and_reloaded_when_settings_change(tmp_path):
    factory = Factory({"cpu": FakeModel(GOOD)})
    t = stt.Transcriber(tmp_path, size="tiny", device="cpu", model_factory=factory)
    t.transcribe("a.wav")
    t.transcribe("b.wav")
    assert len(factory.loads) == 1
    t.reconfigure(size="base")
    assert t.state == "idle" and t.model is None
    t.transcribe("c.wav")
    assert [l[0] for l in factory.loads] == ["tiny", "base"]
    t.reconfigure(size="base")  # no change: stays loaded
    assert t.model is not None
    t.close()
    assert t.model is None and t.state == "idle"


def test_info_and_download_state(tmp_path):
    models = tmp_path / "models"
    t = stt.Transcriber(models, size="small", device="cpu", model_factory=Factory({"cpu": FakeModel(GOOD)}))
    info = t.info()
    assert info["download"] == "missing" and info["loaded"] is False and info["model"] == "small" and info["device"] == "cpu"
    snap = models / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert stt.model_present(models, "small") and not stt.model_present(models, "tiny")
    t.transcribe("a.wav")
    info = t.info()
    assert info["download"] == "ready" and info["loaded"] is True and info["state"] == "ready" and info["gpu_lease"] == "disabled"


def test_state_while_loading_is_downloading_when_files_are_missing(tmp_path):
    seen = []

    def factory(size, device, compute, root):
        seen.append(t.state)
        return FakeModel(GOOD)

    t = stt.Transcriber(tmp_path, device="cpu", model_factory=factory)
    t.transcribe("a.wav")
    assert seen == ["downloading"] and t.state == "ready"


def test_model_repo_names():
    assert stt.model_repo("small") == "Systran/faster-whisper-small"
    assert stt.model_repo("distil-large-v3") == "Systran/distil-whisper-large-v3"


# -- samples -------------------------------------------------------------------------------------------------

def test_sample_arrays_are_converted_to_float32():
    np = pytest.importorskip("numpy")
    model = FakeModel(GOOD)
    t = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": model}))
    t.transcribe(np.array([0, 16384, -32768], dtype=np.int16))
    audio = model.calls[0]["audio"]
    assert audio.dtype == np.float32 and list(audio) == [0.0, 0.5, -1.0]
    t.transcribe(np.array([0.25], dtype=np.float64))
    assert model.calls[1]["audio"].dtype == np.float32


def test_empty_sample_array_returns_an_empty_transcript_without_loading():
    np = pytest.importorskip("numpy")
    factory = Factory({"cpu": FakeModel(GOOD)})
    r = stt.Transcriber("m", device="cpu", model_factory=factory).transcribe(np.array([], dtype=np.int16), language="es")
    assert r.text == "" and r.segments == [] and r.language == "es" and factory.loads == []


# -- cleaning ------------------------------------------------------------------------------------------------

def test_hallucinations_and_doubtful_segments_are_removed():
    segs = [
        seg(0, 2, "Hola, esto es real."),
        seg(2, 4, "Subtítulos realizados por la comunidad de Amara.org"),
        seg(4, 6, "Thanks for watching!"),
        seg(6, 8, "algo dudoso", nsp=0.9),
        seg(8, 10, "otra cosa dudosa", lp=-1.8),
        seg(10, 12, "bla bla bla", cr=3.1),
        seg(12, 14, "Seguimos con lo importante."),
    ]
    r = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": FakeModel(segs)})).transcribe("a.wav")
    assert [s["text"] for s in r.segments] == ["Hola, esto es real.", "Seguimos con lo importante."]
    st = r.stats
    assert (st["dropped_hallucination"], st["dropped_no_speech"], st["dropped_logprob"], st["dropped_compression"]) == (2, 1, 1, 1)
    assert st["segments_in"] == 7 and st["kept"] == 2 and st["dropped"] == 5


def test_clean_false_keeps_everything():
    segs = [seg(0, 2, "Thanks for watching!"), seg(2, 4, "real")]
    r = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": FakeModel(segs)})).transcribe("a.wav", clean=False)
    assert len(r.segments) == 2 and r.stats == {}


def test_extra_phrases():
    segs = [seg(0, 2, "Visita nuestra web"), seg(2, 4, "contenido")]
    r = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": FakeModel(segs)})).transcribe("a.wav", extra_phrases=["visita nuestra web"])
    assert [s["text"] for s in r.segments] == ["contenido"]


def test_clean_segments_collapses_loops_and_duplicates():
    out = stt.clean_segments([
        {"start_s": 0, "end_s": 1, "text": "the the the the the the", "words": [{"word": "the"}]},
        {"start_s": 1, "end_s": 2, "text": "Hello there everyone"},
        {"start_s": 2, "end_s": 3, "text": "Hello there everyone."},
        {"start_s": 3, "end_s": 4, "text": "Hello there everyone"},
        {"start_s": 4, "end_s": 5, "text": "Something else"},
    ])
    assert [s["text"] for s in out] == ["the", "Hello there everyone", "Something else"]
    assert out[0]["words"] == [] and out[1]["end_s"] == 4


def test_clean_segments_does_not_mutate_input_and_keeps_stats():
    src = [{"start_s": 0, "end_s": 1, "text": "a b a b a b a b a b"}]
    st = stt.CleanStats()
    out = stt.clean_segments(src, stats=st)
    assert src[0]["text"] == "a b a b a b a b a b" and out[0]["text"] == "a b" and st.loops_collapsed == 4


def test_weak_one_word_segments():
    assert stt.is_hallucination("you") and not stt.is_hallucination("you", deny_weak=False)
    assert not stt.is_hallucination("you know what I mean")
    assert stt.is_hallucination("[Música]") and stt.is_hallucination("♪♪") and stt.is_hallucination("   ")
    assert stt.is_hallucination("Subtitles by the Amara.org community") and stt.is_hallucination("Suscríbete!")
    assert not stt.is_hallucination("Please subscribe to the mailing list before Friday")


def test_collapse_ngram_loops():
    assert stt.collapse_ngram_loops("no no no") == ("no no no", 0)  # three is natural speech
    assert stt.collapse_ngram_loops("ok so so so so so ok") == ("ok so ok", 4)
    assert stt.collapse_ngram_loops("la casa la casa la casa la casa roja") == ("la casa roja", 3)
    assert stt.collapse_ngram_loops("") == ("", 0)


def test_normalize():
    assert stt.normalize("¡Suscríbete,  YA!") == "suscribete ya"


# -- cancellation --------------------------------------------------------------------------------------------

def test_cancel_raises_with_the_partial_segments():
    cancel = threading.Event()
    segs = [seg(0, 2, "uno"), seg(2, 4, "dos"), seg(4, 6, "tres")]
    t = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": FakeModel(segs)}))

    def progress(fraction):
        if fraction >= 0.15:
            cancel.set()

    with pytest.raises(Cancelled) as e:
        t.transcribe("a.wav", progress=progress, cancel=cancel)
    assert [s["text"] for s in e.value.partial] == ["uno"]  # 2 s of 10 s was reported, then the flag was seen
    assert t.state == "ready"  # the model stays usable
    assert t.transcribe("a.wav").text.startswith("uno")


def test_cancel_before_start():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": FakeModel(GOOD)})).transcribe("a.wav", cancel=cancel)


# -- GPU lease and CPU fallbacks --------------------------------------------------------------------------------

def test_cuda_with_a_granted_lease_releases_after_the_first_inference():
    lease = FakeLease()
    factory = Factory({"cuda": FakeModel(GOOD)})
    t = stt.Transcriber("m", size="medium", device="cuda", model_factory=factory, lease_factory=lease.factory, owner="scribe")
    r = t.transcribe("a.wav")
    assert r.device == "cuda" and factory.loads[0][1:3] == ("cuda", "float16")
    assert lease.args[:3] == (5120, "whisper medium", "scribe") and lease.args[3] == stt.DEFAULT_LEASE_TIMEOUT_S
    assert lease.acquired == 1 and lease.released == 1 and t.lease_state == "granted"
    assert t.info()["gpu_lease"] == "granted"


def test_lease_timeout_falls_back_to_the_cpu_and_keeps_the_model(caplog):
    lease = FakeLease("timeout")
    factory = Factory({"cuda": RuntimeError("must not load on cuda"), "cpu": FakeModel(GOOD)})
    t = stt.Transcriber("m", device="cuda", model_factory=factory, lease_factory=lease.factory)
    r = t.transcribe("a.wav")
    assert r.device == "cpu" and r.text and t.lease_state == "fallback_cpu" and "lease" in r.note.lower()
    assert [l[1] for l in factory.loads] == ["cpu"] and factory.loads[0][2] == "int8"
    t.transcribe("b.wav")
    assert len(factory.loads) == 1  # no reload (and no new 120 s wait) for every file
    assert t.info()["device_used"] == "cpu"


def test_cuda_load_failure_falls_back_to_the_cpu():
    factory = Factory({"cuda": RuntimeError("CUDA out of memory"), "cpu": FakeModel(GOOD)})
    t = stt.Transcriber("m", device="cuda", lease=False, model_factory=factory)
    r = t.transcribe("a.wav")
    assert [l[1] for l in factory.loads] == ["cuda", "cpu"] and r.device == "cpu" and "out of memory" in r.note
    assert t.state == "ready"


def test_load_failure_on_the_cpu_is_an_error_state():
    factory = Factory({"cpu": OSError("model.bin is corrupt")})
    t = stt.Transcriber("m", device="cpu", model_factory=factory)
    with pytest.raises(OSError):
        t.transcribe("a.wav")
    assert t.state == "error" and "corrupt" in t.error and t.model is None


def test_cuda_error_in_the_middle_of_an_inference_reloads_on_the_cpu():
    lease = FakeLease()
    broken = FakeModel(GOOD, fail_on=(1, "Library cublas64_12.dll is not found or cannot be loaded"))
    factory = Factory({"cuda": broken, "cpu": FakeModel(GOOD)})
    t = stt.Transcriber("m", device="cuda", model_factory=factory, lease_factory=lease.factory)
    r = t.transcribe("a.wav")
    assert r.device == "cpu" and len(r.segments) == 3 and "cublas" in r.note and "CPU" in r.note
    assert [l[1] for l in factory.loads] == ["cuda", "cpu"] and lease.released == 1
    assert t.device_setting == "cpu"
    r2 = t.transcribe("b.wav")  # stays on the CPU, no new load
    assert r2.device == "cpu" and len(factory.loads) == 2


def test_other_inference_errors_are_not_swallowed():
    model = FakeModel(GOOD, fail_on=(0, "tensor shape mismatch"))
    t = stt.Transcriber("m", device="cpu", model_factory=Factory({"cpu": model}))
    with pytest.raises(RuntimeError, match="shape mismatch"):
        t.transcribe("a.wav")


def test_non_cuda_error_on_the_gpu_is_raised_and_the_lease_released():
    lease = FakeLease()
    model = FakeModel(GOOD, fail_on=(0, "tensor shape mismatch"))
    t = stt.Transcriber("m", device="cuda", model_factory=Factory({"cuda": model}), lease_factory=lease.factory)
    with pytest.raises(RuntimeError):
        t.transcribe("a.wav")
    assert lease.released == 1


def test_lease_can_be_turned_off(monkeypatch):
    lease = FakeLease()
    t = stt.Transcriber("m", device="cuda", lease=False, model_factory=Factory({"cuda": FakeModel(GOOD)}), lease_factory=lease.factory)
    t.transcribe("a.wav")
    assert lease.acquired == 0
    monkeypatch.setenv("HOARD_GPU_LEASE", "0")
    lease2 = FakeLease()
    stt.Transcriber("m", device="cuda", model_factory=Factory({"cuda": FakeModel(GOOD)}), lease_factory=lease2.factory).transcribe("a.wav")
    assert lease2.acquired == 0


def test_lease_environment(monkeypatch):
    assert stt.whisper_vram_mb("small") == 2048 and stt.whisper_vram_mb("distil-large-v3") == 6144 and stt.whisper_vram_mb("unheard-of") == 2048
    monkeypatch.setenv("HOARD_WHISPER_VRAM_MB", "3000")
    assert stt.whisper_vram_mb("small") == 3000
    monkeypatch.setenv("HOARD_WHISPER_VRAM_MB", "lots")
    assert stt.whisper_vram_mb("small") == 2048
    monkeypatch.setenv("HOARD_LEASE_TIMEOUT_S", "7.5")
    assert stt.lease_timeout_s() == 7.5
    monkeypatch.setenv("HOARD_LEASE_TIMEOUT_S", "soon")
    assert stt.lease_timeout_s() == stt.DEFAULT_LEASE_TIMEOUT_S
    monkeypatch.delenv("HOARD_LEASE_TIMEOUT_S")
    monkeypatch.setenv("SCRIBE_LEASE_TIMEOUT_S", "9")  # the old name still works
    assert stt.lease_timeout_s() == 9.0


def test_reconfigure_releases_a_held_lease():
    lease = FakeLease()
    t = stt.Transcriber("m", device="cuda", model_factory=Factory({"cuda": FakeModel(GOOD)}), lease_factory=lease.factory)
    t.ensure_loaded()
    assert lease.acquired == 1 and lease.released == 0  # held until the first inference is done
    t.reconfigure(size="tiny")
    assert lease.released == 1 and t.model is None


# -- missing faster-whisper / CUDA DLLs ---------------------------------------------------------------------------

def test_missing_faster_whisper_is_a_clear_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    t = stt.Transcriber("m", device="cpu")
    with pytest.raises(Unavailable) as e:
        t.transcribe("a.wav")
    assert "faster" in str(e.value).lower() and t.state == "error"


def test_importing_stt_needs_no_heavy_dependency():
    import subprocess

    code = "import sys; sys.modules['faster_whisper']=None; sys.modules['numpy']=None; sys.modules['ctranslate2']=None; import hoard_link.media.stt as s; print(s.available.__name__)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_cuda_dll_dirs_registers_the_pip_wheel_folders(tmp_path, monkeypatch):
    monkeypatch.undo()  # undo the autouse stub of cuda_dll_dirs
    site = tmp_path / "site-packages"
    for lib in ("cublas", "cudnn"):
        (site / "nvidia" / lib / "bin").mkdir(parents=True)
    (site / "nvidia" / "empty").mkdir(parents=True)
    added: list[str] = []
    monkeypatch.setattr(stt.os, "add_dll_directory", lambda p: added.append(p), raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    found = stt.cuda_dll_dirs(roots=[site], platform="win32")
    assert [f.split("nvidia")[1].strip("/\\").split("/")[0].split("\\")[0] for f in found] == ["cublas", "cudnn"]
    assert added == found and stt.os.environ["PATH"].startswith(found[0])
    assert stt.cuda_dll_dirs(roots=[site], platform="linux") == []  # not Windows: nothing to do
    assert stt.cuda_dll_dirs(roots=[tmp_path / "nowhere"], platform="win32") == []
