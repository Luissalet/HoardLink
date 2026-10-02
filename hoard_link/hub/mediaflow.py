"""Opt-in download -> stateless transcription -> transcript collection, owned by the Hub."""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

from ..atomic import write_text_atomic
from ..ids import new_id
from ..paths import unsafe_folder, unsafe_file, is_inside
from . import contract


class MediaFlow:
    def __init__(self, hub: Any):
        self.hub = hub
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def start(self, args):
        if not str(args.get("folder") or "").strip():
            raise ValueError("choose a transcript folder")
        folder = Path(str(args.get("folder") or "")).expanduser().resolve()
        bad = unsafe_folder(folder, lang="en")
        if bad:
            raise ValueError(bad)
        if not args.get("url") and not args.get("download_id"):
            raise ValueError("give url or download_id")
        with self._lock:
            self._jobs = {k:v for k,v in self._jobs.items() if time.time()-v["created_at"] < 3600 or v["status"] == "running"}
            if sum(v["status"] == "running" for v in self._jobs.values()) >= 2:
                return {"ok": False, "error": "two media imports are already running"}
            job = {"job_id": new_id(), "status": "running", "stage": "download", "created_at": time.time(), "done": threading.Event()}
            self._jobs[job["job_id"]] = job
        threading.Thread(target=self._run, args=(job, dict(args), folder), daemon=True, name="hub-media-import").start()
        return {"ok": True, "job_id": job["job_id"], "status": "running"}

    def status(self, args):
        with self._lock:
            job = self._jobs.get(str(args.get("job_id") or ""))
        if not job:
            raise LookupError("media import job not found")
        job["done"].wait(max(0, min(150, float(args.get("wait_s") or 0))))
        with self._lock:
            return {"ok": job["status"] != "failed", **{k:v for k,v in job.items() if k != "done"}}

    def _call(self, app_id, tool, args):
        app = self.hub.get(app_id)
        if app is None:
            raise RuntimeError(f"{app_id} is not registered")
        result = contract.call_app(app, tool, args, timeout=45, caller="hub")
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or f"{app_id}.{tool} failed")
        data = result.get("result") or {}
        if data.get("ok") is False and data.get("status") not in ("queued", "running", "downloading", "processing"):
            raise RuntimeError(data.get("error") or f"{app_id}.{tool} failed")
        return data

    def _finish_job(self, app, status_tool, key, data):
        deadline = time.monotonic() + 600
        while data.get("status") not in ("done", "error", "failed", "cancelled"):
            if self._closed.is_set() or time.monotonic() >= deadline:
                raise RuntimeError("media import interrupted or timed out; owner job can be followed separately")
            data = self._call(app, status_tool, {key: data.get(key), "wait_s": 30})
        if data.get("status") != "done":
            raise RuntimeError(data.get("error") or f"{app} job {data.get('status')}")
        return data

    def _run(self, job, args, folder):
        try:
            if args.get("download_id"):
                media = self._call("links", "media_status", {"id": args["download_id"], "wait_s": 30})
            else:
                media = self._call("links", "media_download", {"url": args["url"], "format": "audio", "dest_dir": str(folder),
                                                              "save_link": False, "wait": True, "timeout_s": 30})
            media = self._finish_job("links", "media_status", "id", media)
            files = media.get("files") or []
            if not files:
                raise RuntimeError("download produced no files")
            audio = Path(files[0]["path"]).resolve()
            if not is_inside(audio, folder) or unsafe_file(audio, lang="en"):
                raise RuntimeError("download file is outside the selected folder or is unsafe")
            with self._lock:
                job.update(stage="transcription", download_id=media.get("id"), audio_path=str(audio))
            transcript = self._call("funes", "transcribe_file", {"path": str(audio), "language": args.get("language") or "auto",
                                                                "model": args.get("model"), "wait_s": 30})
            with self._lock:
                job["transcribe_job_id"] = transcript.get("job_id")
            transcript = self._finish_job("funes", "transcribe_status", "job_id", transcript)
            text = str(transcript.get("text") or "").strip()
            if not text:
                raise RuntimeError("no speech was recovered; nothing was indexed")
            path = folder / (job["job_id"] + ".transcript.txt")
            write_text_atomic(path, f"Source: {args.get('url') or media.get('source_url') or audio}\n\n{text}\n")
            with self._lock:
                job.update(stage="indexing", transcript_path=str(path))
            collection = self._call("borges", "library_add_collection", {"path": str(folder), "name": "Media transcripts",
                                                                         "include": ["*.transcript.txt"], "watch": True})
            # Adding an existing folder does not reindex it; explicitly request the scan.
            collection_id = (collection.get("collection") or collection).get("id")
            if collection_id:
                indexing = self._call("borges", "library_reindex", {"collection_id": collection_id})
            else:
                indexing = collection
            with self._lock:
                job.update(status="done", stage="index_requested", result={"download_id": media.get("id"), "transcript_path": str(path),
                                                                           "collection": collection, "indexing": indexing})
        except Exception as exc:
            with self._lock:
                job.update(status="failed", error=str(exc))
        finally:
            job["done"].set()
