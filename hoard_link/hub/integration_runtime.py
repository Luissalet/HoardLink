"""Compare the station's startup revision with the current local mainline."""

from __future__ import annotations

from subprocess import TimeoutExpired

from . import integration_git as git


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
