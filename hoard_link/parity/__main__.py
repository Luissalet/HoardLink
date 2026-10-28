"""``python -m hoard_link.parity``: run harnesses, compare two traces, read the state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .compare import EmptyTraceError, TraceMismatch, compare_traces
from .corpus import read_jsonl
from .harness import load_harness, run_harness, write_lock
from .select import State, changed_harnesses, git_changed


def _print_report(r: dict) -> None:
    out = sys.stdout
    line = f"{r['harness']}: {r['status']}"
    if r["status"] == "pass":
        m = r["matched"]
        line += f" - {m['cases']:,} cases match" + (" (oracle cached)" if r["oracle"]["cached"] else "") + f" - {r['duration_s']}s"
        st = r.get("stability") or {}
        if st.get("required", 1) > 1:
            line += f" - pass {st['passes']}/{st['required']}"
    elif r["status"] == "fail":
        line += " - " + r["divergence"]["summary"]
        sv = r.get("survey")
        if sv:
            line += f"\n  survey: {sv['divergent']:,} of {sv['cases']:,} cases diverge"
            for cat, n in list(sv["by_category"].items())[:12]:
                line += f"\n    {n:>6,}  {cat}"
    elif r["status"] == "skipped":
        line += " - " + r["reason"]
    elif r["status"] == "error":
        line += " - " + r.get("error", "")
    print(line, file=out)
    if r["status"] in ("pass", "skipped"):
        print(f"  not covered: {r['scope']}", file=out)
    if r.get("log_tail"):
        print("  " + r["log_tail"].replace("\n", "\n  "), file=out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hoard_link.parity", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run harness files")
    r.add_argument("harnesses", nargs="+", type=Path)
    r.add_argument("--state-dir", type=Path, default=Path(".parity"))
    r.add_argument("--fresh", action="store_true", help="ignore the oracle cache")
    r.add_argument("--force", action="store_true", help="run even when nothing changed since the last pass")
    r.add_argument("--stable", type=int, default=1, help="passes in a row before a harness counts as stable (3 for a new one)")
    r.add_argument("--changed", metavar="BASE", help="only harnesses a git diff against BASE can affect")
    r.add_argument("--changed-files", metavar="FILE", type=Path, help="only harnesses the paths listed in FILE can affect")
    r.add_argument("--survey", action="store_true", help="on a divergence, count every divergent case by category")
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("compare", help="compare two trace files exactly")
    c.add_argument("reference", type=Path)
    c.add_argument("candidate", type=Path)
    c.add_argument("--inputs", type=Path, help="the corpus file, to print the input of the first differing case")
    c.add_argument("--survey", action="store_true", help="count every divergent case by category instead of stopping at the first")
    s = sub.add_parser("status", help="last result of every harness in a state folder")
    s.add_argument("--state-dir", type=Path, default=Path(".parity"))
    k = sub.add_parser("lock", help="pin the harness' artifacts and tool versions")
    k.add_argument("harness", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "compare":
        inputs = None
        if a.inputs:
            table = {c["id"]: c.get("input") for c in read_jsonl(a.inputs)}
            inputs = table.get
        if a.survey:
            from .compare import survey_traces
            try:
                sv = survey_traces(a.reference, a.candidate, inputs=inputs)
            except EmptyTraceError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(sv.to_dict(), ensure_ascii=False, indent=1))
            return 1 if sv.total_divergent else 0
        try:
            m = compare_traces(a.reference, a.candidate, inputs=inputs)
        except EmptyTraceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except TraceMismatch as exc:
            print(json.dumps(exc.divergence.to_dict(), ensure_ascii=False, indent=1))
            return 1
        print(f"match: {m.cases:,} cases")
        return 0
    if a.cmd == "status":
        st = State(a.state_dir)
        for name, e in sorted(st.data["harnesses"].items()):
            print(f"{name}: {e['status']} - {e['passes']} pass(es) in a row - {e['at']}")
        return 0
    if a.cmd == "lock":
        print(write_lock(load_harness(a.harness)))
        return 0

    loaded = [load_harness(p) for p in a.harnesses]
    if a.changed or a.changed_files:
        changed: list = []
        if a.changed_files:
            changed = [l.strip() for l in a.changed_files.read_text(encoding="utf-8").splitlines() if l.strip()]
        for h in loaded:
            if a.changed:
                changed += git_changed(h.root, a.changed)
        loaded = changed_harnesses(loaded, changed)
        if not loaded:
            print("no harness depends on the changed files")
            return 0
    for h in loaded:
        h.survey = h.survey or a.survey
    reports = [run_harness(h, a.state_dir, fresh=a.fresh, force=a.force, stable=a.stable) for h in loaded]
    for rep in reports:
        if a.json:
            print(json.dumps(rep, ensure_ascii=False))
        else:
            _print_report(rep)
    if any(x["status"] == "error" for x in reports):
        return 2
    return 1 if any(x["status"] == "fail" for x in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
