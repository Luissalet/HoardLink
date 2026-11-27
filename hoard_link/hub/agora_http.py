"""Bound Agora work separately from the Hub's app and health requests."""

from concurrent.futures import CancelledError, Future
from queue import Empty, SimpleQueue
from threading import Lock, Thread


class AgoraBusy(RuntimeError):
    """The lane has no room for another request."""


class AgoraWorkPool:
    """Bound the number of queued and active calls, with lazy daemon workers.

    The Hub already shuts down without waiting for open requests. A long inbox
    poll or provider discovery must not keep the process alive after shutdown.
    """

    def __init__(self, name, workers, capacity):
        self._name = f"hoard-agora-{name}"
        self._workers = workers
        self._capacity = capacity
        self._threads = []
        self._outstanding = 0
        self._queue = SimpleQueue()
        self._lock = Lock()
        self._closed = False

    def submit(self, invoke, *args):
        with self._lock:
            if self._closed or self._outstanding >= self._capacity:
                raise AgoraBusy
            if self._outstanding >= len(self._threads) and len(self._threads) < self._workers:
                thread = Thread(target=self._run, name=f"{self._name}-{len(self._threads)}", daemon=True)
                try:
                    thread.start()
                except RuntimeError as exc:
                    raise AgoraBusy from exc
                self._threads.append(thread)
            future = Future()
            self._outstanding += 1
            self._queue.put((future, invoke, args))
        return future

    def _run(self):
        while (work := self._queue.get()) is not None:
            future, invoke, args = work
            failure = None
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                result = invoke(*args)
            except BaseException as exc:
                failure = exc
            finally:
                with self._lock:
                    self._outstanding -= 1
            if failure is None:
                future.set_result(result)
            else:
                future.set_exception(failure)

    def close(self):
        queued = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    future, _, _ = self._queue.get_nowait()
                except Empty:
                    break
                queued.append(future)
                self._outstanding -= 1
            for _ in self._threads:
                self._queue.put(None)
        for future in queued:
            future.cancel()


class AgoraHTTP:
    def __init__(self):
        self._ordinary = AgoraWorkPool("request", workers=8, capacity=32)
        self._console = AgoraWorkPool("overview", workers=3, capacity=8)
        self._inbox = AgoraWorkPool("inbox", workers=24, capacity=24)

    def call(self, name, invoke, *, wait_s=None):
        try:
            return self._lane(name, wait_s).submit(invoke).result()
        except CancelledError as exc:
            raise AgoraBusy from exc

    def _lane(self, name, wait_s):
        if name in ("overview", "hub_agora_console_overview"):
            return self._console
        if name in ("inbox", "hub_agora_inbox"):
            try:
                if float(wait_s or 0) > 0:
                    return self._inbox
            except (TypeError, ValueError):
                pass
        return self._ordinary

    def close(self):
        for lane in (self._ordinary, self._console, self._inbox):
            lane.close()
