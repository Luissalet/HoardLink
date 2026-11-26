# Isolated Hub for tests

Use a dedicated directory under the operating system’s temporary directory.
Put the Hub data, fixture roots and any `FAUSTUS_DIR` set by the test in that
directory. For example, use `<test>/hub` for `--data-dir` and list `<test>/apps`
under `roots` in `hub.json`. Start the Hub with `--port`, `--data-dir` and
`--no-window`. Use a free port other than 8810. The Hub validates the resolved
paths before scanning or binding its port.

The test catalog shows fixture metadata. The start, restart, group, session
startup, keep-alive, scheduler, rule, worker recovery and placement functions
are disabled in isolated mode. The fixture launcher is `hub_isolated_app`.
Send `action: start`, the registered `app` id and the temporary fixture path
as `worktree`. The fixture must declare a headless launcher. Its data, bearer
file and ports are assigned for that invocation. The tool returns the instance
id and an undo action. Use `action: stop` with that id to stop one fixture.

On Windows each fixture starts in a private nested Job Object. Closing the Hub
releases the enclosing Job and ends the descendants of every fixture, even after
its direct launcher has exited. An incompatible enclosing Job causes a launch
error. On Linux the Hub runs under `systemd-run --user` as the main process of a
transient service with control-group cleanup and restart disabled. Linux
fixtures use delegated cgroup v2 subgroups for individual stops. Start the Hub
from a session with a user manager available.
