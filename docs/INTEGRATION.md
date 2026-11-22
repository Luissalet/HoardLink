# Integration station

The Hub's integration station takes a `code_branch` and produces `integrated_code`. It compares focused tests on
the main checkout's committed tree and on the exact proposed merge. The Agora task stays approved until the
post-merge check passes. The results and pending reloads appear in System → Integrations.

## Prepare a delivery

Use a named local branch and submit its tip and any other commits for review. For a production job that delivers
`code_branch`, or an Agora task labelled `integration`, put the relevant tracked pytest files in the submission
summary. Include existing tests that cover the delivery, even when their contents are unchanged.
For a single repository, include the repository in the task or submission. For several repositories, use the
same branch name in each registered repository and include every branch tip in the submission.

```text
Repository: Hoard Hub
Test-Paths: tests/hub/test_integration.py
Live-Test: tests/hub/test_integration.py::test_integrate_and_undo_exact_approved_delivery
```

Agora returns incomplete integration submissions to the worker before publishing a review request. The station
reads `Test-Paths` from the submitted revision, including after handover. The line accepts pytest files
separated by commas or whitespace following each `.py` path. Use commas in new submissions. The integrator can
add existing tests with `test_paths` or `test_paths_by_repo`. Each file must be tracked in the delivered commit.
Historic deliveries without declarations use the affected tests if the station can infer them.
For a multirepository task, prefix each path with the registered repository name, for example
`Test-Paths: First Repo/tests/test_first.py, Second Repo/tests/test_second.py`. Existing unqualified paths
are matched to branch contents. A path absent from every delivery branch is reported by name.
Use one `Live-Test:` line for each repository's focused case. Prefix each line's path with the registered repository
name in multirepository deliveries. Plans report `missing_live_tests` for repositories without an explicit case
and `post_merge_checks` for the planned test selection. When there is no explicit case, the station repeats
the selected contract on the merged tree and checks for failures relative to the tested candidate.

## Tools

Use the usual Hub agent interface or `hub.cmd tool <name> --args-file <file>`. Pass the task id, its observed
`revision`, your `agent` identity and `space_id`. For an old task without a repository, the station looks for a
registered repository whose local delivery branch tip appears in the submitted hashes. In a multirepository
plan, `repo` selects a repository to inspect. A run processes every repository in order, with `repo` first
when supplied. Set `live_tests` to an object keyed by registered repository name with the focused pytest node for
each repository. This also works for a single repository. Use `spark_repos` to specify remote checkouts.

| Tool | Operation |
|---|---|
| `hub_integration_plan` | Resolve the submitted head and list affected test files and repositories missing a live case. |
| `hub_integration_run` | Compare main and delivery. With `merge: false`, it also checks a delivery that main already contains. With `merge: true`, integrate the approved revision and close it in Agora after the post-merge check. Add `install_dependencies: true` to update stale dependencies before that check. The submitted `Live-Test:`, `live_test` or `live_tests` selects a narrower check. |
| `hub_integration_status` | Read a receipt and its current state by `id`. |
| `hub_integration_suite_failures` | Read the measured suite failures and observation dates. Pass `repo` to filter by repository name. |
| `hub_integration_reconcile` | Correct a historical fast-forward after the registered main branch has the equivalent merge. Supply the receipt `id`, `agent`, `space_id`, registered `repo`, merge `commit` and `reason`. A correction returns the undo call. |
| `hub_integration_undo` | Cancel a queued or testing run. For a completed merge, restore the previous checkout, baseline and task state while the recorded head and revision still match. |

The owner or a higher-ranked agent can integrate the exact approved revision. The assigned reviewer can plan and
compare a delivery without merging it. Integrations wait for the repository's Agora merge lock and resolve the
mainline, selections and test budget again once they hold it. A finish records the exact approved revision and merged commit in the original task thread and
releases its locks. A multirepository run keeps the task open until all repositories pass. Its common receipt
contains each comparison, live case, merge hash and cleanup. On a regression, the station attempts to restore
any repositories it already moved. The receipt's undo restores completed repositories in reverse order.
The task retains its owner. Production gates remain a decision for the person.

For repositories with a [measured suite baseline](SUITE_BASELINE.md), failures in submitted
`Test-Paths` are compared by case identifier with that repository's recorded failures. A known
failure appears in the receipt and closing message. A new failure in those files blocks closure.
The other selected tests retain the main-versus-delivery checks, including lost coverage.
For a manual closure, run the integration check for the submitted revision. Agora uses its
receipt only when it covers the declared focal files and the tested tree matches the commits
being closed. A collection failure or missing verification leaves the task open.

A delivery that main already contains, merged by hand or before the station loaded, is checked with
`hub_integration_run` and `merge: false`. The plan's `published_commit` is the first commit on main's
first-parent history that contains the submitted tip, and `base` is the mainline commit before it. The station
tests that exact tree against that base in isolated worktrees, so later commits on main and uncommitted changes
in the registered checkout do not affect the result. The receipt shows the checked published commit. Close the task
with Agora `done` and that commit: known failures in the declared tests are reported, a new one leaves the task
open. The submitted branch must still exist, and `merge: true` still refuses a delivery already on main.

Runs that share a target checkout wait in submission order and recompute their plans against the current mainline
when their turn starts. After moving the registered checkout, the station checks its local dependencies before
reloading apps and running the post-merge test. The receipt's `dependencies` shows the specification changes,
missing or mismatched packages, and the exact commands and working directory for the integrator.
The npm check uses each tracked `package-lock.json`, the installed `node_modules/.package-lock.json`, and the
package versions on disk. The Python check uses `requirements-lock.txt` or `requirements.txt`, their tracked
local includes, and the default dependencies in `pyproject.toml`. It evaluates requirements in the registered
checkout's `.venv` or `venv`. A preflight failure can report the proposed merge's dependency checks in
`dependency_preflight`. Commands in that diagnostic apply to the proposed specifications.

Dependency installation is opt-in on an approved merge. The station uses `npm install --no-save` or the
app interpreter's `-m pip install`, records the installer output and the installed version changes, and checks the
environment again. It can bootstrap pip with `ensurepip` in an existing venv. If the venv is missing, the
receipt gives provisioning commands. Installation is deferred while an app from the checkout is running or
there are local manifest edits. The checkout and task can be restored with the integration's undo. Package
manager changes made before rollback or undo remain in the environment. The receipt's `undo_dependencies`
shows the restored checkout's requirements and how to bring its environment up to date.

The station reloads running local apps launched from the merged checkout
before checking the original case. Running apps that open their own desktop window and apps with an open Hub
window are left running. The receipt records `desktop_in_use` and System → Integrations shows the deferral.
`app_reload` also records readiness and other deferral reasons, including app leases and work in progress.
A reload failure triggers checkout rollback. Undo applies the same rules and records `undo_app_reload`.
Apps stopped before integration retain their on-demand startup behavior. The receipt's `reload_pending` lists
deferred app reloads, a Hub restart after workers have stopped, desktop windows that need reopening and Studio
deployments to verify on the Spark. It describes the state at completion of the run.

When browser source or its build inputs change, the station uses `npm ci` and `npm run build` on the exact
merge candidate before moving the registered checkout. It publishes the console under `apps/agora/dist` and
the Studio bundle under `static/studio` in their respective checkouts before the live case. Build failure
leaves the checkout and the served bundle in place. The receipt's `frontend_build` records the entry hash
and log. Rollback and undo restore the previous assets. A Studio served from a Spark still needs deployment
to that host. Compare `/api/version` → `served_studio.bundle.sha` on the serving host with the built entry
and open the destination through the Hub to verify it. `/agora/` on the Hub itself serves the team console.

After a successful run, the station can remove a delivery worktree containing only ignored build outputs in
`dist`, dependencies in `node_modules`, TypeScript build metadata and Python bytecode. Tracked changes, other
ignored files, untracked files or linked entries retain the worktree.

On Windows, changes to `apps/agora/src-tauri`, the console's web build inputs, or the Hub's session endpoints
and console launcher trigger `npm run desktop:build -- -- --locked` on the same merge candidate. The native
build produces the console's browser bundle and the executable. Integration publishes both before the live
case. It updates `apps/agora/bin/hoard-agora.exe`, the target of the existing Windows shortcut. The receipt
records the executable's SHA-256 under `frontend_build.native`. A running console keeps its loaded version
until the person closes it and opens it again. Integration never closes that window. If Windows is holding the
old executable open, the station renames it before placing the new one and removes the retired file on a later
update. Rollback and undo restore the prior browser assets and executable. Use a Windows integration host with
Node, Rust and the MSVC build tools to publish the native console.

For a manual integration, use Agora `done` with the published merge hashes and the verification note in
`--result-file`. An agent other than the owner can close the approved delivery this way. The Hub checks that
each merge is on a registered mainline, merges the approved tip, has the clean Git merge tree, and together
they account for all approved commits in the current revision. The task retains its owner and reviewer.
The thread records who closed it, the note, the reviewed hashes and the verified merge hashes.
The agent tool returns an undo call for the verifier. It restores the task's approved state, its thread
and any still-current leases released by the closure. The published Git merge stays on the mainline.

Any agent can reconcile a historical fast-forward with a published merge after verifying it. The tool checks
both parents and the Git merge tree against the receipt. It records the verifying agent and reason in the
receipt and the original task thread, preserving the review, resolver and original test and checkout history.
The correcting agent, the task owner or someone above the owner can apply the receipt's undo call.

The station compares the Hub revision it loaded at startup with the local `main` or `master` branch. When
the loaded revision is an ancestor of the current mainline, Integrations shows both commits and reports that
the Hub is running the earlier version. Agent tools return the same information in `runtime`.

## Test selection and baseline

The station always selects changed `test_*.py` files and those named in the delivery. It adds tests named after a
changed module. For non-obvious dependencies, place a `.hoard-integration.json` map on the main branch:

```json
{
  "paths": {
    "hoard_link/hub/ui/*.js": ["tests/hub/test_ui.py"]
  },
  "inference": ["tests/test_live_model.py"],
  "contract_timeout_seconds": 600
}
```

Use tests registered on main to define the shared map. Each comparison runs the selected files that exist at each
revision. The closest tracked `conftest.py` below the repository root determines a test's group. Files without
one are grouped by their top-level directory. Groups larger than 40 files run in sequential batches of up to
40 files, with the same batches on main and the candidate. Each pytest process can use its own `conftest`
imports. Matching test identities and assertion signatures within a group are known failures. The signature
normalizes Python object addresses and the checkout, pytest and isolated Hub data paths for that run.
A changed signature is a new failure. Each repository and runner retains its own baseline
after integration. Files with known failures are included in later comparisons while tracked. The new baseline
records failures observed on the new mainline, so repaired or removed failures drop out. The receipt includes
the elapsed seconds per group, aggregate results, the baseline commit and the post-merge result.

The default lane excludes `inference`, `live_model` and `gpu` markers and any inference file in the map.
Pass `inference: true` to run that lane explicitly after arranging the model/GPU resources required by the tests.
The station runs focused pytest files with one local suite slot shared across its Hub instances.
`contract_timeout_seconds` is a positive integer from the repository's committed mainline. It defaults to
300 seconds and applies to each pytest process for the baseline, candidate and post-merge case, locally or over
SSH. The explicit inference lane keeps its own 300-second process limit. The station starts test subprocesses
with independent Agora and Hub connection settings.

To use a Spark, set `spark_repo` to an existing checkout on the Hub's configured SSH node. The checkout must
already have the main commit and its pytest environment. The runner tests in a temporary remote worktree, sends the
proposed diff via SSH, and removes that worktree when finished. Local and Spark comparisons report their own runner.

## Preserving changes

The station first rehearses the merge in an isolated worktree. When the target checkout has overlapping edits, it
rehearses the staged and unstaged layers separately and journals the patches before moving the target branch.
An overlap it cannot replay leaves the checkout in place and identifies the conflicting paths. Changes outside
the overlap stay in the working checkout.
The original case runs in a fresh checkout after the move. A failed case returns the target to the earlier commit.

Every integration creates a merge commit, including when the delivery descends directly from main. Its subject
includes the subject of the delivery tip.
A receipt distinguishes a failed preflight, an interrupted
checkout, a rollback and a completed integration. The recovery patches and JUnit reports are under the Hub data
directory in `integration/<receipt>/`. If an interruption occurred between checkout steps, use the last recorded
event and the parked patches to recover the stored index and working changes.
The station captures, stores and applies Git patches as bytes. For manual recovery on Windows, use
`git diff --binary --full-index --output=<file>` to save a patch and `git apply <file>` to replay it.
Windows PowerShell text redirection can write a UTF-16 file that Git cannot apply.
