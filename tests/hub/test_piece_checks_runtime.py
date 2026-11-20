from types import SimpleNamespace

from hoard_link.hub.piece_checks import _runtime


def test_inspector_uses_declared_python_and_code_outside_manifest(tmp_path):
    manifest = tmp_path / "registry" / "faustus-plugin.json"
    source = tmp_path / "code"
    python = tmp_path / "environment" / "Scripts" / "python.exe"
    source.mkdir()
    python.parent.mkdir(parents=True)
    python.touch()
    app = SimpleNamespace(manifest_path=str(manifest), launch=SimpleNamespace(cwd=str(source), executable=str(python)))

    assert _runtime(app) == ([source, manifest.parent], str(python))
