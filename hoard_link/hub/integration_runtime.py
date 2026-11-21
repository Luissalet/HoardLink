"""Compare the station's startup revision with the current local mainline."""

from __future__ import annotations

from subprocess import TimeoutExpired

from . import integration_git as git


def pending_reloads(repo, base, commit, build, apps):
    pending = [{"target": app["app"], "action": "reload_app", "reason": app.get("reason")}
               for app in apps if app["state"] == "deferred"]
    delta = git.changed(repo, base, commit)
    if any(path.startswith("hoard_link/hub/") and path.endswith(".py") for path in delta):
        pending.append({"target": "hub", "action": "restart_hub_when_idle"})
    if any(path.startswith("shell/") for path in delta):
        pending.append({"target": "hub_desktop", "action": "reopen_hub_window"})
    if build and build.get("native"):
        pending.append({"target": "agora_desktop", "action": "reopen_native_console"})
    if build and build["destination"] == "static/studio":
        pending.append({"target": "studio", "action": "verify_spark_deployment", "sha256": build["sha256"]})
    return pending


class IntegrationRuntime:
    def __init__(self, repo):
        self.repo = repo
        try:
            root = git.git_text(repo, "rev-parse", "--show-toplevel")
            self.loaded_commit = git.git_text(repo, "rev-parse", "HEAD") if repo.samefile(root) else None
        except (git.IntegrationError, OSError, TimeoutExpired):
            self.loaded_commit = None

    def status(self):
        if not self.loaded_commit:
            return None
        try:
            for branch in ("main", "master"):
                tip = git.git(self.repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
                if tip.returncode == 0:
                    main_commit = tip.stdout.decode().strip()
                    return {"loaded_commit": self.loaded_commit, "main_commit": main_commit,
                            "stale": main_commit != self.loaded_commit
                            and git.ancestor(self.repo, self.loaded_commit, main_commit)}
        except (git.IntegrationError, OSError, TimeoutExpired):
            pass
        return None
