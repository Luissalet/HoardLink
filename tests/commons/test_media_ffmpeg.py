"""hoard_link.media.ffmpeg against the real ffmpeg: every clip is generated with ``-f lavfi`` (2 s of tone / colour) into tmp_path."""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

from hoard_link.errors import Unavailable
from hoard_link.media import bins
from hoard_link.media import ffmpeg as ff
from hoard_link.media.ffmpeg import FFmpeg, FFmpegError

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg / ffprobe not installed")


# -- pure helpers (no ffmpeg needed) ---------------------------------------------------------------------------

def test_seconds_and_fps_fraction():
    assert ff.seconds(12.5) == "12.500" and ff.seconds(-1) == "0.000"
    assert ff.fps_fraction(30) == "30" and ff.fps_fraction(29.97) == "30000/1001" and ff.fps_fraction(25.0) == "25"
    assert ff.fps_fraction(23.976) == "24000/1001" and ff.fps_fraction(59.94) == "60000/1001"


def test_filter_path_escapes_a_windows_path():
    assert ff.filter_path("C:\\Users\\me\\a b\\x.ass") == "'C\\:/Users/me/a b/x.ass'"
    assert ff.filter_path("/tmp/it's.ass") == "'/tmp/it\\'s.ass'"


def test_filter_text_escapes_drawtext_specials():
    assert ff.filter_text("50%: it's \\ok") == "50\\%\\: it'\\''s \\\\ok"


@pytest.mark.parametrize("factor, stages", [(1.0, 0), (1.0000001, 0), (1.5, 1), (2.0, 1), (3.0, 2), (4.0, 2), (9.0, 2), (0.5, 1), (0.3, 2), (0.1, 2)])
def test_atempo_chain_stays_in_range_and_multiplies_out(factor, stages):
    chain = ff.atempo_chain(factor)
    assert len(chain) == stages
    values = [float(c.split("=")[1]) for c in chain]
    assert all(0.5 - 1e-9 <= v <= 2.0 + 1e-9 for v in values)
    if chain:
        assert math.prod(values) == pytest.approx(max(0.25, min(4.0, factor)), rel=1e-5)


def test_needs_transcode_plays_everywhere_rule():
    ok = {"has_video": True, "video_codec": "h264", "pix_fmt": "yuv420p", "has_audio": True, "audio_codec": "aac"}
    assert not ff.needs_transcode(ok)
    assert not ff.needs_transcode({**ok, "audio_codec": "mp3"})
    assert ff.needs_transcode({**ok, "video_codec": "hevc"})
    assert ff.needs_transcode({**ok, "pix_fmt": "yuv420p10le"})
    assert ff.needs_transcode({**ok, "pix_fmt": "yuv444p"})
    assert ff.needs_transcode({**ok, "audio_codec": "opus"})
    assert not ff.needs_transcode({"has_audio": True, "audio_codec": "aac"})


def test_peaks_from_pcm():
    pcm = struct.pack("<8h", 0, 100, -200, 0, 32767, 0, 0, -32768)
    assert ff.peaks_from_pcm(pcm, 4) == pytest.approx([100 / 32768, 200 / 32768, 32767 / 32768, 1.0])
    assert ff.peaks_from_pcm(b"", 3) == [0.0, 0.0, 0.0]
    assert ff.peaks_from_pcm(pcm, 0) == []
    assert len(ff.peaks_from_pcm(pcm, 100)) == 8  # never more buckets than samples


def test_is_media_file():
    assert ff.is_media_file("a/B.MP4") and ff.is_media_file("x.flac") and not ff.is_media_file("x.txt")


def test_input_arg_never_becomes_an_option_or_a_protocol():
    assert ff._input_arg("-evil.mp4") == "./-evil.mp4"
    assert ff._input_arg("concat:a|b") == "file:concat:a|b"
    assert ff._input_arg("http://x/y") == "file:http://x/y"
    assert ff._input_arg("C:\\a.mp4") == "C:\\a.mp4"
    assert ff._input_arg("/tmp/a.mp4") == "/tmp/a.mp4"


def test_missing_ffmpeg_raises_unavailable(monkeypatch):
    monkeypatch.setattr(bins, "find", lambda name, **kw: bins.Tool(name))
    tool = FFmpeg()
    assert tool.available() is False
    with pytest.raises(Unavailable):
        tool.ffmpeg


def test_given_tools_are_used_without_discovery(monkeypatch):
    monkeypatch.setattr(bins, "find", lambda *a, **k: pytest.fail("discovery must not run"))
    tool = FFmpeg({"ffmpeg": "/x/ffmpeg", "ffprobe": ["/x/ffprobe", "-hide_banner"]})
    assert tool.ffmpeg == ["/x/ffmpeg"] and tool.ffprobe == ["/x/ffprobe", "-hide_banner"] and tool.available()


def test_summarize_rotation_and_kinds():
    info = {"format": {"duration": "3.5", "format_name": "mov,mp4", "bit_rate": "1000"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
                         "pix_fmt": "yuv420p", "tags": {"rotate": "90"}},
                        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
    s = FFmpeg.summarize(info)
    assert (s["kind"], s["width"], s["height"], s["rotation"]) == ("video", 1080, 1920, 90)
    assert s["fps"] == pytest.approx(29.97, abs=0.01) and s["duration_s"] == 3.5 and s["audio_codec"] == "aac" and s["has_audio"]
    img = FFmpeg.summarize({"format": {}, "streams": [{"codec_type": "video", "codec_name": "png", "width": 4, "height": 3}]})
    assert img["kind"] == "image"
    assert FFmpeg.summarize({"streams": [{"codec_type": "audio", "codec_name": "mp3"}], "format": {"duration": "1"}})["kind"] == "audio"
    assert FFmpeg.summarize({})["kind"] == "unknown"


# -- real ffmpeg -------------------------------------------------------------------------------------------

def gen(path: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], check=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100"]
    colour = ["-f", "lavfi", "-i", "color=c=blue:s=320x240:r=25:d=2"]
    out = {"dir": d}
    out["tone"] = gen(d / "tone.wav", *tone, "-ac", "1")
    out["tone_mp3"] = gen(d / "tone.mp3", *tone, "-c:a", "libmp3lame")
    out["video"] = gen(d / "clip.mp4", *colour, *tone, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    out["hevc10"] = gen(d / "hevc.mkv", *colour, *tone, "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-c:a", "libopus", "-shortest")
    out["silent"] = gen(d / "silent.wav", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2")
    out["image"] = gen(d / "pic.png", "-f", "lavfi", "-i", "color=c=red:s=64x48", "-frames:v", "1")
    return out


@pytest.fixture(scope="module")
def tool():
    return FFmpeg()


@needs_ffmpeg
def test_available_and_version(tool):
    assert tool.available() and tool.version and tool.ffmpeg and tool.ffprobe


@needs_ffmpeg
def test_probe_and_summarize_video(tool, media):
    info = tool.probe(media["video"])
    s = tool.summarize(info)
    assert s["kind"] == "video" and (s["width"], s["height"]) == (320, 240) and s["fps"] == 25
    assert s["video_codec"] == "h264" and s["audio_codec"] == "aac" and s["pix_fmt"] == "yuv420p" and s["sample_rate"] == 44100
    assert s["duration_s"] == pytest.approx(2.0, abs=0.15) and s["has_video"] and s["has_audio"]
    assert not ff.needs_transcode(s)


@needs_ffmpeg
def test_probe_audio_and_image(tool, media):
    a = tool.summarize(tool.probe(media["tone"]))
    assert a["kind"] == "audio" and a["channels"] == 1 and a["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert tool.summarize(tool.probe(media["image"]))["kind"] == "image"
    assert tool.duration(media["tone"]) == pytest.approx(2.0, abs=0.05)


@needs_ffmpeg
def test_safe_probe_reads_normal_files(tool, media):
    for key in ("video", "tone", "tone_mp3", "image", "hevc10"):
        s = tool.summarize(tool.probe(media[key], safe=True))
        assert s["kind"] != "unknown", key
    assert tool.summarize(tool.probe(media["video"], safe=True))["width"] == 320


@needs_ffmpeg
@pytest.mark.parametrize("bad", ["http://example.com/a.mp4", "concat:a.mp4|b.mp4", "\\\\server\\share\\a.mp4", "//server/share/a.mp4", "file:///etc/passwd"])
def test_safe_probe_refuses_urls_and_network_paths(tool, bad):
    with pytest.raises(FFmpegError) as e:
        tool.probe(bad, safe=True)
    assert e.value.code == "invalid_path"


@needs_ffmpeg
def test_safe_probe_refuses_missing_empty_and_directories(tool, tmp_path):
    with pytest.raises(FFmpegError) as e:
        tool.probe(tmp_path / "missing.mp4", safe=True)
    assert e.value.code == "invalid_path"
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(FFmpegError) as e:
        tool.probe(empty, safe=True)
    assert e.value.code == "invalid_path"
    with pytest.raises(FFmpegError) as e:
        tool.probe(tmp_path, safe=True)
    assert e.value.code == "invalid_path"


@needs_ffmpeg
def test_safe_probe_refuses_oversized_files(tool, media, monkeypatch):
    monkeypatch.setattr(ff, "MAX_MEDIA_BYTES", 10)
    with pytest.raises(FFmpegError) as e:
        tool.probe(media["video"], safe=True)
    assert e.value.code == "file_too_large"


@needs_ffmpeg
def test_safe_probe_does_not_follow_a_playlist_pretending_to_be_media(tool, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    evil = tmp_path / "evil.mp4"
    evil.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1,\nfile://{secret}\n")
    with pytest.raises(FFmpegError) as e:
        tool.probe(evil, safe=True)
    assert e.value.code == "invalid_media" and "TOP SECRET" not in str(e.value)


@needs_ffmpeg
def test_probe_garbage_is_invalid_media(tool, tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a video" * 50)
    with pytest.raises(FFmpegError) as e:
        tool.probe(junk)
    assert e.value.code == "invalid_media"
    with pytest.raises(FFmpegError):
        tool.duration(junk)


@needs_ffmpeg
def test_probe_option_looking_names_are_safe(tool, media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copy(media["tone"], tmp_path / "-weird.wav")
    assert tool.summarize(tool.probe("-weird.wav"))["kind"] == "audio"


@needs_ffmpeg
def test_run_reports_progress_and_returns_log(tool, media, tmp_path):
    seen: list[float] = []
    out = tmp_path / "o.wav"
    tool.run(["-i", media["tone"], "-ar", "8000", out], duration_s=2.0, progress=seen.append)
    assert out.exists() and seen and seen[-1] == 1.0 and seen == sorted(seen) and all(0 <= v <= 1 for v in seen)
    assert len(seen) == len(set(seen))  # no duplicate callbacks


@needs_ffmpeg
def test_run_failure_carries_the_log(tool, tmp_path):
    with pytest.raises(FFmpegError) as e:
        tool.run(["-i", tmp_path / "nope.wav", tmp_path / "o.wav"])
    assert e.value.returncode and "nope.wav" in e.value.stderr and e.value.code == "failed"


@needs_ffmpeg
def test_run_cancel_kills_ffmpeg(tool, tmp_path):
    cancel = threading.Event()
    threading.Timer(0.8, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(ff.Cancelled):
        tool.run(["-re", "-f", "lavfi", "-i", "sine=frequency=100:duration=600", "-f", "null", "-"], cancel=cancel)
    assert time.monotonic() - t0 < 20


@needs_ffmpeg
def test_run_timeout(tool):
    with pytest.raises(FFmpegError) as e:
        tool.run(["-re", "-f", "lavfi", "-i", "sine=frequency=100:duration=600", "-f", "null", "-"], timeout=1)
    assert e.value.code == "timeout"


@needs_ffmpeg
def test_extract_pcm(tool, media):
    pcm = tool.extract_pcm(media["tone"], rate=16000)
    assert len(pcm) == pytest.approx(2 * 16000 * 2, rel=0.02)
    part = tool.extract_pcm(media["tone"], rate=8000, start_s=0.5, duration_s=1.0)
    assert len(part) == pytest.approx(8000 * 2, rel=0.02)
    assert max(abs(v) for v in struct.unpack(f"<{len(pcm) // 2}h", pcm)) > 3000  # it is the tone, not silence


@needs_ffmpeg
def test_extract_pcm_missing_stream_fails(tool, media):
    with pytest.raises(FFmpegError):
        tool.extract_pcm(media["tone"], stream=3)


@needs_ffmpeg
def test_extract_frames_and_grab_frame(tool, media, tmp_path):
    data, w, h = tool.extract_frames(media["video"], width=64, fps=5)
    assert (w, h) == (64, 48) and len(data) == pytest.approx(w * h * 3 * 10, rel=0.15)
    assert tuple(data[:3]) == pytest.approx((0, 0, 255), abs=40)  # blue
    gray, gw, gh = tool.extract_frames(media["video"], width=32, fps=2, gray=True)
    assert len(gray) % (gw * gh) == 0 and len(gray) >= gw * gh
    png = tool.grab_frame(media["video"], 1.0, tmp_path / "f" / "frame.png", width=100)
    assert png.exists() and png.read_bytes()[:4] == b"\x89PNG"
    s = tool.summarize(tool.probe(png))
    assert s["width"] == 100 and s["height"] == 76
    jpg = tool.grab_frame(media["video"], 0.2, tmp_path / "frame.jpg")
    assert jpg.read_bytes()[:2] == b"\xff\xd8"


@needs_ffmpeg
def test_waveform_peaks(tool, media):
    peaks = tool.waveform_peaks(media["tone"], buckets=50)
    assert len(peaks) == 50 and all(0 <= p <= 1 for p in peaks) and 0.1 < max(peaks) < 0.2  # sine's default amplitude is 0.125
    quiet = tool.waveform_peaks(media["silent"], buckets=10)
    assert quiet == [0.0] * 10


@needs_ffmpeg
def test_loudness_ebur128(tool, media):
    m = tool.loudness(media["tone"])
    assert m["integrated_lufs"] is not None and -30 < m["integrated_lufs"] < 0
    assert m["lra"] is not None and m["true_peak_dbfs"] is not None and m["true_peak_dbfs"] <= 1
    silent = tool.loudness(media["silent"])
    assert silent["integrated_lufs"] is None or silent["integrated_lufs"] <= -69.9  # -inf is None; ebur128 floors at -70


@needs_ffmpeg
def test_loudnorm_two_pass(tool, media, tmp_path):
    measured = tool.loudnorm_measure(media["tone"])
    assert set(measured) == {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    assert all(math.isfinite(v) for v in measured.values())
    flt = FFmpeg.loudnorm_second_pass(measured, target_i=-16.0)
    assert flt.startswith("loudnorm=I=-16.0:TP=-1.5:LRA=11.0:measured_I=") and flt.endswith("linear=true:print_format=summary")
    out = tmp_path / "norm.wav"
    tool.run(["-i", media["tone"], "-af", flt, out])
    assert out.exists()


@needs_ffmpeg
def test_encoder_and_filter_lists(tool):
    assert tool.has_encoder("aac") and tool.has_encoder("libx264") and "libx264" in tool.encoders
    assert tool.has_filter("ebur128") and tool.has_filter("atempo") and not tool.has_filter("definitely_not_a_filter")


@needs_ffmpeg
def test_video_encoder_choice(tool):
    assert tool.video_encoder("x264") == "libx264" and tool.video_encoder("x264", "hevc") == "libx265"
    assert tool.video_encoder("auto") in ("libx264", "h264_nvenc")
    if not tool._nvenc_works():
        with pytest.raises(FFmpegError) as e:
            tool.video_encoder("nvenc")
        assert e.value.code == "nvenc_unavailable"


@needs_ffmpeg
def test_conversions(tool, media, tmp_path):
    w = tool.to_wav16k_mono(media["tone_mp3"], tmp_path / "a" / "x.wav")
    with wave.open(str(w)) as f:
        assert (f.getnchannels(), f.getframerate(), f.getsampwidth()) == (1, 16000, 2)
    seen: list[float] = []
    m = tool.to_mp3(media["tone"], tmp_path / "x.mp3", bitrate="96k", progress=seen.append)
    assert tool.summarize(tool.probe(m))["audio_codec"] == "mp3" and seen and seen[-1] == 1.0
    o = tool.to_ogg_opus(media["video"], tmp_path / "x.ogg")
    s = tool.summarize(tool.probe(o))
    assert s["audio_codec"] == "opus" and not s["has_video"]


@needs_ffmpeg
def test_ensure_playable_leaves_good_files_alone(tool, media, tmp_path):
    src = tmp_path / "ok.mp4"
    shutil.copy(media["video"], src)
    before = src.read_bytes()
    assert tool.ensure_playable(src) == src and src.read_bytes() == before
    assert tool.ensure_playable(media["tone"]) == media["tone"]  # not a video
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"nope")
    assert tool.ensure_playable(junk) == junk  # unreadable: untouched


@needs_ffmpeg
def test_ensure_playable_converts_hevc_10bit_opus(tool, media, tmp_path):
    src = tmp_path / "in.mkv"
    shutil.copy(media["hevc10"], src)
    assert ff.needs_transcode(tool.summarize(tool.probe(src)))
    seen: list[float] = []
    out = tool.ensure_playable(src, preset="ultrafast", progress=seen.append)
    assert out.suffix == ".mp4" and out.exists() and not src.exists()  # replaced
    s = tool.summarize(tool.probe(out))
    assert s["video_codec"] == "h264" and s["pix_fmt"] == "yuv420p" and s["audio_codec"] == "aac" and not ff.needs_transcode(s)
    assert seen and seen[-1] == 1.0 and not list(tmp_path.glob("*.hoard-tmp.*"))


@needs_ffmpeg
def test_ensure_playable_keeps_the_original_when_asked(tool, media, tmp_path):
    src = tmp_path / "in.mkv"
    shutil.copy(media["hevc10"], src)
    out = tool.ensure_playable(src, replace=False, preset="ultrafast")
    assert src.exists() and out.name == "in.h264.mp4" and tool.summarize(tool.probe(out))["video_codec"] == "h264"
    again = tool.ensure_playable(src, replace=False, preset="ultrafast")
    assert again != out and again.exists()  # never overwrites a previous result


@needs_ffmpeg
def test_ensure_playable_cancel_removes_the_partial_file(tool, tmp_path):
    src = tmp_path / "long.mkv"
    gen(src, "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=40", "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le")
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    with pytest.raises(ff.Cancelled):
        tool.ensure_playable(src, cancel=cancel, preset="veryslow")
    assert src.exists() and not list(tmp_path.glob("*.hoard-tmp.*"))


# -- WAV helpers ---------------------------------------------------------------------------------------------

def make_wav(rate=16000, channels=1, width=2, seconds_=0.5, value=1000) -> bytes:
    buf = io.BytesIO()
    n = int(rate * seconds_)
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes(struct.pack("<h", value) * (n * channels))
        else:
            w.writeframes(b"\x80" * (n * channels))
    return buf.getvalue()


def test_concat_wavs_same_format_needs_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(bins, "find", lambda *a, **k: pytest.fail("ffmpeg must not be needed"))
    data, dur = FFmpeg().concat_wavs([(make_wav(), 0.25), (make_wav(), 0.0), (make_wav(seconds_=0.25), 1.0)])
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 8000 + 4000 + 8000 + 4000 + 16000
    assert dur == pytest.approx(2.5)
    pcm = wave.open(io.BytesIO(data)).readframes(8000 + 4000)
    assert struct.unpack("<h", pcm[:2])[0] == 1000 and struct.unpack("<h", pcm[16000:16002])[0] == 0  # the pause is silence


def test_concat_wavs_empty_raises():
    with pytest.raises(FFmpegError):
        FFmpeg().concat_wavs([])


@needs_ffmpeg
def test_concat_wavs_converts_a_different_format(tool):
    data, dur = tool.concat_wavs([(make_wav(rate=16000), 0.0), (make_wav(rate=24000, channels=2, seconds_=0.5), 0.0)])
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == pytest.approx(16000, abs=400)
    assert dur == pytest.approx(1.0, abs=0.03)


@needs_ffmpeg
def test_convert_wav(tool):
    out = tool.convert_wav(make_wav(rate=22050), 2, 2, 44100)
    with wave.open(io.BytesIO(out)) as w:
        assert (w.getnchannels(), w.getframerate()) == (2, 44100)
