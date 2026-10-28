"""The differential kit through the hub: routes, tools and a background run."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from hoard_link.hub import tools

from ._hub_fakes import http, make_hub, serve, wait_for

ROOT = str(Path(__file__).resolve().parents[2])
ORACLE = textwrap.dedent('''
    import sys
    from hoard_link.parity import run_cli
    def execute(inp, t):
        t.state("n", inp["n"])
        t.result(inp["n"] * 2)
    sys.exit(run_cli(execute))
''')


def write_trace(path, records):
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records), encoding="utf-8")


def test_compare_status_and_run(tmp_path):
    hub = make_hub(tmp_path, [])
    server = serve(hub)
    base = hub.config.url.rstrip("/")
    try:
        a, b, e = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "e.jsonl"
        write_trace(a, [{"id": "c0", "ev": [["prng", "random", 0.5]], "out": 1}, {"id": "c1", "ev": [], "out": 2}])
        write_trace(b, [{"id": "c0", "ev": [["prng", "random", 0.5]], "out": 1}, {"id": "c1", "ev": [], "out": 3}])
        e.write_text("")
        status, res = http(base + "/api/parity/compare", {"reference": str(a), "candidate": str(a)})
        assert res["result"] == "match" and res["cases"] == 2
        status, res = http(base + "/api/parity/compare", {"reference": str(a), "candidate": str(b)})
        assert res["result"] == "diverge" and res["divergence"]["case_id"] == "c1" and res["divergence"]["path"] == ".out"
        status, res = http(base + "/api/parity/compare", {"reference": str(a), "candidate": str(b), "survey": True})
        assert res["survey"]["divergent"] == 1 and res["survey"]["cases"] == 2
        out = tools.call(hub, "hub_parity_compare", {"reference": str(e), "candidate": str(e)})
        assert out["ok"] is False and out["result"] == "empty"

        harness = tmp_path / "h.py"
        harness.write_text(textwrap.dedent(f'''
            import sys
            from pathlib import Path
            from hoard_link.parity import Harness, Command, Corpus
            HERE = Path(__file__).parent
            def harness():
                def corpus():
                    c = Corpus(1); c.grid(lambda n: {{"n": n}}, n=[1, 2, 3]); return c
                env = {{"PYTHONPATH": {ROOT!r}}}
                (HERE / "o.py").write_text({ORACLE!r}, encoding="utf-8")
                cmd = Command([sys.executable, str(HERE / "o.py")], env=env)
                return Harness(name="hub-toy", root=HERE, corpus=corpus, oracle=cmd, probe=cmd, scope="toy: same program on both sides")
        '''), encoding="utf-8")
        state = tmp_path / "state"
        started = tools.call(hub, "hub_parity_run", {"harness": str(harness), "state_dir": str(state)})
        assert started["ok"] and started["started"]
        done = wait_for(lambda: (lambda s: s if not s["running"] and s["harnesses"] else None)(tools.call(hub, "hub_parity_status", {"state_dir": str(state)})), timeout=60)
        row = done["harnesses"][0]
        assert row["harness"] == "hub-toy" and row["status"] == "pass" and row["report"]["scope"].startswith("toy")
        status, res = http(base + "/api/parity/status?state_dir=" + str(state).replace("\\", "/"))
        assert res["harnesses"][0]["report"]["matched"]["cases"] == 3
        again = tools.call(hub, "hub_parity_run", {"harness": str(harness), "state_dir": str(state)})
        assert again["ok"]
        assert tools.call(hub, "hub_parity_run", {"harness": str(tmp_path / "nope.py")})["ok"] is False
    finally:
        server.shutdown()
        hub.close()


def test_parity_tools_are_in_the_catalogue_with_hints():
    by = {t["name"]: t for t in tools.all_tools()}
    for name in ("hub_parity_compare", "hub_parity_status", "hub_parity_run"):
        assert len(by[name]["description"].splitlines()[0]) <= 110, name
    assert by["hub_parity_compare"]["annotations"]["readOnlyHint"] and by["hub_parity_status"]["annotations"]["readOnlyHint"]
    assert "annotations" not in by["hub_parity_run"]
