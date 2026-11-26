"""Startup and process ownership for an isolated Hub."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import secrets
import select
import sys
import tempfile
from contextlib import suppress
from subprocess import DEVNULL, TimeoutExpired
from time import monotonic, sleep


def passive_error():
    return {"ok": False, "status": 409, "code": "isolated_hub",
            "error": "The isolated Hub is passive. El Hub aislado es pasivo."}


def inside(path, root):
    return Path(path).expanduser().resolve().is_relative_to(root)


def temporary_root(data_dir):
    temp = Path(tempfile.gettempdir()).resolve()
    root = Path(data_dir).expanduser().resolve().parent
    if root == temp or not root.is_relative_to(temp):
        raise ValueError("Use a dedicated temporary folder for isolated Hub data. "
                         "Usa una carpeta temporal propia para los datos del Hub aislado.")
    return root


_UNIT_PREFIX = "hoard-hub-test-"
_CGROUP_MOUNT = Path("/sys/fs/cgroup")
_SERVICE_PROPERTIES = {
    "Type": "exec", "KillMode": "control-group", "Restart": "no", "Delegate": "yes",
}


def _systemctl(*args):
    from .._process import run_hidden

    try:
        return run_hidden(["systemctl", "--user", *args], text=True, capture_output=True, timeout=10)
    except (OSError, TimeoutExpired) as exc:
        raise OSError("Cannot reach the systemd user manager. No se puede acceder al gestor de usuario de systemd.") from exc


def enter_linux_service(argv, env):
    """Start the actual Hub as the main process of a transient user service."""
    from .._process import run_hidden

    state = _systemctl("show", "--property=Version", "--value")
    if state.returncode or not state.stdout.strip():
        raise OSError("An isolated Hub requires the systemd user manager. "
                      "El Hub aislado necesita el gestor de usuario de systemd.")
    unit = f"{_UNIT_PREFIX}{secrets.token_hex(12)}.service"
    properties = [f"--property={key}={value}" for key, value in _SERVICE_PROPERTIES.items()]
    settings = [f"--setenv={key}={value}" for key, value in env.items()
                if key.startswith("HOARD_HUB_") or key in ("FAUSTUS_DIR", "TMPDIR", "TEMP", "TMP")]
    try:
        return run_hidden(["systemd-run", "--user", "--wait", "--pipe", "--collect",
                           f"--unit={unit}", f"--working-directory={os.getcwd()}",
                           *properties, *settings, "--", sys.executable, "-m", "hoard_link.hub",
                           "--isolated-unit", unit, *argv]).returncode
    except KeyboardInterrupt:
        _systemctl("stop", unit)
        return 130


def service_cgroup(unit):
    """Check the kernel and the manager, including when the internal flag is supplied directly."""
    if not unit.startswith(_UNIT_PREFIX) or not unit.endswith(".service") or "/" in unit:
        raise OSError("Invalid isolated Hub service unit. Unidad de servicio aislado no válida.")
    fields = ("MainPID", "ControlGroup", *_SERVICE_PROPERTIES)
    result = _systemctl("show", unit, f"--property={','.join(fields)}")
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    group = props.get("ControlGroup", "")
    actual = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    if (result.returncode or props.get("MainPID") != str(os.getpid()) or not group.startswith("/")
            or PurePosixPath(group).name != unit or f"0::{group}" not in actual
            or any(props.get(key) != value for key, value in _SERVICE_PROPERTIES.items())):
        raise OSError("The isolated Hub must be the main process of its transient systemd user service. "
                      "El Hub aislado debe ser el proceso principal de su unidad temporal de usuario.")
    return _CGROUP_MOUNT / group.lstrip("/")


_JOIN_CGROUP = """
import os, sys
with open(sys.argv[1], 'w') as membership:
    membership.write('0')
os.write(int(sys.argv[2]), b'1')
os.close(int(sys.argv[2]))
os.execve(sys.argv[3], sys.argv[3:], os.environ)
"""


class _LinuxLaunch:
    def __init__(self, group, process):
        self.group = group
        self.process = process

    def running(self):
        self.process.poll()  # reap the launcher even when its descendants are still running
        return "populated 1" in (self.group / "cgroup.events").read_text(encoding="ascii").splitlines()

    def close(self):
        (self.group / "cgroup.kill").write_text("1", encoding="ascii")
        deadline = monotonic() + 5
        while self.running():
            if monotonic() >= deadline:
                raise OSError("The fixture cgroup is still draining")
            sleep(0.01)
        self.group.rmdir()


class LinuxJobs:
    def __init__(self, group):
        self._group = group
        self._children = set()
        self._closed = False

    def launch(self, argv, *, cwd, env, log):
        from .._process import popen_hidden

        if self._closed:
            raise OSError("The Hub service group is closed")
        group = self._group / f"fixture-{secrets.token_hex(12)}"
        group.mkdir()
        process = None
        try:
            if not (group / "cgroup.kill").exists():
                raise OSError("Fixture cleanup requires cgroup.kill")
            read_fd, write_fd = os.pipe()
            try:
                try:
                    process = popen_hidden([sys.executable, "-I", "-S", "-c", _JOIN_CGROUP,
                                            str(group / "cgroup.procs"), str(write_fd), *argv],
                                           cwd=cwd, env=env, stdin=DEVNULL, stdout=log, stderr=log,
                                           pass_fds=(write_fd,))
                finally:
                    os.close(write_fd)
                if not select.select([read_fd], [], [], 5)[0] or os.read(read_fd, 1) != b"1":
                    raise OSError("The fixture could not enter its cgroup")
            finally:
                os.close(read_fd)
        except OSError:
            if process is not None:
                with suppress(OSError):
                    process.kill()
                    _LinuxLaunch(group, process).close()
            else:
                group.rmdir()
            raise
        child = _LinuxLaunch(group, process)
        self._children.add(child)
        return child

    def stop(self, child):
        child.close()
        self._children.discard(child)

    def close(self):
        self._closed = True
        for child in tuple(self._children):
            self.stop(child)
