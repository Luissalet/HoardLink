"""Keep installation-specific paths and private network names out of tracked text."""

from pathlib import Path
import re
import subprocess

import pytest


_SEPARATOR = r"(?:[\\/]|%2f|%5c)+"
_WINDOWS_PROFILE = re.compile(
    rf"[a-z]:{_SEPARATOR}users{_SEPARATOR}"
    r"""([^\\/\r\n\t"'\x60<>:*?|]+?)(?=[\\/\r\n\t"'\x60<>:*?|]|%2f|%5c|$)""",
    re.IGNORECASE,
)
_WINDOWS_SLUG = re.compile(r"\b[a-z]-{1,2}users-[\w.-]+", re.IGNORECASE)
_TAILNET = re.compile(r"(?<![\w-])([a-z0-9-]+)\.ts\.net\b", re.IGNORECASE)

# A shared Windows directory, documentation placeholders and named test fixtures.
_GENERIC_PROFILES = {"public", "example-user", "…"}
_PROFILE_FIXTURES = {
    "tests/commons/test_docs_archives.py": {"luis"},
    "tests/commons/test_docs_sniff.py": {"luis"},
    "tests/commons/test_media_ffmpeg.py": {"me"},
    "tests/commons/test_paths.py": {"me", "someone", "ñ"},
    "tests/hub/test_toast_identity.py": {"a b", "a%20b"},
    "tests/vectors/paths.json": {"luis"},
}
_SLUG_FIXTURES = {"c--users-alice-desktop-modelos", "c-users-alice-desktop-faustus"}
_TAILNET_FIXTURES = {"example", "tail1", "tail1234", "tailnet", "x"}


def _findings(path: str, content: str):
    allowed_profiles = _GENERIC_PROFILES | _PROFILE_FIXTURES.get(path, set())
    for number, line in enumerate(content.splitlines(), 1):
        if any(m[1].casefold() not in allowed_profiles for m in _WINDOWS_PROFILE.finditer(line)):
            yield number, "Windows profile path"
        if any(m[0].casefold() not in _SLUG_FIXTURES for m in _WINDOWS_SLUG.finditer(line)):
            yield number, "Windows profile path slug"
        if any(m[1].casefold() not in _TAILNET_FIXTURES for m in _TAILNET.finditer(line)):
            yield number, "private network domain"


def test_tracked_text_does_not_identify_an_installation():
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("requires the Git index")
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    findings = []
    for entry in filter(None, paths.split(b"\0")):
        name = entry.decode("utf-8")
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            content = data.decode("utf-16", errors="replace")
        elif b"\0" in data:
            continue
        else:
            content = data.decode("utf-8", errors="replace")
        findings.extend(f"{name}:{line}: {kind}" for line, kind in _findings(name, content))
    assert not findings, "\n" + "\n".join(findings)


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("file:///D:/Users/" + "Account Name/Documents", "Windows profile path"),
        (r"E:\\USERS\\" + r"account\\Desktop", "Windows profile path"),
        ("C:%5cUsers%5c" + "Account%20Name%5cDesktop", "Windows profile path"),
        ("c--Users-" + "account-Desktop", "Windows profile path slug"),
        ("https://worker." + "unlisted-network" + ".ts.net:1234", "private network domain"),
        ("unlisted-network" + ".TS.NET", "private network domain"),
    ],
)
def test_installation_patterns(content, kind):
    assert list(_findings("example.txt", content)) == [(1, kind)]


def test_fictional_examples_are_explicitly_allowed():
    path = "C:/Users/" + "Luis/Documents"
    assert list(_findings("tests/vectors/paths.json", path)) == []
    assert list(_findings("example.txt", path)) == [(1, "Windows profile path")]
    assert list(_findings("example.txt", "https://worker.example.ts.net")) == []
