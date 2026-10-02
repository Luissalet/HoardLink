"""Fixtures and a Node runner with byte support for the docs commons tests (not collected as tests)."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from tests.commons.jsrun import JS_DIR, camel, camel_keys, node


# ---- Node runner that revives {"__b64__": "..."} arguments into Buffers and returns Buffers the same way -------

def run_js_bytes(module: str, calls: list[dict[str, Any]], *, timeout: float = 60.0) -> list[Any]:
    exe = node()
    mod = (JS_DIR / module).resolve().as_uri()
    prepared = [{"fn": camel(c["fn"]), "args": c.get("args", []), "opts": camel_keys(c.get("opts") or {})} for c in calls]
    script = (
        f"import * as m from {json.dumps(mod)};\n"
        "const calls = JSON.parse(process.argv[2]);\n"
        "const revive = (v) => { if (v && typeof v === 'object' && !Array.isArray(v) && '__b64__' in v) return Buffer.from(v.__b64__, 'base64');\n"
        "  if (Array.isArray(v)) return v.map(revive); return v; };\n"
        "const wrap = (v) => { if (v instanceof Uint8Array) return { __b64__: Buffer.from(v).toString('base64') };\n"
        "  if (Array.isArray(v)) return v.map(wrap);\n"
        "  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, wrap(x)])); return v; };\n"
        "const out = [];\n"
        "for (const c of calls) {\n"
        "  try {\n"
        "    const f = m[c.fn]; if (typeof f !== 'function') throw new Error('no function ' + c.fn);\n"
        "    const args = c.args.map(revive);\n"
        "    const callArgs = Object.keys(c.opts).length ? [...args, c.opts] : args;\n"
        "    let r = f(...callArgs); if (r && typeof r.then === 'function') r = await r;\n"
        "    out.push(r === undefined ? null : wrap(r));\n"
        "  } catch (e) { out.push({ __error__: String(e && e.message || e) }); }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "run.mjs"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([exe, str(f), json.dumps(prepared)], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def b64(data: bytes) -> dict[str, str]:
    return {"__b64__": base64.b64encode(data).decode("ascii")}


def unb64(value: Any) -> Any:
    """Revive ``{"__b64__"}`` in vector arguments for the Python side."""
    if isinstance(value, dict) and "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    if isinstance(value, list):
        return [unb64(v) for v in value]
    return value


def jsonable(value: Any) -> Any:
    """What a Python result looks like after the JSON round trip the Node runner does (bytes as ``__b64__``)."""
    if isinstance(value, (bytes, bytearray)):
        return b64(bytes(value))
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def approx(a: Any, b: Any, tol: float = 1e-5) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) <= tol
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(approx(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(approx(a[k], b[k], tol) for k in a)
    return a == b


# ---- fixtures ----------------------------------------------------------------------------------------------

def make_zip(files: list[tuple[str, Any]], *, stored_first: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            kind = zipfile.ZIP_STORED if (stored_first and name == "mimetype") else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), content, compress_type=kind)
    return buf.getvalue()


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF (Helvetica) with one text line per page; ``""`` makes a page with no text."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> >>".encode())
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode("latin-1") if text else b""
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
