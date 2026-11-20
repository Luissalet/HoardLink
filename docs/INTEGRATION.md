# Integration station

The Hub's integration station takes a `code_branch` and produces `integrated_code`. It compares focused tests on
the main checkout's committed tree and on the exact proposed merge, runs the original case after merging, and
leaves a receipt in System → Integrations. The Agora task stays approved until the original case passes.

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
separated by commas or whitespace following each `.py` path. Use commas in new submissions. When a historic
submission has no line, the integrator can pass a `test_paths`
array to plan or run. Those files must be tracked in the delivered commit and may be existing tests.
For a multirepository task, prefix each path with the registered repository name, for example
`Test-Paths: First Repo/tests/test_first.py, Second Repo/tests/test_second.py`. Existing unqualified paths
are matched to branch contents. `test_paths_by_repo` supplies paths for repositories without declared tests.
Use one `Live-Test:` line for each repository's focused case. Prefix each line's path with the registered repository
name in multirepository deliveries. Plans report `missing_live_tests` for repositories still awaiting a focused case.

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
| `hub_integration_run` | Compare main and delivery. With `merge: true`, integrate the approved revision and close it in Agora. Use the submitted `Live-Test:` or pass `live_test` or `live_tests` for an explicit override. |
| `hub_integration_status` | Read a receipt and its current state by `id`. |
| `hub_integration_reconcile` | Correct a historical fast-forward after the registered main branch has the equivalent merge. Supply the receipt `id`, `agent`, `space_id`, registered `repo`, merge `commit` and `reason`. A correction returns the undo call. |
| `hub_integration_undo` | Cancel a queued or testing run. For a completed merge, restore the previous checkout, baseline and task state while the recorded head and revision still match. |

The owner or a higher-ranked agent can integrate the exact approved revision. The assigned reviewer can plan and
compare a delivery without merging it. The integrator holds the repository's Agora merge lock during the checkout
change and finish. A finish records the exact approved revision and merged commit in the original task thread and
releases its locks. A multirepository run keeps the task open until all repositories pass. Its common receipt
contains each comparison, live case, merge hash and cleanup. On a regression, the station attempts to restore
any repositories it already moved. The receipt's undo restores completed repositories in reverse order.
The task retains its owner. Production gates remain a decision for the person.

Runs that share a target checkout wait in submission order and recompute their plans against the current mainline
when their turn starts. After moving a checkout, the station reloads running local apps launched from that checkout
before checking the original case. `app_reload` records readiness or the reason the restart was deferred, including
app leases and work in progress. A reload failure triggers checkout rollback. Undo records `undo_app_reload` for
apps using the restored checkout. Apps stopped before integration retain their on-demand startup behavior.

After a successful run, the station can remove a delivery worktree containing only ignored build outputs in
`dist`, dependencies in `node_modules`, TypeScript build metadata and Python bytecode. Tracked changes, other
ignored files, untracked files or linked entries retain the worktree.

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
imports. Matching test identities and assertion signatures within a group
are known failures. A changed signature is a new failure. Each repository and runner retains its own baseline
after integration. A receipt carries group and aggregate results, the baseline commit and the original case result.

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
An overlap it cannot replay leaves the checkout in place. Changes outside the overlap stay in the working checkout.
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
