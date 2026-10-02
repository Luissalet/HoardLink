"""hoard_link.paths: unsafe folders/files/output dirs, Windows paths on any OS, pasted-path cleaning, zip-slip."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from hoard_link import paths
from tests.commons.jsrun import load_vectors

CASES = load_vectors("paths")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{c['args'][0][:50]}:{c['opts'].get('data_dir', '')[-8:]}")
def test_vectors(case):
    if case["platform"] == "posix" and os.name == "nt":
        pytest.skip("POSIX path on Windows is simply relative")
    if case["platform"] == "win" and os.name == "nt" and case["expect"] is None:
        pytest.skip("Windows path that is allowed by policy: the folder does not exist on this machine")
    assert getattr(paths, case["fn"])(*case["args"], **case["opts"]) == case["expect"]


# ------------------------------------------------------------------ clean_user_path

@pytest.mark.parametrize("raw,expect", [
    ('"C:\\My Folder\\file.txt"', "C:\\My Folder\\file.txt"),
    ("  'C:\\a b'  ", "C:\\a b"),
    ('\u201cC:\\Users\\Ñ\u201d', "C:\\Users\\Ñ"),
    ('""C:\\x""', "C:\\x"),
    ("  /tmp/some dir  ", "/tmp/some dir"),
    ('"', '"'),
    ("", ""),
    (None, ""),
    ('"C:\\it\'s fine"', "C:\\it's fine"),
    ("C:\\no\\quotes", "C:\\no\\quotes"),
    ('"half', '"half'),
])
def test_clean_user_path(raw, expect):
    assert paths.clean_user_path(raw) == expect


def test_clean_expands_home_and_variables(monkeypatch):
    monkeypatch.setenv("HL_TEST_DIR", "/data/x")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\someone")
    assert paths.clean_user_path("$HL_TEST_DIR/a") == "/data/x/a"
    assert paths.clean_user_path("%HL_TEST_DIR%/a") == "/data/x/a"
    assert paths.clean_user_path("%UNDEFINED_HL_VAR%\\a") == "%UNDEFINED_HL_VAR%\\a"
    assert paths.clean_user_path('"~/docs"') == os.path.expanduser("~/docs")


# ------------------------------------------------------------------ real filesystem

def test_real_folder_ok_missing_and_file(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (tmp_path / "a.txt").write_text("x")
    assert paths.unsafe_folder(folder, lang="en") is None
    assert paths.unsafe_folder(f'"{folder}"', lang="en") is None                       # pasted with quotes
    assert paths.unsafe_folder(tmp_path / "nope", lang="en") == "the folder does not exist"
    assert paths.unsafe_folder(tmp_path / "a.txt", lang="en") == "that path is a file, not a folder"
    assert paths.unsafe_folder(folder) == "ok" or paths.unsafe_folder(folder) is None


def test_default_language_is_spanish_and_en_is_available(tmp_path):
    assert paths.unsafe_folder("relative", lang="es") == "la ruta debe ser absoluta"
    assert paths.unsafe_folder("relative") == "la ruta debe ser absoluta"
    assert paths.unsafe_folder("relative", lang="en") == "the path must be absolute"
    assert paths.unsafe_folder("relative", lang="fr") == "the path must be absolute"
    assert paths.reason("path_root", "es").startswith("la raíz")
    assert paths.reason("nope") == "nope"
    assert set(paths.REASONS["path_system"]) == {"es", "en"}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_into_a_system_folder_is_seen_through(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to("/etc")
    assert paths.unsafe_folder(link, lang="en") == "system folders are not allowed"


def test_home_folder_itself_is_refused(monkeypatch, tmp_path):
    home = tmp_path / "me"
    home.mkdir()
    (home / "Documents").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert paths.unsafe_folder(home, lang="en") == "the user profile folder is too broad: pick a subfolder"
    assert paths.unsafe_folder(home / "Documents", lang="en") is None
    assert paths.unsafe_folder("~", lang="en") == "the user profile folder is too broad: pick a subfolder"


@pytest.mark.skipif(os.name == "nt", reason="POSIX layout")
def test_root_user_folder_is_refused():
    assert paths.unsafe_folder("/root", lang="en") in ("system folders are not allowed", "the user profile folder is too broad: pick a subfolder")


def test_own_data_folder_real(tmp_path):
    data = tmp_path / "data"
    (data / "inbox").mkdir(parents=True)
    (data / "db").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert paths.unsafe_folder(data / "db", data_dir=data, lang="en") == "the app's own data folder cannot be used"
    assert paths.unsafe_folder(data / "inbox", data_dir=data, allow_data_subdir="inbox") is None
    assert paths.unsafe_folder(data / "inbox", data_dir=data, allow_data_subdir=data / "inbox") is None
    assert paths.unsafe_folder(tmp_path, data_dir=data, lang="en") == "that folder contains the app's own data folder"
    assert paths.unsafe_folder(other, data_dir=data) is None


def test_hidden_folders_but_not_the_system_temp_folder(tmp_path):
    hidden = tmp_path / ".git" / "hooks"
    hidden.mkdir(parents=True)
    assert paths.unsafe_folder(hidden, lang="en") == "configuration and hidden folders are not allowed"
    # tmp_path itself may sit under a "hidden" name on some systems; the temp-folder exemption keeps it usable
    assert paths.unsafe_folder(tmp_path, lang="en") is None


def test_unsafe_file_real(tmp_path):
    ok = tmp_path / "factura.pdf"
    ok.write_bytes(b"x")
    assert paths.unsafe_file(ok) is None
    for name in (".env", ".env.local", "id_rsa", "id_ed25519", "mcp-token", "site.pem", "api.key", "store.p12", "vault.kdbx", "credentials.json", "secrets.yml", ".htpasswd"):
        f = tmp_path / name
        f.write_text("x")
        assert paths.unsafe_file(f, lang="en") == "that looks like a credentials file", name
    assert paths.unsafe_file(tmp_path / "missing.pdf", lang="en") == "the file does not exist"
    assert paths.unsafe_file(tmp_path, lang="en") == "the file does not exist"        # a folder is not a file
    sub = tmp_path / "node_modules"
    sub.mkdir()
    (sub / "a.js").write_text("x")
    assert paths.unsafe_file(sub / "a.js", lang="en") == "configuration and hidden folders are not allowed"


def test_unsafe_file_own_data(tmp_path):
    data = tmp_path / "data"
    (data / "inbox").mkdir(parents=True)
    (data / "app.db").write_text("x")
    (data / "inbox" / "a.pdf").write_text("x")
    assert paths.unsafe_file(data / "app.db", data_dir=data, lang="en") == "the app's own data folder cannot be used"
    assert paths.unsafe_file(data / "inbox" / "a.pdf", data_dir=data, allow_data_subdir="inbox") is None


def test_unsafe_output_dir_real(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "file").write_text("x")
    assert paths.unsafe_output_dir(tmp_path / "new" / "deep", data_dir=data) is None
    assert paths.unsafe_output_dir(tmp_path, data_dir=data) is None
    assert paths.unsafe_output_dir(tmp_path / "file", data_dir=data, lang="en") == "that path is a file, not a folder"
    assert paths.unsafe_output_dir(data / "x", data_dir=data, lang="en") == "the app's own data folder cannot be used"
    assert paths.unsafe_output_dir(data / "workshop" / "x", data_dir=data, allow_data_subdir="workshop") is None
    assert paths.unsafe_output_dir(os.path.abspath(os.sep), data_dir=data, lang="en") == "a drive root is too broad"


# ------------------------------------------------------------------ is_inside, hidden_parts, safe_member

def test_is_inside_posix_and_windows(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert paths.is_inside(tmp_path / "a" / "b", tmp_path)
    assert paths.is_inside(tmp_path, tmp_path)
    assert not paths.is_inside(tmp_path, tmp_path / "a")
    assert not paths.is_inside(tmp_path / "a" / ".." / ".." / "x", tmp_path / "a")
    assert not paths.is_inside(tmp_path / "ab", tmp_path / "a")                       # a prefix of the name is not inside
    assert paths.is_inside("C:\\Data\\Sub\\x", "c:\\data")
    assert paths.is_inside("C:/Data/Sub/../Sub/x", "C:\\DATA\\")
    assert not paths.is_inside("C:\\Data\\..\\Other", "C:\\Data")
    assert not paths.is_inside("D:\\Data", "C:\\Data")
    assert not paths.is_inside("C:\\Data", "/tmp")                                    # mixed flavours
    assert not paths.is_inside("relative", "C:\\Data") and not paths.is_inside("", "C:\\Data")


def test_hidden_parts():
    assert paths.hidden_parts("/home/me/.git/hooks") == [".git"]
    assert paths.hidden_parts("/home/me/.ssh/node_modules/.git") == [".ssh", "node_modules", ".git"]
    assert paths.hidden_parts("C:\\Users\\Me\\AppData\\Roaming\\X") == ["appdata"]
    assert paths.hidden_parts(PureWindowsPath("C:\\Users\\Me\\.AWS")) == [".aws"]
    assert paths.hidden_parts("C:\\Users\\Me\\AppData\\Local\\Temp\\ok") == []
    assert paths.hidden_parts("C:\\Users\\Me\\AppData\\Local\\Temp\\ok\\.git") == [".git"]
    assert paths.hidden_parts("/home/me/docs") == []
    assert paths.hidden_parts("") == [] and paths.hidden_parts("relative/.git") == []
    assert paths.hidden_parts("/a/.git/b/.git") == [".git"]                             # no repeats


def test_safe_member_accepts_normal_names(tmp_path):
    base = tmp_path / "out"
    base.mkdir()
    assert paths.safe_member(base, "a.txt") == (base / "a.txt").resolve()
    assert paths.safe_member(base, "dir/sub/b.txt") == (base / "dir" / "sub" / "b.txt").resolve()
    assert paths.safe_member(base, "dir\\sub\\c.txt") == (base / "dir" / "sub" / "c.txt").resolve()
    assert paths.safe_member(base, "./x/../y.txt") == (base / "y.txt").resolve()
    assert paths.safe_member(base, "ñandú/café.txt").name == "café.txt"


@pytest.mark.parametrize("name", [
    "../evil.txt", "a/../../evil.txt", "..\\evil.txt", "/etc/passwd", "\\windows\\system32", "C:\\evil.txt", "C:evil.txt", "c:/evil.txt",
    "", "..", "a\x00b", "dir/../../x",
])
def test_safe_member_rejects_zip_slip(tmp_path, name):
    with pytest.raises(ValueError):
        paths.safe_member(tmp_path, name)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_safe_member_rejects_a_symlink_that_leaves_the_folder(tmp_path):
    base = tmp_path / "out"
    base.mkdir()
    (base / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        paths.safe_member(base, "escape/evil.txt")


def test_safe_member_with_a_real_zip(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok/a.txt", "a")
        zf.writestr("../bad.txt", "b")
    out = tmp_path / "out"
    out.mkdir()
    made, refused = [], []
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            try:
                target = paths.safe_member(out, info.filename)
            except ValueError:
                refused.append(info.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            made.append(target)
    assert refused == ["../bad.txt"] and [t.name for t in made] == ["a.txt"] and not (tmp_path / "bad.txt").exists()
