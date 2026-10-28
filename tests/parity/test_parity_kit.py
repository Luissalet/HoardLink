"""The differential test kit: corpus, traces, exact comparison, lock, selective runs, oracle cache and the report."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hoard_link.parity import (survey_traces, Command, Corpus, EmptyTraceError, Harness, LockError, TraceMismatch, compare_traces, f32_bits, f64_bits,
                               from_f32_bits, from_f64_bits, load_harness, make_lock, read_jsonl, run_harness, verify_lock, write_jsonl)
from hoard_link.parity.__main__ import main as cli
from hoard_link.parity.select import State, affected, changed_harnesses, expand, glob_regex

# a toy "game": damage = base * mult with a roll from the random source; the two programs implement it twice
ORACLE = textwrap.dedent('''
    import sys
    from hoard_link.parity import run_cli, RecordingRandom
    COUNT = sys.argv[0].replace("oracle.py", "oracle.count")
    open(COUNT, "a").write("x")
    def execute(inp, t):
        rng = RecordingRandom(inp["seed"], t)
        t.state("start", {"hp": inp["hp"]})
        roll = rng.randint(85, 100)
        dmg = inp["base"] * roll // 100
        t.query("typeMultiplier", 1, inp["type"])
        hp = max(0, inp["hp"] - dmg)
        t.state("end", {"hp": hp})
        t.result({"damage": dmg, "fainted": hp == 0})
    sys.exit(run_cli(execute))
''')
PROBE_SAME = ORACLE.replace('open(COUNT, "a").write("x")', "pass")
PROBE_ORDER = PROBE_SAME.replace('roll = rng.randint(85, 100)\n', 'crit = rng.random()\n    roll = rng.randint(85, 100)\n')   # one extra dice call
PROBE_STATE = PROBE_SAME.replace('t.state("end", {"hp": hp})', 't.state("end", {"hp": hp + (1 if dmg % 2 == 0 else 0)})')
PROBE_EMPTY = "import sys\nopen(sys.argv[2], 'w').close()\n"


def corpus():
    c = Corpus("toy-seed")
    c.grid(lambda base, hp: {"seed": base * 1000 + hp, "base": base, "hp": hp, "type": "fire"}, base=[0, 1, 50, 100, 250], hp=[1, 100, 300])
    c.sample(40, lambda rng: {"seed": rng.randrange(10**6), "base": rng.randrange(0, 300), "hp": rng.randrange(1, 400), "type": rng.choice(["fire", "water"])})
    return c


def make(tmp_path: Path, probe_src: str, **kw) -> Harness:
    (tmp_path / "oracle.py").write_text(ORACLE, encoding="utf-8")
    (tmp_path / "probe.py").write_text(probe_src, encoding="utf-8")
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    return Harness(name="toy", root=tmp_path, corpus=corpus, oracle=Command([sys.executable, str(tmp_path / "oracle.py")], env=env),
                   probe=Command([sys.executable, str(tmp_path / "probe.py")], env=env), scope="does not model critical hits, items or abilities",
                   depends_on=["probe.py"], oracle_depends_on=["oracle.py"], **kw)


# -- corpus -----------------------------------------------------------------------------------------------------
def test_corpus_is_reproducible_from_its_seed(tmp_path):
    a, b = corpus().cases(), corpus().cases()
    assert a == b and len(a) == 15 + 40 and a[0]["id"] == "c000000"
    assert write_jsonl(tmp_path / "a.jsonl", a)[1] == write_jsonl(tmp_path / "b.jsonl", b)[1]
    other = Corpus("another")
    other.sample(5, lambda r: {"v": r.random()})
    assert other.cases() != Corpus("toy-seed").cases()[:5]


def test_floats_travel_as_bits():
    for v in (-0.0, 0.0, 1e-40, math.inf, -math.inf, 0.1):
        assert math.copysign(1, from_f64_bits(f64_bits(v))) == math.copysign(1, v) and from_f64_bits(f64_bits(v)) == v
    assert math.isnan(from_f64_bits(f64_bits(math.nan))) and f32_bits(1.0) == "3f800000" and from_f32_bits("80000000") == 0.0


# -- comparison -------------------------------------------------------------------------------------------------
def write(path, records):
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records), encoding="utf-8")


def rec(i, ev, out=1):
    return {"id": f"c{i:06d}", "ev": ev, "out": out}


def test_equal_traces_match_and_empty_ones_prove_nothing(tmp_path):
    a, b, e = tmp_path / "a", tmp_path / "b", tmp_path / "e"
    write(a, [rec(0, [["prng", "random", 0.5]]), rec(1, [])])
    write(b, [rec(0, [["prng", "random", 0.5]]), rec(1, [])])
    assert compare_traces(a, b).cases == 2
    e.write_text("")
    with pytest.raises(EmptyTraceError):
        compare_traces(e, e)
    with pytest.raises(EmptyTraceError):
        compare_traces(a, tmp_path / "missing")
    nl = tmp_path / "nl"
    nl.write_text("\n\n")
    with pytest.raises(EmptyTraceError):
        compare_traces(nl, nl)


def test_first_divergence_names_the_case_event_and_input(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [rec(0, [["state", "s", 1]]), rec(1, [["state", "s", 1], ["prng", "random", 0.25], ["query", "q", 3]])])
    write(b, [rec(0, [["state", "s", 1]]), rec(1, [["state", "s", 1], ["prng", "random", 0.75], ["query", "q", 3]])])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b, inputs={"c000001": {"hp": 7}}.get)
    d = err.value.divergence
    assert (d.line, d.case_id, d.kind, d.event_index, d.event_kind, d.event_name) == (2, "c000001", "event", 1, "prng", "random")
    assert d.expected == 0.25 and d.actual == 0.75 and d.input == {"hp": 7} and d.path == ".ev[1][2]"
    assert d.context["expected"][-1] == ["prng", "random", 0.25] and "event 1 (prng random)" in d.summary


def test_a_different_order_or_count_of_random_calls_is_a_divergence(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [rec(0, [["prng", "random", 0.5], ["prng", "randint", 90]])])
    write(b, [rec(0, [["prng", "randint", 90], ["prng", "random", 0.5]])])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.event_index == 0
    write(b, [rec(0, [["prng", "random", 0.5]])])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.kind == "length" and err.value.divergence.event_index == 1


def test_extra_and_missing_cases_and_format_differences(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [rec(0, []), rec(1, [])])
    write(b, [rec(0, [])])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.kind == "missing_case" and err.value.divergence.case_id == "c000001"
    with pytest.raises(TraceMismatch) as err:
        compare_traces(b, a)
    assert err.value.divergence.kind == "extra_case"
    a.write_text('{"id":"c000000","ev":[],"out":1}\n')
    b.write_text('{"id":"c000000","ev":[],"out":1.0}\n')
    with pytest.raises(TraceMismatch):            # 1 and 1.0 are different results
        compare_traces(a, b)
    b.write_text('{"ev":[],"id":"c000000","out":1}\n')
    with pytest.raises(TraceMismatch) as err:     # same values, different text
        compare_traces(a, b)
    assert err.value.divergence.kind == "format"
    assert compare_traces(a, b, normalize=lambda r: r).cases == 1


def test_normalize_removes_what_may_differ(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [{"id": "c000000", "ev": [], "out": {"at": "2026-01-01", "n": 1}}])
    write(b, [{"id": "c000000", "ev": [], "out": {"at": "2026-02-02", "n": 1}}])
    with pytest.raises(TraceMismatch):
        compare_traces(a, b)
    drop = lambda r: {**r, "out": {k: v for k, v in r["out"].items() if k != "at"}}
    assert compare_traces(a, b, normalize=drop).cases == 1


def test_large_equal_traces_use_the_block_path(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    records = [rec(i, [["state", "s", i]]) for i in range(30000)]
    write(a, records)
    write(b, records)
    assert compare_traces(a, b).cases == 30000
    records[29999] = rec(29999, [["state", "s", -1]])
    write(b, records)
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.line == 30000


# -- lock -------------------------------------------------------------------------------------------------------
def test_lock_pins_hashes_and_tools(tmp_path):
    (tmp_path / "engine.bin").write_bytes(b"v1")
    lock = make_lock(tmp_path, {"engine": "engine.bin"}, {"python": [sys.executable, "--version"]})
    assert verify_lock(lock, tmp_path, {"python": [sys.executable, "--version"]})["engine"]
    (tmp_path / "engine.bin").write_bytes(b"v2")
    with pytest.raises(LockError, match="engine.bin changed"):
        verify_lock(lock, tmp_path)
    (tmp_path / "engine.bin").unlink()
    with pytest.raises(LockError, match="missing"):
        verify_lock(lock, tmp_path)
    lock["tools"]["python"] = "Python 0.0"
    (tmp_path / "engine.bin").write_bytes(b"v1")
    with pytest.raises(LockError, match="tool python"):
        verify_lock(lock, tmp_path, {"python": [sys.executable, "--version"]})


# -- runs -------------------------------------------------------------------------------------------------------
def test_matching_programs_pass_and_the_report_carries_its_scope(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    rep = run_harness(h, tmp_path / "state")
    assert rep["status"] == "pass" and rep["matched"]["cases"] == 55 and rep["corpus"]["seed"] == "toy-seed"
    saved = json.loads((tmp_path / "state" / "toy" / "report.json").read_text(encoding="utf-8"))
    assert saved["scope"] == "does not model critical hits, items or abilities" and saved["oracle"]["cached"] is False
    assert saved["corpus"]["sha256"] and saved["probe"]["output_sha256"] == saved["oracle"]["output_sha256"]


def test_a_harness_without_scope_is_refused(tmp_path):
    with pytest.raises(ValueError, match="scope"):
        Harness(name="x", root=tmp_path, corpus=corpus, oracle=Command(["a"]), probe=Command(["b"]), scope="  ")


def test_an_extra_random_call_is_reported_at_the_first_case(tmp_path):
    rep = run_harness(make(tmp_path, PROBE_ORDER), tmp_path / "state")
    d = rep["divergence"]
    assert rep["status"] == "fail" and d["case_id"] == "c000000" and d["event_kind"] == "prng" and d["event_index"] == 1
    assert d["input"]["base"] == 0 and "random" in d["summary"]


def test_a_state_difference_only_in_some_cases_is_found_with_its_input(tmp_path):
    rep = run_harness(make(tmp_path, PROBE_STATE), tmp_path / "state")
    d = rep["divergence"]
    assert rep["status"] == "fail" and d["event_kind"] == "state" and d["event_name"] == "end"
    assert d["expected"] + 1 == d["actual"] and d["path"].endswith("[2].hp") and d["input"]["hp"] >= 1


def test_an_empty_probe_output_is_an_error_not_a_pass(tmp_path):
    rep = run_harness(make(tmp_path, PROBE_EMPTY), tmp_path / "state")
    assert rep["status"] == "error" and "empty" in rep["error"]


def test_a_crashing_process_is_an_error_with_its_log(tmp_path):
    rep = run_harness(make(tmp_path, "raise SystemExit('boom')\n"), tmp_path / "state")
    assert rep["status"] == "error" and "probe exited" in rep["error"] and "boom" in rep["log_tail"]


def test_unchanged_dependencies_skip_and_a_change_reruns(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    assert run_harness(h, tmp_path / "state")["status"] == "pass"
    skipped = run_harness(h, tmp_path / "state")
    assert skipped["status"] == "skipped" and "passes" in skipped["stability"]
    saved = json.loads((tmp_path / "state" / "toy" / "report.json").read_text(encoding="utf-8"))
    assert saved["status"] == "pass"                                   # a skip does not overwrite the pass
    assert run_harness(h, tmp_path / "state", force=True)["status"] == "pass"
    (tmp_path / "probe.py").write_text(PROBE_SAME + "\n# edited\n", encoding="utf-8")
    assert run_harness(h, tmp_path / "state")["status"] == "pass"       # reran: the key moved
    assert State(tmp_path / "state").get("toy")["passes"] == 1


def test_a_new_harness_needs_three_passes_in_a_row(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    seen = [run_harness(h, tmp_path / "state", stable=3) for _ in range(4)]
    assert [r["status"] for r in seen] == ["pass", "pass", "pass", "skipped"]
    assert [r["stability"]["passes"] for r in seen[:3]] == [1, 2, 3] and seen[2]["stability"]["stable"] is True


def test_the_oracle_is_cached_until_its_inputs_change(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    counter = tmp_path / "oracle.count"
    assert run_harness(h, tmp_path / "state")["oracle"]["cached"] is False and counter.read_text() == "x"
    (tmp_path / "probe.py").write_text(PROBE_SAME + "\n# probe edit\n", encoding="utf-8")
    rep = run_harness(h, tmp_path / "state")
    assert rep["status"] == "pass" and rep["oracle"]["cached"] is True and counter.read_text() == "x"   # oracle did not rerun
    rep = run_harness(h, tmp_path / "state", force=True, fresh=True)
    assert rep["oracle"]["cached"] is False and counter.read_text() == "xx"
    (tmp_path / "oracle.py").write_text(ORACLE + "\n# oracle edit\n", encoding="utf-8")
    rep = run_harness(h, tmp_path / "state")
    assert rep["oracle"]["cached"] is False and counter.read_text() == "xxx"


def test_a_damaged_cache_entry_is_not_replayed(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    run_harness(h, tmp_path / "state")
    entry = next((tmp_path / "state" / "oracle-cache").glob("*/output.jsonl"))
    entry.write_text(entry.read_text(encoding="utf-8").replace("fainted", "fainteD"), encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE_SAME + "\n# again\n", encoding="utf-8")
    rep = run_harness(h, tmp_path / "state")
    assert rep["status"] == "pass" and rep["oracle"]["cached"] is False


def test_a_moved_lock_stops_the_run(tmp_path):
    (tmp_path / "engine.bin").write_bytes(b"v1")
    h = make(tmp_path, PROBE_SAME, lock="lock.json", lock_artifacts={"engine": "engine.bin"})
    from hoard_link.parity import write_lock
    write_lock(h)
    assert run_harness(h, tmp_path / "state")["status"] == "pass"
    (tmp_path / "engine.bin").write_bytes(b"v2")
    rep = run_harness(h, tmp_path / "state", force=True)
    assert rep["status"] == "error" and "engine.bin changed" in rep["error"]


def test_editing_a_source_during_the_run_aborts_it(tmp_path):
    h = make(tmp_path, PROBE_SAME.replace("sys.exit(run_cli(execute))", "open(__file__, 'a').write('# touched\\n')\nsys.exit(run_cli(execute))"))   # the probe edits itself
    rep = run_harness(h, tmp_path / "state")
    assert rep["status"] == "error" and "sources changed while the run was in progress" in rep["error"]


def test_empty_corpus_is_an_error(tmp_path):
    h = make(tmp_path, PROBE_SAME)
    h.corpus = lambda: []
    assert run_harness(h, tmp_path / "state")["status"] == "error"


# -- selection --------------------------------------------------------------------------------------------------
def test_globs_and_changed_selection(tmp_path):
    assert glob_regex("engine/**/*.mjs").match("engine/src/a/b.mjs") and glob_regex("engine/**/*.mjs").match("engine/b.mjs")
    assert not glob_regex("engine/*.mjs").match("engine/src/b.mjs") and glob_regex("a?.txt").match("ab.txt")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("1")
    assert [p.name for p in expand(tmp_path, ["src/**/*.js", "missing.txt"])] == ["a.js"]
    h1 = Harness(name="one", root=tmp_path, corpus=corpus, oracle=Command(["a"]), probe=Command(["b"]), scope="s", depends_on=["src/**/*.js"])
    h2 = Harness(name="two", root=tmp_path, corpus=corpus, oracle=Command(["a"]), probe=Command(["b"]), scope="s", depends_on=["other/*.py"])
    assert [h.name for h in changed_harnesses([h1, h2], [tmp_path / "src" / "a.js"])] == ["one"]
    assert [h.name for h in changed_harnesses([h1, h2], ["other/x.py"])] == ["two"]
    assert changed_harnesses([h1, h2], ["README.md"]) == []
    assert affected(tmp_path, ["src/**/*.js"], [str(tmp_path / "src" / "deep" / "x.js")])


def test_git_changed_lists_committed_and_uncommitted_files(tmp_path):
    from hoard_link.parity import git_changed
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("1"); git("add", "."); git("commit", "-qm", "one")
    git("checkout", "-qb", "work")
    (tmp_path / "b.py").write_text("2"); git("add", "."); git("commit", "-qm", "two")
    (tmp_path / "a.py").write_text("changed")
    (tmp_path / "c.py").write_text("new")
    assert sorted(p.name for p in git_changed(tmp_path, "main")) == ["a.py", "b.py", "c.py"]


# -- CLI and harness file -----------------------------------------------------------------------------------------
def test_cli_run_compare_status(tmp_path, capsys):
    (tmp_path / "oracle.py").write_text(ORACLE, encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE_ORDER, encoding="utf-8")
    pkg_root = str(Path(__file__).resolve().parents[2])
    (tmp_path / "toy_harness.py").write_text(textwrap.dedent(f'''
        import sys
        from pathlib import Path
        sys.path.insert(0, {pkg_root!r})
        from hoard_link.parity import Harness, Command, Corpus
        HERE = Path(__file__).parent
        def corpus():
            c = Corpus(7)
            c.sample(10, lambda r: {{"seed": r.randrange(1000), "base": 10, "hp": 50, "type": "x"}})
            return c
        def harness():
            env = {{"PYTHONPATH": {pkg_root!r}}}
            return Harness(name="clitoy", root=HERE, corpus=corpus, oracle=Command([sys.executable, str(HERE / "oracle.py")], env=env),
                           probe=Command([sys.executable, str(HERE / "probe.py")], env=env), scope="toy", depends_on=["probe.py"])
    '''), encoding="utf-8")
    assert load_harness(tmp_path / "toy_harness.py").name == "clitoy"
    state = tmp_path / "st"
    assert cli(["run", str(tmp_path / "toy_harness.py"), "--state-dir", str(state)]) == 1
    out = capsys.readouterr().out
    assert "clitoy: fail" in out and "case c000000" in out
    assert cli(["compare", str(state / "clitoy" / "oracle.jsonl"), str(state / "clitoy" / "probe.jsonl"), "--inputs", str(state / "clitoy" / "inputs.jsonl")]) == 1
    assert '"event_kind": "prng"' in capsys.readouterr().out
    (tmp_path / "probe.py").write_text(PROBE_SAME, encoding="utf-8")
    assert cli(["run", str(tmp_path / "toy_harness.py"), "--state-dir", str(state), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    (tmp_path / "ch.txt").write_text("README.md\n", encoding="utf-8")
    assert cli(["run", str(tmp_path / "toy_harness.py"), "--state-dir", str(state), "--changed-files", str(tmp_path / "ch.txt")]) == 0
    assert "no harness depends" in capsys.readouterr().out
    assert cli(["status", "--state-dir", str(state)]) == 0 and "clitoy: pass" in capsys.readouterr().out
    assert cli(["compare", str(state / "clitoy" / "oracle.jsonl"), str(state / "clitoy" / "oracle.jsonl")]) == 0


# -- tolerant reference values and the survey -----------------------------------------------------------------------
def test_range_and_approx_references_accept_values_inside_and_reject_outside(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [{"id": "c000000", "ev": [], "out": {"hp": {"$range": [80, 100]}, "p": {"$approx": [0.25, 0.01]}, "exact": 3}}])
    write(b, [{"id": "c000000", "ev": [], "out": {"hp": 93, "p": 0.255, "exact": 3}}])
    assert compare_traces(a, b).cases == 1
    write(b, [{"id": "c000000", "ev": [], "out": {"hp": [81, 100], "p": 0.25, "exact": 3}}])
    assert compare_traces(a, b).cases == 1                                   # a list is inside when all of it is
    for bad in ({"hp": 101, "p": 0.25, "exact": 3}, {"hp": 90, "p": 0.3, "exact": 3}, {"hp": [90, 120], "p": 0.25, "exact": 3},
                {"hp": "90", "p": 0.25, "exact": 3}, {"hp": 90, "p": 0.25, "exact": 4}):
        write(b, [{"id": "c000000", "ev": [], "out": bad}])
        with pytest.raises(TraceMismatch):
            compare_traces(a, b)
    write(b, [{"id": "c000000", "ev": [], "out": {"hp": 101, "p": 0.25, "exact": 3}}])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.path == ".out.hp"


def test_oneof_reference_accepts_only_its_listed_values_of_the_same_type(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, [{"id": "c000000", "ev": [["state", "dmg", {"$oneof": [41, 42, 44]}]], "out": {"s": {"$oneof": ["par", None]}}}])
    for good in ((42, "par"), (44, None)):
        write(b, [{"id": "c000000", "ev": [["state", "dmg", good[0]]], "out": {"s": good[1]}}])
        assert compare_traces(a, b).cases == 1
    for bad in ((43, "par"), (42.0, "par"), ("42", "par"), (42, "brn"), (42, 0)):
        write(b, [{"id": "c000000", "ev": [["state", "dmg", bad[0]]], "out": {"s": bad[1]}}])
        with pytest.raises(TraceMismatch):
            compare_traces(a, b)
    write(b, [{"id": "c000000", "ev": [["state", "dmg", 43]], "out": {"s": "par"}}])
    with pytest.raises(TraceMismatch) as err:
        compare_traces(a, b)
    assert err.value.divergence.path == ".ev[0][2]" and err.value.divergence.expected == {"$oneof": [41, 42, 44]}


def test_survey_counts_every_divergent_case_by_category(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    ref = [rec(i, [["state", "s", i], ["prng", "random", 0.5]], out=i) for i in range(10)]
    cand = [dict(r) for r in ref]
    cand[2] = rec(2, [["state", "s", 2], ["prng", "random", 0.9]], out=2)
    cand[5] = rec(5, [["state", "s", 5], ["prng", "random", 0.1]], out=5)
    cand[7] = rec(7, [["state", "s", 99], ["prng", "random", 0.5]], out=7)
    write(a, ref)
    write(b, cand)
    sv = survey_traces(a, b, inputs={"c000002": {"x": 1}}.get)
    assert (sv.cases, sv.matching, sv.total_divergent) == (10, 7, 3)
    assert sv.by_category == {"event:prng:random:.ev[*][*]": 2, "event:state:s:.ev[*][*]": 1}
    assert [d.case_id for d in sv.divergences] == ["c000002", "c000005", "c000007"] and sv.divergences[0].input == {"x": 1}
    with pytest.raises(EmptyTraceError):
        survey_traces(a, tmp_path / "nope")


def test_the_report_of_a_survey_harness_carries_the_counts(tmp_path):
    h = make(tmp_path, PROBE_STATE)
    h.survey = True
    rep = run_harness(h, tmp_path / "state")
    assert rep["status"] == "fail" and rep["survey"]["divergent"] >= 2 and rep["survey"]["cases"] == 55
    assert list(rep["survey"]["by_category"])[0].startswith("event:state:end")
