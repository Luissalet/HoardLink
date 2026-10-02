"""Run functions of a js/hoard-commons/ module with node and return their JSON results.

Shared vectors (tests/vectors/<name>.json) are lists of ``{"fn": "snake_name", "args": [...], "opts": {...},
"expect": ...}``. The Python test calls ``module.snake_name(*args, **opts)``; :func:`run_js` calls the
camelCase twin as ``camelName(...args, optsInCamelCase)`` (the options object is only passed when non-empty).
Tests that use it skip when ``node`` is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS_DIR = ROOT / "js" / "hoard-commons"
VECTORS = ROOT / "tests" / "vectors"


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def camel_keys(opts: dict[str, Any]) -> dict[str, Any]:
    return {camel(k): v for k, v in opts.items()}


def load_vectors(name: str) -> list[dict[str, Any]]:
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))


def node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed")
    return exe


def run_js(module: str, calls: list[dict[str, Any]], *, timeout: float = 60.0) -> list[Any]:
    """Each call ``{"fn", "args", "opts"}``; returns the results in order (``{"__error__": msg}`` on a throw).
    Promises are awaited."""
    exe = node()
    mod = (JS_DIR / module).resolve().as_uri()
    prepared = [{"fn": camel(c["fn"]), "args": c.get("args", []), "opts": camel_keys(c.get("opts") or {})} for c in calls]
    script = (
        f"import * as m from {json.dumps(mod)};\n"
        "const calls = JSON.parse(process.argv[2]);\n"
        "const out = [];\n"
        "for (const c of calls) {\n"
        "  try {\n"
        "    const f = m[c.fn]; if (typeof f !== 'function') throw new Error('no function ' + c.fn);\n"
        "    const args = Object.keys(c.opts).length ? [...c.args, c.opts] : c.args;\n"
        "    let r = f(...args); if (r && typeof r.then === 'function') r = await r;\n"
        "    out.push(r === undefined ? null : r);\n"
        "  } catch (e) { out.push({ __error__: String(e && e.message || e) }); }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        payload = json.dumps(prepared)
        proc = subprocess.run([exe, str(f), payload], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def python_call(module: Any, case: dict[str, Any]) -> Any:
    return getattr(module, case["fn"])(*case.get("args", []), **(case.get("opts") or {}))


_FLOATY = re.compile(r"^-?\d+\.\d+$")


def normalise(value: Any) -> Any:
    """JSON round trip (tuples become lists, Decimal is not expected here)."""
    return json.loads(json.dumps(value, default=str))
