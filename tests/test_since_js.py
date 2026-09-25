"""js/hoard-link.js resolveSince speaks the same words as hoard_link/since.py (run with node when present)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "js" / "hoard-link.js"

SCRIPT = r"""
const { resolveSince, SINCE_HELP } = await import(process.argv[2]);
const now = new Date(2026, 8, 25, 14, 30).getTime();
const out = {};
for (const w of ["1h", "hace 3 días", "última hora", "esta mañana", "esta semana", "last week", "2026-09-01", ""]) {
  out[w] = resolveSince(w, now);
}
out.epoch = resolveSince(1788000000, now);
try { resolveSince("cuando sea", now); out.bad = "no error"; } catch (e) { out.bad = [e.status, e.message === SINCE_HELP]; }
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_resolve_since(tmp_path):
    import json
    from datetime import datetime

    script = tmp_path / "t.mjs"
    script.write_text(SCRIPT, encoding="utf-8")
    run = subprocess.run(["node", str(script), JS.as_uri()], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    now = datetime(2026, 9, 25, 14, 30).timestamp()

    def ts(iso: str) -> float:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    assert ts(out["1h"]) == pytest.approx(now - 3600)
    assert ts(out["hace 3 días"]) == pytest.approx(now - 3 * 86400)
    assert ts(out["última hora"]) == pytest.approx(now - 3600)
    assert ts(out["esta mañana"]) == pytest.approx(datetime(2026, 9, 25, 6).timestamp())
    assert ts(out["esta semana"]) == pytest.approx(datetime(2026, 9, 21).timestamp())
    assert ts(out["last week"]) == pytest.approx(now - 7 * 86400)
    assert out["2026-09-01"] == "2026-09-01" and out[""] is None
    assert ts(out["epoch"]) == 1788000000
    assert out["bad"] == [400, True]
