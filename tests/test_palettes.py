"""scripts/hoard_palettes.py: the shared theme file is exactly what the table generates, and every app has its own accent."""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _palettes():
    spec = importlib.util.spec_from_file_location("hoard_palettes", ROOT / "scripts" / "hoard_palettes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_theme_css_is_generated_from_the_table():
    mod = _palettes()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    assert (ROOT / "hoard_link" / "ui" / "hoard-theme.css").read_text(encoding="utf-8") == buf.getvalue()


def test_cookhoard_has_its_own_tomato_palette():
    mod = _palettes()
    css = (ROOT / "hoard_link" / "ui" / "hoard-theme.css").read_text(encoding="utf-8")
    accent, _tint, _sat = mod.PALETTES["cookhoard"]
    assert 'html[data-hoard-app="cookhoard"]' in css and f"--hoard-accent: {accent};" in css
    assert 5 <= mod.hue_of(accent) <= 20                       # a warm red-orange, tomato / paprika
    others = {k: v[0] for k, v in mod.PALETTES.items() if k != "cookhoard"}
    assert accent not in others.values()
    for name, other in others.items():                          # distinct from every other app's accent
        assert abs(mod.hue_of(accent) - mod.hue_of(other)) >= 4 or abs(mod.lum(accent) - mod.lum(other)) >= 0.02, name
    pal = mod.palette(accent, None, 0.16)
    assert mod.contrast(accent, pal["deep"]) >= 4.5            # readable as text on the app's background
    assert mod.contrast(accent, pal["accent-ink"]) >= 4.5      # and carries its own ink on a primary button


def test_every_accent_is_unique():
    mod = _palettes()
    accents = [v[0] for v in mod.PALETTES.values()]
    assert len(accents) == len(set(accents))
