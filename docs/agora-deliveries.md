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

To restore a task's previous repository list, update it with the previous values. To restore earlier hashes, submit those hashes in a new revision so the reviewers can review the restored delivery.

`hub_agora_review_diff` and the CLI report the repository for each commit. For a task with no repository or assignments, the Hub looks up each commit in its registered Git checkouts. It shows the commits it can locate and reports any unresolved ones. An identical commit found in several checkouts lists those repositories. If an abbreviation matches different commits, submit the full hash in a named repository group. Make missing commit objects available in a registered checkout before retrying.

In the Ágora console, select the task from **Trabajo** or the review column in **En directo**. Expand **Delivery diff** for the counts by repository, commit and file. Select **Show changes** and open a file to read the patch. The Hub's **Delivery review** shows the repository alongside the commits and retains line comments and guided review.
