"""Validate updated family consumers in their own environments, with bounded parallelism.

No apps are started. Logs stay in the selected local output directory. Run
from HoardLink: python scripts/validate_cohesion.py --output-dir PATH
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

PROJECTS = {
    "argus": ("Argus's Hoard", []), "babel": ("Babel's Hoard", []),
    "daguerre": ("Daguerre's Hoard", []), "dorian": ("Dorian's Hoard", []),
    "funes": ("Funes's Hoard", ["tests/test_backend.py"]), "galton": ("Galton's Hoard", []),
    "kafka": ("Kafka's Hoard", []), "laplace": ("Laplace's Hoard", []),
    "pygmalion": ("Pygmalion's Hoard", []), "scheherazade": ("Scheherazade's Hoard", []),
}


def check(app, root, output):
    folder_name, tests = PROJECTS[app]
    folder = root / folder_name
    candidates = [folder / name / "Scripts/python.exe" for name in (".venv", "venv")]
    python = next((p for p in candidates if p.is_file()), None)
    if python is None:
        return {"app": app, "ok": False, "error": "app interpreter missing"}
    commands = [("pytest", [str(python), "-m", "pytest", "-q", "-o", "addopts=", *tests], folder)]
    if app in ("kafka", "galton", "pygmalion"):
        commands.append(("api_doc", [str(python), "scripts/gen_api_doc.py"], folder))
    if app in ("babel", "daguerre"):
        commands.append(("build", ["npm.cmd" if os.name == "nt" else "npm", "run", "build"], folder / "frontend"))
    records = []
    environment = dict(os.environ, PYTHONUTF8="1")
    for name, argv, cwd in commands:
        began = time.monotonic()
        path = output / f"{app}-{name}.log"
        try:
            with path.open("w", encoding="utf-8") as stream:
                result = subprocess.run(argv, cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=1200, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            records.append({"check": name, "exit_code": result.returncode, "seconds": round(time.monotonic() - began, 2), "log": str(path)})
            if result.returncode:
                break
        except (OSError, subprocess.TimeoutExpired) as error:
            records.append({"check": name, "exit_code": -1, "error": type(error).__name__, "log": str(path)})
            break
    return {"app": app, "ok": all(r["exit_code"] == 0 for r in records), "checks": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--apps", nargs="+", choices=PROJECTS, default=list(PROJECTS))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=3)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {app: pool.submit(check, app, args.root, args.output_dir) for app in args.apps}
        records = []
        for app, future in futures.items():
            record = future.result()
            records.append(record)
            print(json.dumps({"app": app, "ok": record["ok"]}), flush=True)
    (args.output_dir / "summary.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return int(any(not r["ok"] for r in records))


if __name__ == "__main__":
    raise SystemExit(main())
