"""Checkpointed Links -> Funes -> Borges workflow. Restart pauses; explicit
resume follows saved owner job IDs. Lost mutation replies are never replayed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..atomic import write_text_atomic, write_json_atomic
from ..ids import new_id
from ..paths import unsafe_folder, unsafe_file, is_inside
from ..waiting import DONE_STATES, clamp_wait
from . import contract


class _Paused(RuntimeError):
    pass


class _Rejected(RuntimeError):
    """The owner refused the operation before returning a job."""
    pass


class MediaFlow:
    def __init__(self, hub: Any):
        self.hub = hub
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._closed = threading.Event()
        data_dir = getattr(getattr(hub, "config", None), "data_dir", None)
        self._path = Path(data_dir) / "media-imports.json" if data_dir else None
        if self._path and self._path.exists():
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            if saved.get("schema") != 1 or not isinstance(saved.get("jobs"), list):
                raise ValueError("unsupported media import journal; preserve it for recovery")
            for value in saved["jobs"]:
                job = dict(value, done=threading.Event(), worker=False)
                if job["status"] == "running":
                    job.update(status="paused", error="Hub restarted; resume explicitly using the saved owner job IDs")
                job["done"].set()
                self._jobs[job["job_id"]] = job
            self._save()

    @staticmethod
    def _serial(job):
        return {k: v for k, v in job.items() if k not in ("done", "worker")}

    def _save(self):
        if self._path:
            write_json_atomic(self._path, {"schema": 1, "jobs": [self._serial(j) for j in self._jobs.values()]})

    def _checkpoint(self, job, **fields):
        with self._lock:
            job.update(fields, updated_at=time.time())
            self._save()

    def close(self):
        self._closed.set()

    def _check_open(self):
        if self._closed.is_set():
            raise _Paused("Hub stopped; owner work keeps running, resume explicitly")

    def start(self, args):
        self._check_open()
        if not str(args.get("folder") or "").strip():
            raise ValueError("choose a transcript folder")
        folder = Path(str(args["folder"])).expanduser().resolve()
        bad = unsafe_folder(folder, lang="en")
        if bad:
            raise ValueError(bad)
        if bool(args.get("url")) == bool(args.get("download_id")):
            raise ValueError("give exactly one of url or download_id")
        if args.get("url"):
            parsed = urlsplit(str(args["url"]))
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("give a HTTP(S) source URL without credentials")
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items() if time.time() - v["created_at"] < 7 * 86400
                          or v["status"] in ("running", "paused")}
            if args.get("download_id"):
                for prior in self._jobs.values():
                    previous = prior["arguments"]
                    same_options = (previous.get("language") or "auto", previous.get("model")) == (args.get("language") or "auto", args.get("model"))
                    if (prior.get("download_id") == str(args["download_id"]) and prior["folder"] == str(folder)
                            and same_options and prior["status"] in ("running", "paused", "done")):
                        return {**self._view(prior), "existing": True}
            if len(self._jobs) >= 100:
                return {"ok": False, "error": "media import journal is full; finish paused imports first"}
            if sum(v.get("worker", False) for v in self._jobs.values()) >= 2:
                return {"ok": False, "error": "two media imports are already running"}
            job = {"job_id": new_id(), "status": "running", "stage": "download", "created_at": time.time(),
                   "arguments": {k: v for k, v in args.items() if k in ("url", "download_id", "language", "model")},
                   "folder": str(folder), "done": threading.Event(), "worker": True}
            if args.get("download_id"):
                job["download_id"] = str(args["download_id"])
            self._jobs[job["job_id"]] = job
            self._save()
        self._launch(job)
        return {"ok": True, "job_id": job["job_id"], "status": "running"}

    def _launch(self, job):
        self._event(job, "started")
        threading.Thread(target=self._run, args=(job,), daemon=True, name="hub-media-import").start()

    def resume(self, args):
        self._check_open()
        with self._lock:
            job = self._jobs.get(str(args.get("job_id") or ""))
            if not job:
                raise LookupError("media import job not found")
            if job.get("worker"):
                raise ValueError("media import worker is still running")
            if job["status"] == "done":
                return self._view(job)
            if sum(v.get("worker", False) for v in self._jobs.values()) >= 2:
                raise ValueError("two media imports are already running")
            pending = job.get("pending")
            reconcile = {"media_download": "download_id", "transcribe_file": "transcribe_job_id",
                         "library_add_collection": "collection_id", "library_reindex": "index_requested"}
            key = reconcile.get(pending)
            if key and not args.get(key):
                raise ValueError(f"uncertain {pending}; inspect the owner and provide {key} before resuming")
            if key:
                value = args[key]
                if key == "index_requested" and value is not True:
                    raise ValueError("index_requested must be true after checking the owner")
                if key == "collection_id" and (type(value) is not int or value <= 0):
                    raise ValueError("collection_id must be a positive integer from Borges")
                if key in ("download_id", "transcribe_job_id") and (not isinstance(value, str) or not value.strip() or len(value) > 128):
                    raise ValueError(f"{key} must be the owner's job ID (1..128 characters)")
                job[key] = args[key]
            folder = Path(job["folder"]).resolve()
            bad = unsafe_folder(folder, lang="en")
            if bad:
                raise ValueError(bad)
            job.update(status="running", worker=True, pending="", error="")
            job["done"].clear()
            self._save()
        self._launch(job)
        return {"ok": True, "job_id": job["job_id"], "status": "running"}

    def _view(self, job):
        return {"ok": job["status"] != "failed",
                **{k: v for k, v in self._serial(job).items() if k != "arguments"},
                "still_running": job["status"] == "running", "resume_required": job["status"] == "paused"}

    def status(self, args):
        with self._lock:
            job = self._jobs.get(str(args.get("job_id") or ""))
        if not job:
            raise LookupError("media import job not found")
        job["done"].wait(clamp_wait(args.get("wait_s")))
        with self._lock:
            return self._view(job)

    def list(self):
        with self._lock:
            return {"ok": True, "jobs": [self._view(j) for j in sorted(self._jobs.values(),
                                                                    key=lambda j: j["created_at"], reverse=True)]}

    def _event(self, job, state):
        events = getattr(self.hub, "events", None)
        if events:
            try:
                events.emit("hub.job." + state, {"job_id": job["job_id"], "title": "Media import: transcript library",
                            "kind": "media_import", "stage": job["stage"], "error": job.get("error", "")}, source="hub")
            except Exception:
                pass  # journal is the truth; events are hints

    def _call(self, app_id, tool, args):
        self._check_open()
        app = self.hub.get(app_id)
        if app is None:
            raise _Rejected(f"{app_id} is not registered")
        result = contract.call_app(app, tool, args, timeout=45, caller="hub")
        if not result.get("ok"):
            error = result.get("error") or f"{app_id}.{tool} failed"
            certain = result.get("status") in (400, 401, 403, 404, 409, 422, 503)
            certain = certain or any(word in str(error).lower() for word in ("not reachable", "unavailable"))
            raise (_Rejected if certain else RuntimeError)(error)
        data = result.get("result") or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{app_id}.{tool} returned an invalid job view")
        if data.get("ok") is False and data.get("status") not in ("queued", "running", "downloading", "processing"):
            raise RuntimeError(data.get("error") or f"{app_id}.{tool} failed")
        return data

    def _mutate(self, job, app, tool, args, key=None, field=None):
        self._check_open()
        self._checkpoint(job, pending=tool)
        try:
            data = self._call(app, tool, args)
        except _Rejected:
            self._checkpoint(job, pending="")
            raise
        fields = {field: data.get(key) if key else True} if field else {}
        if key and not fields[field]:
            raise _Paused(f"{tool} returned no {key}; inspect the owner before resuming")
        self._checkpoint(job, pending="", **fields)
        self._check_open()
        return data

    def _finish_job(self, app, status_tool, key, data):
        deadline = time.monotonic() + 600
        while data.get("status") not in DONE_STATES:
            self._check_open()
            if time.monotonic() >= deadline:
                raise _Paused("media import timed out; owner job keeps running, resume using its saved ID")
            if not data.get(key):
                raise RuntimeError(f"{app} did not return a {key}")
            data = self._call(app, status_tool, {key: data[key], "wait_s": 30})
        if data.get("status") != "done":
            raise RuntimeError(data.get("error") or f"{app} job {data.get('status')}")
        return data

    @staticmethod
    def _source(args, audio):
        url = str(args.get("url") or "")
        if not url:
            return str(audio)
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))

    def _run(self, job):
        args, folder = job["arguments"], Path(job["folder"])
        try:
            if not job.get("transcript_path"):
                if job.get("download_id"):
                    media = self._call("links", "media_status", {"id": job["download_id"], "wait_s": 30})
                else:
                    media = self._mutate(job, "links", "media_download", {"url": args["url"], "format": "audio",
                            "dest_dir": str(folder), "save_link": False, "wait": True, "timeout_s": 30}, "id", "download_id")
                media = self._finish_job("links", "media_status", "id", media)
                files = media.get("files") or []
                if not files:
                    raise RuntimeError("download produced no files")
                audio = Path(files[0]["path"]).resolve()
                if not audio.is_file() or not is_inside(audio, folder) or unsafe_file(audio, lang="en"):
                    raise RuntimeError("download file is missing, outside the selected folder or unsafe")
                self._checkpoint(job, stage="transcription", audio_path=str(audio))
                self._event(job, "progress")
                if job.get("transcribe_job_id"):
                    transcript = self._call("funes", "transcribe_status", {"job_id": job["transcribe_job_id"], "wait_s": 30})
                else:
                    transcript = self._mutate(job, "funes", "transcribe_file", {"path": str(audio),
                                "language": args.get("language") or "auto", "model": args.get("model"), "wait_s": 30},
                                "job_id", "transcribe_job_id")
                transcript = self._finish_job("funes", "transcribe_status", "job_id", transcript)
                text = str(transcript.get("text") or "").strip()
                if not text:
                    raise RuntimeError("no speech was recovered; nothing was indexed")
                path = folder / (job["job_id"] + ".transcript.txt")
                content = f"Source: {self._source(args, audio)}\n\n{text}\n"
                self._check_open()
                if path.exists() and path.read_bytes() != content.encode("utf-8"):
                    raise RuntimeError("transcript output already exists with different content; preserve it for review")
                write_text_atomic(path, content)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                provenance = {"schema": 1, "workflow": "hub_media_import", "job_id": job["job_id"],
                    "source": self._source(args, audio), "download_id": job.get("download_id"),
                    "transcribe_job_id": job.get("transcribe_job_id"), "transcript_sha256": digest,
                    "language": transcript.get("language"), "model": transcript.get("model"), "created_at": time.time()}
                write_json_atomic(path.with_suffix(".provenance.json"), provenance)
                self._checkpoint(job, stage="indexing", transcript_path=str(path), transcript_sha256=digest)
            path = Path(job["transcript_path"]).resolve()
            if not path.is_file() or not is_inside(path, folder) or unsafe_file(path, lang="en"):
                raise RuntimeError("saved transcript is missing or outside the selected folder")
            if hashlib.sha256(path.read_bytes()).hexdigest() != job["transcript_sha256"]:
                raise RuntimeError("saved transcript changed; review it before indexing")
            self._event(job, "progress")
            if not job.get("collection_id"):
                self._checkpoint(job, pending="library_add_collection")
                try:
                    collection = self._call("borges", "library_add_collection", {"path": str(folder), "name": "Media transcripts",
                                                                              "include": ["*.transcript.txt"], "watch": True})
                except _Rejected:
                    self._checkpoint(job, pending="")
                    raise
                collection_id = (collection.get("collection") or collection).get("id")
                if not collection_id:
                    raise _Paused("library did not return a collection ID; inspect Borges before resuming")
                self._checkpoint(job, pending="", collection_id=collection_id, collection=collection)
            self._check_open()
            if not job.get("index_requested"):
                indexing = self._mutate(job, "borges", "library_reindex", {"collection_id": job["collection_id"]},
                                        field="index_requested")
                self._checkpoint(job, indexing=indexing)
            self._checkpoint(job, status="done", stage="index_requested", result={"download_id": job.get("download_id"),
                        "transcript_path": job["transcript_path"], "collection_id": job["collection_id"], "index_requested": True,
                        "collection": job.get("collection") or {"collection": {"id": job["collection_id"]}},
                        "indexing": job.get("indexing") or {"requested": True}})
            self._event(job, "done")
        except Exception as exc:
            paused = isinstance(exc, _Paused) or bool(job.get("pending")) or self._closed.is_set()
            self._checkpoint(job, status="paused" if paused else "failed", error=str(exc))
            self._event(job, "paused" if paused else "failed")
        finally:
            with self._lock:
                job["worker"] = False
                job["done"].set()
