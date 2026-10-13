"""scripts/sync_vendored.py skips archived and scratch folders (``_archivo``, ``.cache``)."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_vendored.py"


def _vendored(folder: Path) -> None:
    pkg = folder / "pkg" / "hoard_link"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0"\n', encoding="utf-8")
    (pkg / "VENDORED.txt").write_text("vendored\n", encoding="utf-8")


def test_archived_apps_are_not_synced(tmp_path):
    _vendored(tmp_path / "Live Hoard")
    _vendored(tmp_path / "_archivo" / "Old Hoard")
    _vendored(tmp_path / ".scratch" / "Tmp Hoard")
    out = subprocess.run([sys.executable, str(SCRIPT), "--roots", str(tmp_path), "--dry-run"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "Live Hoard" in out.stdout
    assert "_archivo" not in out.stdout and "Old Hoard" not in out.stdout
    assert "Tmp Hoard" not in out.stdout


def test_temporary_checkouts_are_not_synced(tmp_path):
    _vendored(tmp_path / "Live Hoard")
    _vendored(tmp_path / "tmp" / "theirs")
    out = _run("--roots", str(tmp_path), "--dry-run")
    assert "Live Hoard" in out and "theirs" not in out


def test_check_exits_nonzero_on_drift_without_modifying_a_copy(tmp_path):
    folder = tmp_path / "Live Hoard"
    _vendored(folder)
    original = (folder / "pkg/hoard_link/__init__.py").read_bytes()
    check = subprocess.run([sys.executable, str(SCRIPT), "--roots", str(tmp_path), "--check"], capture_output=True, text=True)
    assert check.returncode == 1
    assert (folder / "pkg/hoard_link/__init__.py").read_bytes() == original
    _run("--roots", str(tmp_path))
    check = subprocess.run([sys.executable, str(SCRIPT), "--roots", str(tmp_path), "--check"], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout

def _run(*args):
    out = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_selected_python_relay_preserves_other_files_and_version_stamp(tmp_path):
    folder = tmp_path / 'Selected Hoard'
    _vendored(folder)
    vendor = folder / 'pkg/hoard_link'
    (vendor / 'copilot.py').write_text('# prior relay\n', encoding='utf-8')
    (vendor / 'extra.py').write_text('# app copy remains\n', encoding='utf-8')
    (vendor / 'empty-preserved').mkdir()
    before = {path.name: path.read_bytes() for path in vendor.iterdir() if path.is_file()}
    _run('--roots', str(tmp_path), '--files', 'copilot.py')
    assert (vendor / 'copilot.py').read_bytes() == (SCRIPT.parent.parent / 'hoard_link/copilot.py').read_bytes()
    for name in ('__init__.py', 'VENDORED.txt', 'extra.py'):
        assert (vendor / name).read_bytes() == before[name]
    assert {path.name for path in vendor.iterdir() if path.is_file()} == set(before)
    assert (vendor / 'empty-preserved').is_dir()
    check = subprocess.run([sys.executable, str(SCRIPT), '--roots', str(tmp_path), '--files', 'copilot.py', '--check'], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout


def test_selected_node_relay_preserves_other_common_files(tmp_path):
    folder = tmp_path / 'Node Selected Hoard'
    vendor = folder / 'server/hoard-commons'
    vendor.mkdir(parents=True)
    (folder / 'server/hoard-link.js').write_text('// prior transport\n', encoding='utf-8')
    (vendor / 'copilot.js').write_text('// prior relay\n', encoding='utf-8')
    (vendor / 'copilot-launcher.js').write_text('// prior launcher\n', encoding='utf-8')
    (vendor / 'other.js').write_text('// preserve other\n', encoding='utf-8')
    before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    _run('--roots', str(tmp_path), '--files', 'copilot.js')
    assert (vendor / 'copilot.js').read_bytes() == (SCRIPT.parent.parent / 'js/hoard-commons/copilot.js').read_bytes()
    for name, content in before.items():
        if name != 'server/hoard-commons/copilot.js':
            assert (folder / name).read_bytes() == content
    assert {path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file()} == set(before)


def test_galton_and_pygmalion_are_found_by_folder_name_and_vendored_package(tmp_path):
    for folder, pkg in (("Galton's Hoard", "galton_hoard"), ("Pygmalion's Hoard", "pygmalion_hoard")):
        _vendored(tmp_path / folder)
        old = tmp_path / folder / "pkg" / "hoard_link"
        new = tmp_path / folder / pkg / "hoard_link"
        new.parent.mkdir(parents=True)
        old.rename(new)
        (tmp_path / folder / "pkg").rmdir()
    out = _run("--roots", str(tmp_path), "--dry-run")
    assert "Galton's Hoard: galton_hoard" in out.replace("\\", "/").replace("/hoard_link", "")
    assert "Pygmalion's Hoard: pygmalion_hoard" in out.replace("\\", "/").replace("/hoard_link", "")
    for key in ("galton", "pygmalion"):                       # --apps takes the short name, as for every other app
        only = _run("--roots", str(tmp_path), "--apps", key, "--dry-run")
        assert only.count("file(s) updated") == 1 and key.title() in only


def test_galton_and_pygmalion_are_found_by_manifest_id(tmp_path):
    import json
    for folder, app_id, pkg in (("Galton Hoard Main", "galton", "galton_hoard"),
                                ("pyg-checkout", "pygmalion", "pygmalion_hoard")):
        root = tmp_path / folder
        (root / pkg / "hoard_link").mkdir(parents=True)
        (root / pkg / "hoard_link" / "VENDORED.txt").write_text("vendored\n", encoding="utf-8")
        (root / "faustus-plugin.json").write_text(json.dumps({"id": app_id}), encoding="utf-8")
    for key in ("galton", "pygmalion"):
        out = _run("--roots", str(tmp_path), "--apps", key, "--dry-run")
        assert f"{key}_hoard" in out.replace("\\", "/") and out.count("file(s) updated") == 1


def test_install_vendors_into_a_galton_and_pygmalion_package(tmp_path):
    for folder, pkg in (("Galton's Hoard", "galton_hoard"), ("Pygmalion's Hoard", "pygmalion_hoard")):
        (tmp_path / folder / pkg).mkdir(parents=True)
        (tmp_path / folder / pkg / "__main__.py").write_text("print('x')\n", encoding="utf-8")
    _run("--roots", str(tmp_path), "--install", "galton", "pygmalion")
    for folder, pkg in (("Galton's Hoard", "galton_hoard"), ("Pygmalion's Hoard", "pygmalion_hoard")):
        vend = tmp_path / folder / pkg / "hoard_link"
        assert (vend / "__init__.py").is_file() and (vend / "routes.py").is_file() and (vend / "VENDORED.txt").is_file()
        assert not (vend / "hub").exists()                      # the hub subpackage is never vendored


def test_vendored_copies_get_the_client_but_not_the_hub(tmp_path):
    """A Python app receives hoard_link/ without hub/ (the hub-side linkchat.py stays home) and a Node app
    receives server/hoard-link.js with the chat() and linkStatus() helpers."""
    py_app = tmp_path / "Py Hoard"
    _vendored(py_app)
    node_app = tmp_path / "Node Hoard"
    (node_app / "server").mkdir(parents=True)
    (node_app / "server" / "hoard-link.js").write_text("// old copy\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRIPT), "--roots", str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    vendored = py_app / "pkg" / "hoard_link"
    assert (vendored / "family.py").is_file() and (vendored / "_hubclient.py").is_file()
    assert "def chat(" in (vendored / "family.py").read_text(encoding="utf-8")
    assert not (vendored / "hub").exists() and not (vendored / "hub" / "linkchat.py").exists()
    js = (node_app / "server" / "hoard-link.js").read_text(encoding="utf-8")
    assert "export async function chat(" in js and "export async function linkStatus(" in js
    assert js == (SCRIPT.parents[1] / "js" / "hoard-link.js").read_text(encoding="utf-8")
