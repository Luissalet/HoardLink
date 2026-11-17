# Executors (`executors`)

Run the command-line agents installed on this PC on behalf of a Faustus that lives elsewhere. Module `hoard_link/hub/executors.py`; no page of its own.

A Faustus on a Spark has no `claude` or `codex` of its own: those programs and the sign-in they keep are on the PC. Faustus reaches the Hub through
the link tunnel, and the Hub starts the program here. How an agent is invoked stays in Faustus (it sends the command line from its own table of
runners), so a new runner there needs nothing in the Hub.

Only the hub's page or the hub's own bearer token may use it; another app's token gets 403.

| Route | Does |
|---|---|
| `GET /api/executors?names=claude,codex` | `{host, executors: {name: {installed, path}}}`: which executables are on this PC's PATH (names are plain file names) |
| `POST /api/executors/runs` | body `{argv, stdin?, cwd?, env?, timeout_s?, label?}`; the program is `argv[0]` found on PATH, `stdin` is written to it, `cwd` must be a folder on this PC (without one the run gets its own under `<data>/executors/work/<id>`), `env` is added to the Hub's environment. Answers `{id, host, program, cwd}` |
| `GET /api/executors/runs` | recent runs with status and exit code |
| `GET /api/executors/runs/<id>?since=<offset>` | `{status: running\|done\|error\|cancelled\|timeout, exit_code, seconds, output, offset, more}`; call again with the returned `offset` to read only what is new |
| `POST /api/executors/runs/<id>/cancel` | stops the run and its whole process tree |

Output (stdout and stderr together) goes to `<data>/executors/<id>.log`. At most eight runs go at once; a run that outlives its `timeout_s`
(default 30 minutes) is killed.

## Tools

`hub_executors` (what is installed), `hub_executor_run`, `hub_executor_status` (one run, or the recent list without `run_id`), `hub_executor_cancel`.
