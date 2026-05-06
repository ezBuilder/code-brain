from __future__ import annotations

import os
import sys

OK = 0
GENERIC_ERROR = 1
USAGE_ERROR = 2
CONFIG_INVALID = 10
POLICY_DENIED = 11
SECRET_DETECTED = 12
MANIFEST_DRIFT = 13
WORKER_UNAVAILABLE = 14
INCOMPATIBLE_VERSION = 15
PERMISSION_DENIED = 16

WRITE_COMMANDS = {"render", "trust", "secrets", "upgrade", "migrate", "index", "queue"}


def is_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"} or bool(os.environ.get("GITHUB_ACTIONS"))


def reject_ci_write(command: str, *, dry_run: bool = False) -> None:
    if is_ci() and command in WRITE_COMMANDS and not dry_run:
        raise SystemExit(PERMISSION_DENIED)


def allow_no_redact(yes: bool) -> bool:
    return bool(yes and sys.stdout.isatty() and sys.stdin.isatty())
