# Test suite baselines

Use `scripts/suite_baseline.py` to run test suites from isolated worktrees and
record the Git commit, command, elapsed wall time, exit code and failing cases.
The registry JSON and the Markdown report alongside it belong outside the
repositories.

Prepare each worktree at the local primary branch tip or the specific commit
being compared. Install the test dependencies for the application. Configure
its test profile, data paths and ports before running its suite.

The JSON file contains a `repositories` array. Each repository has a `name`,
the absolute path of its worktree root in `cwd`, the expected Git revision in
`ref`, and a list of `batches` in execution order. A full run executes every
configured batch. Use one batch for an ordinary suite and disjoint test
selectors for a suite that needs to run in parts.

```json
{
  "repositories": [
    {
      "name": "Example",
      "cwd": "C:/worktrees/example",
      "ref": "refs/heads/main",
      "env": {"HOARD_HUB_AUTOSTART": "0", "PYTEST_ADDOPTS": null},
      "batches": [
        {
          "name": "unit",
          "command": [
            "C:/test-env/Scripts/python.exe", "-m", "pytest", "tests/unit",
            "-p", "no:cacheprovider", "--basetemp={artifacts}/tmp", "--junitxml={report}"
          ]
        },
        {
          "name": "integration",
          "command": [
            "C:/test-env/Scripts/python.exe", "-m", "pytest", "tests/integration",
            "-p", "no:cacheprovider", "--basetemp={artifacts}/tmp", "--junitxml={report}"
          ]
        }
      ]
    }
  ]
}
```

Commands are argument arrays. Add `--junitxml={report}` for pytest or
`--test-reporter=junit --test-reporter-destination={report}` for the Node test
runner. A command that wraps another test harness can write its results to the
same JUnit path. `{artifacts}` is a fresh external directory for that attempt
and `{repo}` is the worktree root. The optional batch `cwd` is relative to
that root. Batch `env` values override repository `env` values, and a JSON
`null` removes an inherited environment variable. Supply settings and paths
here, since the expanded values are stored in the registry.
Set these overrides per repository or batch. Use fresh data paths and reserved
test ports. Preserve behavior exercised by the suite, such as event delivery
or discovering the default companion address. Supply an isolated companion
when a test exercises an available service.

```powershell
python scripts/suite_baseline.py C:/qa/lineabase.json
python scripts/suite_baseline.py C:/qa/lineabase.json --repo Example
python scripts/suite_baseline.py C:/qa/lineabase.json --repo Example --batch integration
python scripts/suite_baseline.py C:/qa/lineabase.json --repo Example --resume
```

The runner executes one batch at a time and updates the JSON and Markdown as
each batch finishes. A named batch can be rerun separately. Other results are
retained when the commit and each result's batch definition still match.
An invocation running all batches of a repository replaces the previous set
for that repository. `--resume` keeps completed batches with matching
definitions from the exact same commit and reruns missing or incomplete ones.
The registry lock prevents concurrent runs using that
file. If a run is killed, check the PID in the adjacent `.lock` file and
remove the lock only after the process and its tests have exited.

To compare revisions, keep the original registry and use a second registry for
the new worktree and pinned commit. Define the batches you want to repeat or
measure there. State the commit for each group of results when reporting a run
that spans revisions. Keep the expanded environment from each receipt so a
change in test settings remains visible in the comparison.

The Node reporter writes both individual test cases and aggregate counters
into its JUnit XML. The runner uses those totals to include nested tests and
records TODOs as skipped and cancellations as errors. A batch with zero
tests is incomplete, including when the command exits successfully.
For pytest, when the failure, error and
skip totals agree with the case records, the runner also counts successful
subtests that pytest included only in its suite total.
If a case has multiple outcomes, a setup or teardown error takes precedence
and each failure or error diagnostic is retained. A complete clean run exits
with status 0. A measured failure, startup error, missing report or
inconsistent result exits with status 1. A batch's `result_exit_codes` may
override the default completion codes 0 and 1 if its harness documents
different nonzero codes for complete runs with failures. A selected batch can
succeed while its repository still has pending batches. Inspect the registry
for that distinction and use the saved log and XML for triage.

## Baseline failures in the Hub

System → Integrations shows the recorded failures by repository. Agents can read
the same records with `hub_integration_suite_failures`, optionally passing a
repository name in `repo`.

Set `HOARD_HUB_SUITE_BASELINE` to the external registry JSON and, optionally,
`HOARD_HUB_SUITE_COMPARISON` to the published/local comparison CSV. The standard
development layout discovers `Research/_qa/lineabase/lineabase.json` and
`Research/_qa/publicado3733/comparativa.csv` in an ancestor of the Hub checkout.
Isolated Hub instances use explicit paths. The Hub stores the last imported
snapshot under its own data directory and reads updated receipts as they arrive.
Reading the baseline does not run tests.

The registry's batch start time dates each failure observation. A conclusive
local comparison updates the matching historical case and its commit, or clears
it when the test passes. Skipped, absent and blocked cases retain the historical
observation. A partial suite can also include failures from older batches
confirmed by the comparison associated with that suite. Dates absent from the
receipts are shown as undated. A partial suite appears as an incomplete measurement
even if some of its batches produced results.
