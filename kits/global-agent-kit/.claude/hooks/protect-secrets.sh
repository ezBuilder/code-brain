#!/usr/bin/env bash
set -euo pipefail

payload="$(cat)"
policy_path="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/policies/hook-policy.json"

python3 - "$payload" "$policy_path" <<'PY'
import json
import os
import re
import sys

payload = json.loads(sys.argv[1])
policy_path = sys.argv[2]
tool_input = payload.get("tool_input", {})

paths = []
for key in ("file_path", "path"):
    if key in tool_input:
        paths.append(str(tool_input[key]))
if "files" in tool_input and isinstance(tool_input["files"], list):
    paths.extend(map(str, tool_input["files"]))

allowed_env_examples = re.compile(r"(^|/)\.env\.(example|sample|template)$")
try:
    with open(policy_path) as fh:
        policy = json.load(fh)
    secret_patterns = [re.compile(pattern) for pattern in policy["hard_deny"]["paths"]]
except Exception:
    secret_patterns = [
        re.compile(r"(^|/)\.env($|\.)"),
        re.compile(r"(^|/)secrets?/"),
        re.compile(r"credentials\.json$"),
        re.compile(r"\.pem$"),
        re.compile(r"\.key$"),
        re.compile(r"id_rsa$"),
        re.compile(r"id_ed25519$"),
    ]

for p in paths:
    norm = p.replace("\\", "/")
    if allowed_env_examples.search(norm):
        continue
    if any(rx.search(norm) for rx in secret_patterns):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "민감 파일 접근 차단"
            }
        }, ensure_ascii=False))
        sys.exit(0)
PY
