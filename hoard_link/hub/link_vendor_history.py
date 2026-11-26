"""Read the canonical Git history without updating the checkout or fetching objects."""

from dataclasses import dataclass
import hashlib
import re
import time

from .repos import run_git


@dataclass(frozen=True)
class VendorHistory:
    algorithm: str
    versions: dict[str, int]
    complete: bool

    def blob_id(self, content: bytes) -> str:
        digest = hashlib.new(self.algorithm)
        digest.update(f"blob {len(content)}\0".encode("ascii"))
        digest.update(content)
        return digest.hexdigest()


def published_blobs(canonical, relative, source, deadline):
    """Index consecutive content revisions from the canonical first-parent history."""
    if not (canonical / ".git").exists():
        return None

    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = run_git(str(canonical), "--no-replace-objects", "-c", "protocol.allow=never", *args,
                         timeout=remaining)
        return result.out if result.ok else None

    info = (git("rev-parse", "--show-object-format", "--is-shallow-repository") or "").splitlines()
    if len(info) != 2 or info[0] not in ("sha1", "sha256") or info[1] not in ("true", "false"):
        return None
    output = git("log", "--first-parent", "--diff-merges=first-parent", "--raw", "--no-abbrev", "--no-renames",
                 "--no-ext-diff", "--no-textconv", "--format=", "HEAD", "--", relative)
    if output is None:
        return None
    algorithm, shallow = info
    size = 40 if algorithm == "sha1" else 64
    pattern = re.compile(rf":[0-7]+ [0-7]+ [a-f0-9]{{{size}}} ([a-f0-9]{{{size}}}) [A-Z]\t")
    versions = {}
    history = VendorHistory(algorithm, versions, shallow == "false")
    last = None
    lag = 0
    for line in output.splitlines():
        if not line:
            continue
        match = pattern.match(line)
        if match is None:
            return None
        blob = match[1]
        if blob == last or blob == "0" * size:
            continue
        if last is None and blob != history.blob_id(source):
            return None
        versions.setdefault(blob, lag)
        last = blob
        lag += 1
    return history if versions else None
