"""Read-only family inventory. Reads source/manifests, never private data or tokens.

Run from this checkout: python scripts/audit_cohesion.py --roots PATH ... --output REPORT.json
Use --probe to read health/tool catalogues of registered local apps (never starts them).
Static integration signals are evidence to review, not proof that a flow works.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from hoard_link.hub import contract, drift, procs, registry

SKIP = drift.VENDOR_SKIP | {"hoard_link", "hoard-commons", "build", "release", "dist-electron", "_to_delete",
                           ".impeccable", "corpus", "models", "Screenshots", "OUTDATED DISCONTINUED", "test", "scripts"}
SIGNALS = {
    "shared_services": re.compile(r"\b(?:fam_(?:web|media|docs|embed)|mediaDownload|docsExtract|embedTexts|serviceAvailable)\b"),
    "hub_calls": re.compile(r"\b(?:family|fam|_f)\.call\s*\("),
    "shared_model": re.compile(r"\b(?:hoard_link|hoard-link\.js)\b"),
    "events": re.compile(r"\b(?:family|fam|_f)\.emit\s*\(|\bemitEvent\s*\("),
    "refs": re.compile(r"\b(?:fam_refs|hoard://)"),
    "notifications": re.compile(r"\b(?:fam_notify|notifyFamily)\b"),
    "direct_sibling_http": re.compile(r"https?://(?:localhost|127\.0\.0\.1):(?:87\d\d|88\d\d)\b"),
    "native_engine": re.compile(r"\b(?:faster_whisper|WhisperModel|rapidocr|PiperVoice|SentenceTransformer)\b"),
    "cloud_storage": re.compile(r"\b(?:supabase|firebase)\b", re.I),
}


def source_files(folder):
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith((".", "_")))
        for name in sorted(names):
            path = Path(root) / name
            if path.suffix in {".py", ".js", ".mjs", ".ts", ".tsx"} and path.stat().st_size < 1_000_000:
                yield path


def inspect_app(app, probe=False):
    folder = Path(app.folder)
    signals = {name: [] for name in SIGNALS}
    for path in source_files(folder):
        # Evidence is a path and line only: never return source containing a credential.
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            for key, regex in SIGNALS.items():
                if key == "direct_sibling_http":
                    urls = regex.findall(line)
                    if not any(int(url.rsplit(":", 1)[1]) not in (app.port, 8810) for url in urls):
                        continue
                if regex.search(line) and len(signals[key]) < 30:
                    signals[key].append(f"{path.relative_to(folder).as_posix()}:{line_no}")
    copies = drift.find_vendored(folder, REPO / "hoard_link")
    stale = []
    for copy in copies:
        changed, removed = drift.plan_tree(REPO / "hoard_link", copy)
        stale.append({"path": str(copy.relative_to(folder)), "changed": changed, "removed": removed})
    dirty = subprocess.run(["git", "status", "--porcelain", "-uno"], cwd=folder, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=10)
    result = {"id": app.id, "name": app.name, "folder": str(folder), "purpose": app.purpose,
              "capabilities": list(app.capabilities), "signals": signals, "vendored": stale,
              "node_drift": drift.vendored_js_stale(folder, REPO / "js" / "hoard-link.js"),
              "tracked_changes": dirty.stdout.splitlines() if dirty.returncode == 0 else None}
    if probe:
        health = procs.health(app)
        result["state"] = health.state
        catalog = contract.app_tools(app, timeout=3) if health.state == "healthy" else {}
        result["catalogue_ok"] = bool(catalog.get("ok"))
        result["tools"] = sorted(t["name"] for t in catalog.get("tools", []) if isinstance(t, dict) and t.get("name"))
        result["catalogue_error"] = catalog.get("error")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", default=[str(REPO.parent)])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    apps = registry.scan(args.roots, faustus_dir="D:/LocalAI/Faustus", exclude_ids=["scribe"])
    for root in args.roots:
        folder = Path(root)
        if (folder / "server_runtime.py").is_file() and (folder / "src/family_services.py").is_file():
            apps.append(registry.App(id="faustus", name="Faustus", folder=str(folder), purpose="Agent workspace and orchestration",
                                    url="http://127.0.0.1:7000", health_path="/api/health", expect_service=None))
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda app: inspect_app(app, args.probe), apps))
    report = {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "registered manifests + explicit Faustus root; runtime source signals need review", "probe": args.probe,
              "apps": rows, "summary": {"apps": len(rows), "healthy": sum(r.get("state") == "healthy" for r in rows),
              "copies_with_drift": sum(bool(v["changed"] or v["removed"]) for r in rows for v in r["vendored"])}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
