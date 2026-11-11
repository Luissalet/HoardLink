"""Node export opt-outs survive synchronization without changing ordinary caps."""

import subprocess

import pytest

from tests.commons.jsrun import JS_DIR, node


@pytest.mark.parametrize("enumerable", [True, False])
def test_export_marker_is_stripped_without_mutation(tmp_path, enumerable):
    script = tmp_path / "marker.mjs"
    script.write_text(
        f"import {{ capResult, makeAgentRoutes }} from '{(JS_DIR / 'express.js').as_uri()}';\n"
        "import assert from 'node:assert/strict';\n"
        "const payload = { text: 'Árbol, \"campo\"\\n'.repeat(40000), rows: Array(1200).fill('fila') };\n"
        "const result = Object.freeze(Object.defineProperty({ ...payload }, '__uncapped', "
        f"{{ value: true, enumerable: {str(enumerable).lower()} }}));\n"
        "const out = capResult(result, 1000);\n"
        "assert.deepEqual(out, payload);\n"
        "assert.notEqual(out, result);\n"
        "assert.equal(result.__uncapped, true);\n"
        "assert.equal(Object.hasOwn(out, '__uncapped'), false);\n"
        "assert.ok(capResult(payload, 1000).truncated);\n"
        "assert.ok(capResult({ ...payload, __uncapped: false }, 1000).truncated);\n"
        "assert.ok(capResult({ ...payload, __uncapped: 'true' }, 1000).truncated);\n"
        "const routes = makeAgentRoutes({ token: 'test', callTool: () => result, capLimit: 1000 });\n"
        "let sent; await routes.call({ headers: { authorization: 'Bearer test' }, body: { name: 'export' } }, "
        "{ json(value) { sent = value; } });\n"
        "assert.deepEqual(sent, payload);\n",
        encoding="utf-8",
    )
    result = subprocess.run([node(), str(script)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr[-3000:]


def test_route_export_opt_out_is_scoped_to_the_selected_tool(tmp_path):
    script = tmp_path / "routes.mjs"
    script.write_text(
        f"import {{ makeAgentRoutes }} from '{(JS_DIR / 'express.js').as_uri()}';\n"
        "import assert from 'node:assert/strict';\n"
        "const result = Object.freeze({ text: 'historia'.repeat(10000), count: 137 });\n"
        "const routes = makeAgentRoutes({ token: 'test', capLimit: 1000, uncappedTools: ['export'], "
        "callTool: () => result });\n"
        "const call = async name => { let sent; await routes.call({ headers: { authorization: 'Bearer test' }, "
        "body: { name } }, { json(value) { sent = value; } }); return sent; };\n"
        "for (const name of ['search', 'export', 'search', 'export']) {\n"
        "  const out = await call(name);\n"
        "  if (name === 'export') assert.deepEqual(out, result);\n"
        "  else { assert.ok(out.truncated); assert.ok(out.text.length < result.text.length); }\n"
        "}\n"
        "assert.equal(result.text, 'historia'.repeat(10000));\n",
        encoding="utf-8",
    )
    result = subprocess.run([node(), str(script)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr[-3000:]
