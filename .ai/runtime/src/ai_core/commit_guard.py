"""Secret-in-commit guard (shared by Claude + Codex via the runtime PreToolUse hook).

Blocks `git commit` when the staged change (or, for `-a`, unstaged tracked edits) ADDS a
likely secret to any file — the unattended-loop failure mode where a credential slips into
history. Mirrors the standalone Claude shell hook (kit block-secret-commit.sh) so Codex,
which routes PreToolUse through the runtime, gets the same protection. Respects
.ai/secret_scan_allowlist.txt. No network. stdlib only.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_COMMIT_RE = re.compile(r"\bgit\b[^\n]{0,60}\bcommit\b")
_USE_ALL_RE = re.compile(r"--all\b|(?:^|\s)-[a-zA-Z]*a")
_MAX_DIFF_BYTES = 2_000_000

_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
)


def _run(args: list[str], cwd: str) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def commit_secret_reason(root: Path, command: str) -> str | None:
    """Return a deny reason if this `git commit` would add a secret, else None.

    Fail-open on any error (returns None) so a guard bug never blocks all commits; the
    standalone Claude shell hook and release-gate secret_scan remain as backstops.
    """
    if not command or not _COMMIT_RE.search(command):
        return None
    cwd = str(root)
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd).strip() or cwd
    base = ["git", "diff", "HEAD"] if _USE_ALL_RE.search(command) else ["git", "diff", "--cached"]
    names = [n for n in _run(base + ["--name-only"], top).splitlines() if n.strip()]
    if not names:
        return None
    allow: set[str] = set()
    try:
        with open(os.path.join(top, ".ai", "secret_scan_allowlist.txt")) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    allow.add(line)
    except Exception:
        pass
    targets = [n for n in names if n not in allow]
    if not targets:
        return None
    content = _run(base + ["--"] + targets, top)[:_MAX_DIFF_BYTES]
    added = "\n".join(
        line[1:] for line in content.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    hits = sorted({name for name, rx in _PATTERNS if rx.search(added)})
    if not hits:
        return None
    return (
        "커밋 차단: staged 변경에서 시크릿 패턴 감지(" + ", ".join(hits[:4]) + "). "
        "시크릿을 제거하고 재시도하거나, 오탐이면 해당 경로를 .ai/secret_scan_allowlist.txt에 추가."
    )
