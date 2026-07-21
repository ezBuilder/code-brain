#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.runner_observe import observe_command  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a command with bounded SIGKILL and transport-restart observation."
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--summary-json", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    payload = observe_command(ROOT, command, label=args.label)
    if args.summary_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    exit_code = int(payload["exit_code"])
    if exit_code != 0:
        return exit_code
    return 0 if payload["ok"] is True else 70


if __name__ == "__main__":
    raise SystemExit(main())
