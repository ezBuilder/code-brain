"""Code Brain eval runner (skeleton).

Loads a JSONL case file from ``cases/`` and reports pass/fail against the
assertion shapes defined in ``rubric.md``. Wiring against the live audit
log / hook telemetry lives behind ``--wired``; until that lane is built,
runs return ``skipped`` so CI does not block on placeholder cases.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Iterable


CASES_DIR = pathlib.Path(__file__).parent / "cases"


def load_cases(axis: str) -> Iterable[dict]:
    path = CASES_DIR / f"{axis}.jsonl"
    if not path.exists():
        raise SystemExit(f"unknown axis: {axis} (no {path})")
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def run_axis(axis: str, *, wired: bool) -> dict:
    cases = list(load_cases(axis))
    if not wired:
        return {
            "axis": axis,
            "cases": len(cases),
            "passed": 0,
            "failed": [],
            "skipped": [c["id"] for c in cases],
            "note": "runner not wired; rerun with --wired once telemetry adapter lands",
        }
    raise SystemExit("--wired adapter not implemented yet; see .ai/evals/README.md")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--axis", help="single axis to run (e.g. decision_logging)")
    parser.add_argument("--all", action="store_true", help="run every axis under cases/")
    parser.add_argument("--wired", action="store_true", help="run against live telemetry")
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args()

    if args.all:
        axes = sorted(p.stem for p in CASES_DIR.glob("*.jsonl"))
    elif args.axis:
        axes = [args.axis]
    else:
        parser.error("pass --axis <name> or --all")

    reports = [run_axis(a, wired=args.wired) for a in axes]

    if args.json:
        json.dump(reports, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in reports:
            print(f"{r['axis']}: {r['passed']}/{r['cases']} passed, {len(r['skipped'])} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
