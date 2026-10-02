"""js/hoard-commons/fam-services.js against the same fake hub the Python clients are tested with (skipped without node)."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.commons.fam_hub import TOKEN, FakeHub, ToolError, sequence
from tests.commons.jsrun import JS_DIR, node
from tests.hub.conftest import free_port

ROOT = Path(__file__).resolve().parents[2]
FAM_URI = (JS_DIR / "fam-services.js").resolve().as_uri()
LINK_URI = (ROOT / "js" / "hoard-link.js").resolve().as_uri()


def run(hub_url: str, token_file: Path, body: str, timeout: float = 90.0):
    """Run `body` (an async function body using `fam` and returning a JSON value) in node with the app configured for `hub_url`."""
    exe = node()
    script = (
        f"import * as fam from {json.dumps(FAM_URI)};\nimport * as link from {json.dumps(LINK_URI)};\n"
        f"link.configure({{ app: 'cook', tokenFile: {json.dumps(str(token_file))}, hub: {json.dumps(hub_url)} }});\n"
        f"const out = await (async () => {{ {body} }})();\nprocess.stdout.write(JSON.stringify(out));\n")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([exe, str(f)], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@pytest.fixture
def hub(tmp_path):
    h = FakeHub()
    for app in ("links", "funes", "prospero", "kafka", "borges"):
        h.app(app)
    (tmp_path / "mcp-token").write_text(TOKEN, encoding="utf-8")
    yield h
    h.close()


def view(**extra):
    return {"id": "m1", "ok": True, "status": "done", "kind": "video", "files": [{"path": "/dl/a.mp4", "name": "a.mp4", "size": 3, "kind": "video"}], **extra}


def test_media_download_info_subtitles_and_the_polling(hub, tmp_path):
    running = {"id": "m2", "ok": False, "status": "downloading", "kind": "video", "files": []}
    hub.on("links", "media_download", sequence(lambda a, c: view() if a["url"].endswith("/one") else running))
    hub.on("links", "media_status", sequence(running, view(id="m2")))
    hub.on("links", "media_info", {"title": "T", "duration": 5, "thumbnail": "x", "subtitle_langs": ["es"]})
    hub.on("links", "media_subtitles", {"text": "hola", "lang": "es", "cues": []})
    hub.on("links", "media_audio_for_asr", {"id": "a1", "ok": True, "status": "done", "files": [{"path": "/w/a.wav", "name": "a.wav", "size": 9, "kind": "audio"}]})
    out = run(hub.url, tmp_path / "mcp-token", """
      const a = await fam.mediaDownload('https://x.test/one', { format: 'mp3', quality: 720, destDir: '/dl', sections: [[1, 2.5]], maxDurationS: 60, maxHeight: 720, saveLink: false, cookies: 'firefox' });
      const b = await fam.mediaDownload('https://x.test/two', { timeoutS: 30 });
      const bad = await fam.mediaDownload('https://x.test/two', { sections: [[5, 1]] });
      return { a, b, bad, info: await fam.mediaInfo('https://x.test/one'), subs: await fam.mediaSubtitles('https://x.test/one', { langs: ['es'] }),
               asr: await fam.mediaAudioForAsr('https://x.test/one') };
    """)
    assert out["a"]["ok"] is True and out["a"]["path"] == "/dl/a.mp4" and out["a"]["via"] == "links" and out["a"]["media_kind"] == "video"
    args = hub.of("media_download")[0]["args"]
    assert args["format"] == "audio" and args["quality"] == "720" and args["dir"] == args["dest_dir"] == str(Path("/dl").resolve()) and args["sections"] == [[1, 2.5]]
    assert args["max_duration_s"] == 60 and args["max_height"] == 720 and args["save_link"] is False and args["cookies_browser"] == "firefox" and args["wait"] is True
    assert out["b"]["ok"] is True and out["b"]["id"] == "m2" and len(hub.of("media_status")) == 2
    assert out["bad"]["kind"] == "client_error" and "bad section" in out["bad"]["error"]
    assert out["info"]["thumbnail"] == "x" and out["subs"]["text"] == "hola" and out["asr"]["ok"] is True and out["asr"]["path"] == "/w/a.wav" and out["asr"]["via"] == "links"


def test_media_download_deadline_and_failures(hub, tmp_path):
    running = {"id": "m3", "ok": False, "status": "processing", "kind": "video", "progress": 50, "files": []}
    hub.on("links", "media_download", running)
    hub.on("links", "media_status", running)
    out = run(hub.url, tmp_path / "mcp-token", "return await fam.mediaDownload('https://x.test/v', { timeoutS: 1 });")
    assert out["ok"] is False and out["still_running"] is True and out["kind"] == "timeout" and out["id"] == "m3" and out["progress"] == 50

    def refuse(a, c):
        raise ToolError(400, "enlace no válido")
    hub.on("links", "media_download", refuse)
    out = run(hub.url, tmp_path / "mcp-token", "return await fam.mediaDownload('https://x.test/v');")
    assert out == {"ok": False, "error": "enlace no válido", "via": "links", "kind": "tool_error"}


def test_media_info_falls_back_to_the_probe(hub, tmp_path):
    hub.on("links", "media_probe", {"title": "T", "duration": 7, "heights": [720]})
    out = run(hub.url, tmp_path / "mcp-token", "return await fam.mediaInfo('https://x.test/v');")
    assert out["ok"] and out["partial"] is True and out["title"] == "T" and out["thumbnail"] == "" and out["subtitle_langs"] == []


def test_transcribe_polls_normalises_and_reports_progress(hub, tmp_path):
    hub.on("funes", "transcribe_file", {"job_id": "j1", "status": "running", "progress": 0.1})
    hub.on("funes", "transcribe_status", sequence({"job_id": "j1", "status": "running", "progress": 50}, {
        "job_id": "j1", "status": "done", "language": "es", "text": "hola mundo", "model": "small",
        "segments": [{"start": 0, "end": 1.5, "text": " hola ", "words": [{"start": 0, "end": 0.5, "word": "hola", "probability": 0.9}]}, {"start": 1.5, "end": 2, "text": "mundo"}]}))
    out = run(hub.url, tmp_path / "mcp-token", """
      const seen = [];
      const res = await fam.transcribe('/clase.mp3', { language: 'es', model: 'small', initialPrompt: 'Luis', progress: (f) => seen.push(f) });
      return { res, seen };
    """)
    res = out["res"]
    assert res["ok"] is True and res["via"] == "funes" and res["text"] == "hola mundo" and res["duration_s"] == 2 and res["job_id"] == "j1"
    assert res["segments"][0] == {"start_s": 0, "end_s": 1.5, "text": "hola", "words": [{"start_s": 0, "end_s": 0.5, "word": "hola", "p": 0.9}]}
    assert out["seen"] == [0.1, 0.5, 1]
    a = hub.of("transcribe_file")[0]["args"]
    assert a["path"] == str(Path("/clase.mp3").resolve()) and a["language"] == "es" and a["model"] == "small" and a["initial_prompt"] == "Luis" and a["wait_s"] == 150


def test_transcribe_timeout_status_and_cancel(hub, tmp_path):
    hub.on("funes", "transcribe_file", {"job_id": "j2", "status": "running"})
    hub.on("funes", "transcribe_status", {"job_id": "j2", "status": "running"})
    hub.on("funes", "transcribe_cancel", {"job_id": "j2", "status": "cancelled"})
    out = run(hub.url, tmp_path / "mcp-token", """
      return { res: await fam.transcribe('/a.wav', { timeoutS: 1 }), st: await fam.transcribeStatus('j2', { waitS: 999 }), cancel: await fam.transcribeCancel('j2') };
    """)
    assert out["res"]["kind"] == "timeout" and out["res"]["job_id"] == "j2" and out["res"]["still_running"] is True and out["res"]["via"] == "funes"
    assert out["st"]["still_running"] is True and hub.of("transcribe_status")[-1]["args"]["wait_s"] == 150
    assert out["cancel"] == {"ok": True, "job_id": "j2", "status": "cancelled", "via": "funes"}


def test_speak(hub, tmp_path):
    wav = tmp_path / "t.wav"
    wav.write_bytes(b"x" * 12)
    hub.on("prospero", "voice_tts", {"ok": True, "path": str(wav), "engine_id": "piper"})
    out = run(hub.url, tmp_path / "mcp-token", f"""
      return {{ ok: await fam.speak(' Hola ', {{ voice: 'v', engine: 'piper', lang: 'es', speed: 1.2 }}), empty: await fam.speak('  '), long: await fam.speak('a'.repeat(20001)) }};
    """)
    assert out["ok"] == {"ok": True, "path": str(wav), "bytes": 12, "engine_id": "piper", "via": "prospero"}
    assert hub.of("voice_tts")[0]["args"] == {"text": "Hola", "voice": "v", "engine": "piper", "lang": "es", "speed": 1.2}
    assert out["empty"]["error"] == "empty text" and "too long" in out["long"]["error"] and len(hub.of("voice_tts")) == 1


def test_documents_pdf_ops_extract_and_ocr(hub, tmp_path):
    hub.on("kafka", "pdf_merge", {"ok": True, "operation": "merge", "outputs": [{"path": "/w/m.pdf"}], "output": "/w/m.pdf"})
    hub.on("kafka", "pdf_compress", {"ok": True, "outputs": [{"path": "/w/c.pdf"}]})
    hub.on("kafka", "pdf_info", {"ok": True, "pages": 3})
    hub.on("kafka", "pdf_from_images", {"ok": True, "outputs": [{"path": "/w/i.pdf"}]})
    hub.on("kafka", "doc_extract", {"job_id": "o1", "status": "running"})
    hub.on("kafka", "ocr_status", sequence({"job_id": "o1", "status": "done", "kind": "pdf", "title": "T", "units": [{"kind": "page", "number": 1, "title": "", "text": "uno"}],
                                            "needs_ocr": False, "notes": [], "pages_ocr": 1}, {"available": True, "backend": "rapidocr"}))
    hub.on("kafka", "ocr_image", {"text": "TOTAL", "blocks": [], "backend": "rapidocr"})
    out = run(hub.url, tmp_path / "mcp-token", """
      return {
        merge: await fam.pdfMerge(['/a.pdf', 'd_9'], { ranges: ['', '1-2'], outDir: '/w', fileResult: true }),
        compress: await fam.pdfCompress('/a.pdf', { preset: 'screen', targetMb: 2 }),
        info: await fam.pdfInfo('/a.pdf', { password: 'pw' }),
        images: await fam.imagesToPdf(['/1.png'], { pageSize: 'fit', outDir: '/w' }),
        extract: await fam.docsExtract('/scan.pdf', { ocr: 'force', maxPages: 5, lang: 'en', timeoutS: 30 }),
        badOcr: await fam.docsExtract('/scan.pdf', { ocr: 'maybe' }),
        image: await fam.docsOcrImage('/t.png', { blocks: true }),
        engine: await fam.docsOcrStatus(),
      };
    """)
    assert out["merge"]["ok"] and out["merge"]["paths"] == ["/w/m.pdf"] and out["merge"]["via"] == "kafka"
    assert hub.of("pdf_merge")[0]["args"] == {"files": [str(Path("/a.pdf").resolve()), "d_9"], "ranges": ["", "1-2"], "out_dir": str(Path("/w").resolve()), "file_result": True}
    assert hub.of("pdf_compress")[0]["args"] == {"file": str(Path("/a.pdf").resolve()), "preset": "screen", "target_mb": 2, "engine": "auto"}
    assert out["info"]["pages"] == 3 and hub.of("pdf_info")[0]["args"] == {"file": str(Path("/a.pdf").resolve()), "password": "pw"}
    assert hub.of("pdf_from_images")[0]["args"] == {"images": [str(Path("/1.png").resolve())], "page_size": "fit", "margin_mm": 10, "orientation": "auto", "out_dir": str(Path("/w").resolve())}
    ex = out["extract"]
    assert ex["ok"] and ex["via"] == "kafka" and ex["pages_ocr"] == 1 and ex["text"] == "uno" and ex["units"][0]["number"] == 1
    assert hub.of("doc_extract")[0]["args"] == {"path": str(Path("/scan.pdf").resolve()), "ocr": "force", "max_pages": 5, "lang": "en", "wait_s": 30}
    assert out["badOcr"]["kind"] == "client_error"
    assert out["image"]["text"] == "TOTAL" and hub.of("ocr_image")[0]["args"] == {"path": str(Path("/t.png").resolve()), "lang": "es", "blocks": True}
    assert out["engine"]["available"] is True and hub.of("ocr_status")[-1]["args"] == {}


def test_embeddings_batches_and_model_drift(hub, tmp_path):
    def embed(args, call):
        return {"model": "bge-small", "dim": 2, "vectors": [[3.0, 4.0] if t else [0.0, 1.0] for t in args["texts"]], "normalized": False}
    hub.on("borges", "embed_texts", embed)
    hub.on("borges", "embed_status", {"backend": "fastembed", "model": "bge-small", "dim": 2, "state": "ready"})
    out = run(hub.url, tmp_path / "mcp-token", """
      return { many: await fam.embedTexts(['a', 'b', 'c'], { batch: 2 }), query: await fam.embedQuery('q'), status: await fam.embedStatus(),
               bad: await fam.embedTexts(['a'], { kind: 'x' }), empty: await fam.embedTexts([]) };
    """)
    assert out["many"]["ok"] and out["many"]["model"] == "bge-small" and out["many"]["dim"] == 2 and len(out["many"]["vectors"]) == 3
    assert out["many"]["vectors"][0] == pytest.approx([0.6, 0.8])                       # unit length even though the hub said normalized: false
    assert [len(c["args"]["texts"]) for c in hub.of("embed_texts")[:2]] == [2, 1]
    assert out["query"]["vector"] == pytest.approx([0.6, 0.8]) and hub.of("embed_texts")[2]["args"]["kind"] == "query"
    assert out["status"] == {"ok": True, "backend": "fastembed", "model": "bge-small", "dim": 2, "state": "ready", "ready": True, "via": "borges"}
    assert out["bad"]["kind"] == "client_error" and out["empty"]["vectors"] == []

    hub.on("borges", "embed_texts", sequence(lambda a, c: {"model": "m1", "dim": 2, "vectors": [[1, 0]] * len(a["texts"])}, lambda a, c: {"model": "m2", "dim": 2, "vectors": [[1, 0]] * len(a["texts"])}))
    drift = run(hub.url, tmp_path / "mcp-token", "return await fam.embedTexts(['a', 'b', 'c'], { batch: 2 });")
    assert drift["ok"] is False and "changed during the call" in drift["error"]


def test_errors_hub_down_app_down_missing_tool_and_auth(hub, tmp_path):
    down = run(f"http://127.0.0.1:{free_port()}", tmp_path / "mcp-token", """
      return [await fam.mediaDownload('https://x.test/v'), await fam.transcribe('/a.wav'), await fam.speak('hola'), await fam.pdfInfo('/a.pdf'), await fam.embedTexts(['a'])];
    """)
    assert [r["error"] for r in down] == ["hub unreachable"] * 5 and [r["via"] for r in down] == ["links", "funes", "prospero", "kafka", "borges"]
    assert all(r["kind"] == "hub_down" for r in down)
    hub.set_state("kafka", "stopped")
    out = run(hub.url, tmp_path / "mcp-token", """
      return [await fam.pdfInfo('/a.pdf'), await fam.docsOcrImage('/t.png'), await fam.embedStatus()];
    """)
    assert out[0] == {"ok": False, "error": "kafka unreachable", "via": "kafka", "kind": "app_down"}
    assert out[1]["kind"] == "app_down"
    assert out[2]["kind"] == "tool_missing" and "embed_status" in out[2]["error"]
    (tmp_path / "bad-token").write_text("nope", encoding="utf-8")
    auth = run(hub.url, tmp_path / "bad-token", "return await fam.speak('hola');")
    assert auth["kind"] == "auth" and "nope" not in json.dumps(auth)


def test_service_availability_is_cached(hub, tmp_path):
    out = run(hub.url, tmp_path / "mcp-token", """
      const a = await fam.serviceAvailable('media');
      const b = await fam.serviceAvailable('stt');
      const none = await fam.serviceAvailable('nope');
      return { a, b, none };
    """)
    assert out == {"a": True, "b": True, "none": False}
    assert "/api/apps/links" in hub.gets and "/api/apps/funes" in hub.gets
