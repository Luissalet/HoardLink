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