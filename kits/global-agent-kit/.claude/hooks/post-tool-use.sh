#!/usr/bin/env bash
set -euo pipefail

python3 - 3<&0 <<'PY'
import json
import os
import re
import sys

try:
    with os.fdopen(3) as payload_stream:
        payload = json.load(payload_stream)
except json.JSONDecodeError:
    sys.exit(0)

tool = str(payload.get("tool_name", ""))
tool_input = payload.get("tool_input", {})
if not isinstance(tool_input, dict):
    tool_input = {}

def emit_context(message):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message
        }
    }, ensure_ascii=False))

def compact_path(value):
    text = str(value).replace("\\", "/")
    parts = [p for p in text.split("/") if p]
    if not parts:
        return text[:80]
    return "/".join(parts[-3:])[:120]

if tool in {"Write", "Edit", "MultiEdit"}:
    paths = []
    for key in ("file_path", "path"):
        if key in tool_input:
            paths.append(compact_path(tool_input[key]))
    if "edits" in tool_input and isinstance(tool_input["edits"], list):
        for edit in tool_input["edits"][:3]:
            if isinstance(edit, dict) and "file_path" in edit:
                paths.append(compact_path(edit["file_path"]))

    detail = f" touched={', '.join(dict.fromkeys(paths[:4]))}" if paths else ""
    emit_context(
        "Post-edit reminder: run the closest useful verification before reporting success, "
        "report changed files concisely, and do not revert unrelated dirty state."
        + detail
    )
    sys.exit(0)

if tool != "Bash":
    sys.exit(0)

command = str(tool_input.get("command", ""))
command_lc = command.lower()

verification = re.search(
    r"\b(bash\s+-n|shellcheck|test|pytest|vitest|jest|pnpm\s+(test|lint|build)|npm\s+(test|run\s+(lint|build))|yarn\s+(test|lint|build)|make\s+(test|validate)|tsc|eslint)\b",
    command_lc,
)
risky = re.search(
    r"\b(git\s+(commit|push|merge|rebase)|npm\s+install|pnpm\s+add|yarn\s+add|pip\s+install|uv\s+add|deploy|release|terraform|kubectl)\b",
    command_lc,
)

if verification:
    emit_context("Post-command reminder: treat this as verification evidence; include the command and pass/fail result in the completion report.")
elif risky:
    emit_context("Post-command reminder: high-impact command ran; confirm repo state, approval boundary, and any follow-up verification before continuing.")
PY
