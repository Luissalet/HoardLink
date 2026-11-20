# Integration station

The Hub's integration station takes a `code_branch` and produces `integrated_code`. It compares focused tests on
the main checkout's committed tree and on the exact proposed merge, runs the original case after merging, and
leaves a receipt in System → Integrations. The Agora task stays approved until the original case passes.

## Prepare a delivery

Use a named local branch and submit its tip and any other commits for review. For a production job that delivers
`code_branch`, or an Agora task labelled `integration`, put the changed test paths in the submission summary.
For a single repository, include the repository in the task or submission. For several repositories, use the
same branch name in each registered repository and include every branch tip in the submission.

```text
Repository: Hoard Hub
Test-Paths: tests/hub/test_integration.py
```

Agora returns incomplete integration submissions to the worker before publishing a review request. The station
reads `Test-Paths` from the submitted revision, including after handover. The line accepts pytest files
separated by commas or whitespace following each `.py` path. Use commas in new submissions. When a historic
submission has no line, the integrator can pass a `test_paths`
array to plan or run. Those files must be tracked in the delivered commit and may be existing tests.
For a multirepository task, prefix each path with the registered repository name, for example
`Test-Paths: First Repo/tests/test_first.py, Second Repo/tests/test_second.py`. Existing unqualified paths
are matched to branch contents. `test_paths_by_repo` supplies paths for repositories without declared tests.

## Tools

Use the usual Hub agent interface or `hub.cmd tool <name> --args-file <file>`. Pass the task id, its observed
`revision`, your `agent` identity and `space_id`. For an old task without a repository, the station looks for a
registered repositories whose local delivery branch tips appear in the submitted hashes. In a multirepository
plan, `repo` selects a repository to inspect. A run processes every repository in order, with `repo` first
when supplied. Set `live_tests` to an object keyed by repository name with the focused pytest node for each
repository. Use `spark_repos` to specify remote checkouts per repository.

| Tool | Operation |
|---|---|
| `hub_integration_plan` | Resolve the submitted head and list affected test files. |
| `hub_integration_run` | Compare main and delivery. With `merge: true`, integrate the approved revision and close it in Agora. Supply `live_test`, a selected test file or a pytest node ID that reproduces the original case. |
| `hub_integration_status` | Read a receipt and its current state by `id`. |
| `hub_integration_undo` | Cancel a queued or testing run. For a completed merge, restore the previous checkout, baseline and task state while the recorded head and revision still match. |

The owner or a higher-ranked agent can integrate the exact approved revision. The assigned reviewer can plan and
compare a delivery without merging it. The integrator holds the repository's Agora merge lock during the checkout
change and finish. A finish records the exact approved revision and merged commit in the original task thread and
releases its locks. A multirepository run keeps the task open until all repositories pass. Its common receipt
contains each comparison, live case, merge hash and cleanup. On a regression, the station attempts to restore
any repositories it already moved. The receipt's undo restores completed repositories in reverse order.
The task retains its owner. Production gates remain a decision for the person.

## Test selection and baseline

The station always selects changed `test_*.py` files and those named in the delivery. It adds tests named after a
changed module. For non-obvious dependencies, place a `.hoard-integration.json` map on the main branch:

```json
{
  "paths": {
    "hoard_link/hub/ui/*.js": ["tests/hub/test_ui.py"]
  },
  "inference": ["tests/test_live_model.py"]
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
The station runs focused pytest files with a time limit and one local suite slot shared across its Hub instances.
It starts test subprocesses with independent Agora and Hub connection settings.

To use a Spark, set `spark_repo` to an existing checkout on the Hub's configured SSH node. The checkout must
already have the main commit and its pytest environment. The runner tests in a temporary remote worktree, sends the
proposed diff via SSH, and removes that worktree when finished. Local and Spark comparisons report their own runner.

## Preserving changes

The station first rehearses the merge in an isolated worktree. When the target checkout has overlapping edits, it
rehearses the staged and unstaged layers separately and journals the patches before moving the target branch.
An overlap it cannot replay leaves the checkout in place. Changes outside the overlap stay in the working checkout.
The original case runs in a fresh checkout after the move. A failed case returns the target to the earlier commit.

Non-fast-forward merge commits include the subject of the delivery tip. The station removes the merged delivery
worktree and branch when they are clean. When there are uncommitted or ignored files, it keeps both and shows
the reason in the integration state and receipt. A receipt distinguishes a failed preflight, an interrupted
checkout, a rollback and a completed integration. The recovery patches and JUnit reports are under the Hub data
directory in `integration/<receipt>/`. If an interruption occurred between checkout steps, use the last recorded
event and the parked patches to recover the stored index and working changes.
The station captures, stores and applies Git patches as bytes. For manual recovery on Windows, use
`git diff --binary --full-index --output=<file>` to save a patch and `git apply <file>` to replay it.
Windows PowerShell text redirection can write a UTF-16 file that Git cannot apply.
