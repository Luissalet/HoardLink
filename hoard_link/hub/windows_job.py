"""Private Windows jobs, assigned at process creation."""
from __future__ import annotations

import ctypes as c
from contextlib import ExitStack
from ctypes import wintypes as w
import msvcrt
import os
from subprocess import BELOW_NORMAL_PRIORITY_CLASS, TimeoutExpired, list2cmdline
from threading import Lock
from time import monotonic, sleep
from uuid import uuid4
from weakref import finalize

from .. import _process

_HANDLE_LIST = 0x20002
_JOB_LIST = 0x2000D
_EXTENDED_STARTUPINFO = 0x80000
_UNICODE_ENVIRONMENT = 0x400
_KILL_ON_CLOSE = 0x2000
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


class _BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t),
                ("process_limit", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling_class", w.DWORD)]


class _ExtendedLimits(c.Structure):
    _fields_ = [("basic", _BasicLimits), ("io_counters", c.c_uint64 * 6),
                ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]


class _Accounting(c.Structure):
    _fields_ = [("times", c.c_int64 * 4), ("page_faults", w.DWORD),
                ("total_processes", w.DWORD), ("active_processes", w.DWORD),
                ("terminated_processes", w.DWORD)]


class _Startup(c.Structure):
    _fields_ = [("size", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), ("dimensions", w.DWORD * 7), ("flags", w.DWORD),
                ("show_window", w.WORD), ("reserved_size", w.WORD), ("reserved_data", c.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [("info", _Startup), ("attributes", c.c_void_p)]


class _ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


_kernel = c.WinDLL("kernel32", use_last_error=True)
_user = c.WinDLL("user32", use_last_error=True)


def _api(name, result, *args, library=_kernel):
    fn = getattr(library, name)
    fn.restype = result
    fn.argtypes = args
    return fn


_create_job = _api("CreateJobObjectW", w.HANDLE, c.c_void_p, w.LPCWSTR)
_set_job = _api("SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
_query_job = _api("QueryInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p)
_terminate_job = _api("TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
_close = _api("CloseHandle", w.BOOL, w.HANDLE)
_current = _api("GetCurrentProcess", w.HANDLE)
_duplicate = _api("DuplicateHandle", w.BOOL, w.HANDLE, w.HANDLE, w.HANDLE,
                  c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD)
_init_attrs = _api("InitializeProcThreadAttributeList", w.BOOL, c.c_void_p, w.DWORD, w.DWORD,
                   c.POINTER(c.c_size_t))
_update_attrs = _api("UpdateProcThreadAttribute", w.BOOL, c.c_void_p, w.DWORD, c.c_size_t,
                     c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p)
_delete_attrs = _api("DeleteProcThreadAttributeList", None, c.c_void_p)
_create_process = _api("CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p,
                       w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(_StartupEx), c.POINTER(_ProcessInfo))
_wait = _api("WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
_exit_code = _api("GetExitCodeProcess", w.BOOL, w.HANDLE, c.POINTER(w.DWORD))
_get_station = _api("GetProcessWindowStation", w.HANDLE, library=_user)
_object_info = _api("GetUserObjectInformationW", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD,
                    c.c_void_p, library=_user)
_create_desktop = _api("CreateDesktopW", w.HANDLE, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD,
                       w.DWORD, c.c_void_p, library=_user)
_close_desktop = _api("CloseDesktop", w.BOOL, w.HANDLE, library=_user)


def _check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


class _Job:
    def __init__(self):
        self.handle = _check(_create_job(None, None))  # unnamed and non-inheritable
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_CLOSE
        try:
            _check(_set_job(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except OSError:
            self.close()
            raise

    def running(self):
        if self.handle is None:
            return False
        state = _Accounting()
        _check(_query_job(self.handle, 1, c.byref(state), c.sizeof(state), None))
        return state.active_processes > 0

    def close(self):
        if self.handle is not None:
            _check(_close(self.handle))
            self.handle = None


def _inherit(file, stack):
    result = w.HANDLE()
    current = _current()
    _check(_duplicate(current, msvcrt.get_osfhandle(file.fileno()), current, c.byref(result), 0, True, 2))
    stack.callback(_close, result)
    return result.value


def _launch(jobs, argv, cwd, env, log, *, desktop=None, creationflags=0):
    command = c.create_unicode_buffer(list2cmdline(argv))
    environment = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in
                                                     sorted(env.items(), key=lambda item: item[0].upper())) + "\0")
    with ExitStack() as stack:
        null = stack.enter_context(open(os.devnull, "rb"))
        stdin, output = _inherit(null, stack), _inherit(log, stack)
        handles = (w.HANDLE * 2)(stdin, output)
        job_handles = (w.HANDLE * len(jobs))(*(job.handle for job in jobs))
        size = c.c_size_t()
        _init_attrs(None, 2, 0, c.byref(size))  # first call obtains the buffer size
        attributes = c.create_string_buffer(size.value)
        _check(_init_attrs(attributes, 2, 0, c.byref(size)))
        stack.callback(_delete_attrs, attributes)
        _check(_update_attrs(attributes, 0, _HANDLE_LIST, handles, c.sizeof(handles), None, None))
        _check(_update_attrs(attributes, 0, _JOB_LIST, job_handles, c.sizeof(job_handles), None, None))
        start = _StartupEx()
        start.info.size = c.sizeof(start)
        start.info.flags = 0x100  # STARTF_USESTDHANDLES
        start.info.desktop = desktop
        start.info.stdin, start.info.stdout, start.info.stderr = stdin, output, output
        start.attributes = c.cast(attributes, c.c_void_p)
        info = _ProcessInfo()
        options = {"creationflags": _EXTENDED_STARTUPINFO | _UNICODE_ENVIRONMENT | creationflags}
        _process._hide_console(options)
        _check(_create_process(argv[0], command, None, None, True, options["creationflags"],
                               environment, cwd, c.byref(start), c.byref(info)))
        _close(info.thread)
        return info


class DesktopWorker:
    """The process operations used by the worker monitor and stop controls."""

    def __init__(self, argv, info, job, resources):
        self.args = argv
        self.pid = info.pid
        self.returncode = None
        self._handle = info.process
        self._job = job
        self._group = resources
        self._lock = Lock()
        self._cleanup = finalize(self, _close_worker, resources, self._handle)

    def wait(self, timeout=None):
        milliseconds = _INFINITE if timeout is None else min(_INFINITE - 1, max(0, round(timeout * 1000)))
        state = _wait(self._handle, milliseconds)
        if state == _WAIT_TIMEOUT:
            raise TimeoutExpired(self.args, timeout)
        if state != 0:
            raise c.WinError(c.get_last_error())
        with self._lock:
            if self.returncode is None:
                code = w.DWORD()
                _check(_exit_code(self._handle, c.byref(code)))
                self.returncode = code.value
                # Close the job first to reap any descendants before dropping the desktop.
                self._group.close()
            return self.returncode

    def poll(self):
        try:
            return self.wait(0)
        except TimeoutExpired:
            return None

    def terminate(self):
        with self._lock:
            if self._job.handle is not None:
                _check(_terminate_job(self._job.handle, 1))

    kill = terminate


def _close_worker(resources, process_handle):
    try:
        resources.close()
    finally:
        _close(process_handle)


def launch_on_private_desktop(argv, *, cwd, env, log):
    name = "HoardWorker_" + uuid4().hex
    station = c.create_unicode_buffer(256)
    _check(_object_info(_check(_get_station()), 2, station, c.sizeof(station), None))  # UOI_NAME
    desktop = station.value + "\\" + name
    with ExitStack() as resources:
        handle = _check(_create_desktop(name, None, None, 0, 0x1FF, None))
        resources.callback(_close_desktop, handle)
        job = _Job()
        resources.callback(job.close)
        info = _launch((job,), argv, cwd, env, log, desktop=desktop,
                       creationflags=BELOW_NORMAL_PRIORITY_CLASS)
        return DesktopWorker(argv, info, job, resources.pop_all())


class WindowsJobs:
    def __init__(self):
        self._hub = _Job()
        self._children = set()

    def launch(self, argv, *, cwd, env, log):
        if self._hub.handle is None:
            raise OSError("The Hub job is closed")
        child = _Job()
        try:
            info = _launch((self._hub, child), argv, cwd, env, log)
            _close(info.process)
        except (OSError, ValueError) as exc:
            child.close()
            raise OSError(f"Could not create the process in the Hub jobs: {exc}") from exc
        self._children.add(child)
        return child

    def stop(self, child):
        if child.running():
            _check(_terminate_job(child.handle, 1))
            deadline = monotonic() + 5
            while child.running():
                if monotonic() >= deadline:
                    raise OSError("The fixture job is still draining")
                sleep(0.01)
        child.close()
        self._children.discard(child)

    def close(self):
        self._hub.close()
        for child in tuple(self._children):
            child.close()
            self._children.discard(child)
