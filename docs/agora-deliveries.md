# Reviewing a delivery across repositories

Use `--repo` once per repository when creating or updating a task. The first repository remains the primary one for older clients and for the default locks derived from `--paths`. For paths in other repositories, give `claim` an explicit `--lock path:<repository>/<path>` for each path. An update replaces the repository list. Pass `--repo=` to clear it.

```text
agora --as builder add "Pair of changes" --repo "Hoard Hub" --repo Faustus
agora --as builder update 42 --repo "Hoard Hub" --repo Faustus
agora --as builder submit 42 --summary-file delivery.md --repo-commits "Hoard Hub=1234567,89abcde" --repo-commits "Faustus=abcdef0"
agora --as builder submit 42 --summary-file rebased.md --repo-commits "Hoard Hub=7654321"
agora delivery diff 42 --summary
agora delivery diff 42
```

In MCP, `hub_agora_task_add` and `hub_agora_task_update` take `repositories` as an array of names. `hub_agora_task_submit` takes `commits_by_repo` as an object whose keys are registered repository names and whose values are arrays of Git commit hashes. The first grouped submit of a legacy delivery should list every repository. Later grouped submits replace only the groups supplied and create a new review revision. An empty array removes a group. A legacy submit with a nonempty `commits` list replaces the whole list and its group assignments.

Repository changes appear in the task's edit history (`agora --json task 42`) with their author, previous list and new list. The owner can undo the latest repository change with `agora update 42 --undo-repo REVISION`, using the revision printed by `update`. In MCP, pass back the `undo.arguments` returned by `hub_agora_task_update`. Undo records another edit and leaves other task fields as they are. To restore earlier hashes, submit those hashes in a new revision so the reviewers can review the restored delivery.

`hub_agora_review_diff` and the CLI report the repository for each commit. For legacy deliveries with no repository, or with a single repository and a flat commit list, the Hub checks its registered Git checkouts for commits that have no known location. It shows the commits it can locate and reports any unresolved ones. An identical commit found in several checkouts lists those repositories. If an abbreviation matches different commits, submit the full hash in a named repository group. Make missing commit objects available in a registered checkout before retrying.

In the Ágora console, select the task from **Trabajo** or the review column in **En directo**. Expand **Delivery diff** for the counts by repository, commit and file. Select **Show changes** and open a file to read the patch. The Hub's **Delivery review** shows the repository alongside the commits and retains line comments and guided review.

## Automatic worker receipt

When a managed turn ends, the console checks the submitted hashes in each registered repository. It looks for them in the main checkout, the worker's recorded checkout for that repository and the checkout for the branch declared in the delivery. It reads the worktree registry once per repository and checks for uncommitted files only in the worker's checkouts. If an older receipt has no matching branch or workspace and its commits are outside the main checkout, record the branch in a new submission to let the console verify it. A missing Git object is reported separately.

Verification runs in the background, including when a stopped turn needs a receipt. The Git checks share an eight-second deadline. If that expires, the worker receipt says the delivery is unverified and the task thread records the timeout. An already submitted task remains available for review.
