#!/usr/bin/env bash
# PreToolUse (Bash): block `git commit` when the staged change ADDS a likely secret.
# Complements protect-secrets.sh (sensitive file paths) and block-dangerous.sh (destructive
# commands): this catches a secret pasted into ANY file — the unattended-loop failure mode
# where a credential slips into history. Respects .ai/secret_scan_allowlist.txt (path
# acknowledgments) so known test fixtures do not false-block. No network. Fast: only acts on
# `git commit`.
set -euo pipefail

payload="$(cat)"

python3 - "$payload" <<'PY'
import json
import os
import re
import subprocess
import sys

try:
    payload = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)

tool_input = payload.get("tool_input", {}) or {}
cmd = tool_input.get("command") or tool_input.get("CommandLine") or tool_input.get("commandLine") or ""
if not re.search(r"\bgit\b[^\n]{0,60}\bcommit\b", cmd):
    sys.exit(0)

cwd = payload.get("cwd") or os.getcwd()


def run(args, where):
    try:
        return subprocess.run(args, cwd=where, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


root = run(["git", "rev-parse", "--show-toplevel"], cwd).strip() or cwd

# `-a/--all` commits include unstaged tracked edits, so diff HEAD; otherwise staged only.
use_all = bool(re.search(r"--all\b|(?:^|\s)-[a-zA-Z]*a", cmd))
base = ["git", "diff", "HEAD"] if use_all else ["git", "diff", "--cached"]

names = [n for n in run(base + ["--name-only"], root).splitlines() if n.strip()]
if not names:
    sys.exit(0)

allow = set()
try:
    with open(os.path.join(root, ".ai", "secret_scan_allowlist.txt")) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                allow.add(line)
except Exception:
    pass

targets = [n for n in names if n not in allow]
if not targets:
    sys.exit(0)

content = run(base + ["--"] + targets, root)[:2_000_000]
added = "\n".join(
    line[1:] for line in content.splitlines()
    if line.startswith("+") and not line.startswith("+++")
)

PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
]
hits = sorted({name for name, rx in PATTERNS if rx.search(added)})
if hits:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "커밋 차단: staged 변경에서 시크릿 패턴 감지(" + ", ".join(hits[:4]) + "). "
                "시크릿을 제거하고 재시도하거나, 오탐이면 해당 경로를 .ai/secret_scan_allowlist.txt에 추가."
            ),
        }
    }, ensure_ascii=False))

sys.exit(0)
PY
