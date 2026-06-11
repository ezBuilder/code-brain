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

WRITE_COMMANDS = {
    "render",
    "trust",
    "upgrade",
    "migrate",
    "index",
    "queue",
    "inbox",
    "notify",
    "obs_write",
    "diagnostics_write",
    "memory",
    "audit",
    "worker_stop",
    "session",
    "exec",
    "remote_memory",
    "loop",
}


class PolicyDenied(RuntimeError):
    def __init__(self, command: str) -> None:
        super().__init__(f"CI read-only policy denied write command: {command}")
        self.command = command


def is_ci() -> bool:
    truthy = {"1", "true", "yes", "on"}
    return any(
        os.environ.get(name, "").lower() in truthy
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI")
    )


def reject_ci_write(command: str, *, dry_run: bool = False) -> None:
    if is_ci() and command in WRITE_COMMANDS and not dry_run:
        raise PolicyDenied(command)


def allow_no_redact(yes: bool) -> bool:
    return bool(yes and sys.stdout.isatty() and sys.stdin.isatty())
