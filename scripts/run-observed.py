#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.runner_observe import observe_command  # noqa: E402


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a command with bounded SIGKILL and transport-restart observation."
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--summary-json", action="store_true")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_timeout,
        help="optional child deadline; timeout exits 124 after process-group cleanup",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    evidence_token = os.environ.get("AI_RUNNER_EVIDENCE_TOKEN")
    child_env = None
    if evidence_token is not None:
        child_env = os.environ.copy()
        child_env.pop("AI_RUNNER_EVIDENCE_TOKEN", None)
    payload = observe_command(
        ROOT,
        command,
        label=args.label,
        timeout_seconds=args.timeout_seconds,
        evidence_token=evidence_token,
        env=child_env,
    )
    if args.summary_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    exit_code = int(payload["exit_code"])
    if exit_code != 0:
        return exit_code
    return 0 if payload["ok"] is True else 70


if __name__ == "__main__":
    raise SystemExit(main())
