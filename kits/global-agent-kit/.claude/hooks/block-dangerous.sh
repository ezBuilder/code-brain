#!/usr/bin/env bash
set -euo pipefail

payload="$(cat)"
policy_path="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/policies/hook-policy.json"

python3 - "$payload" "$policy_path" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
policy_path = sys.argv[2]
cmd = payload.get("tool_input", {}).get("command", "")
try:
    with open(policy_path) as fh:
        policy = json.load(fh)
    blocked = policy["hard_deny"]["commands"]
except Exception:
    blocked = [
        r"\brm\s+-rf\s+(/|~|\*|\.)",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\s+-fd\b",
        r"\bdropdb\b",
        r"\bdrop\s+database\b",
        r"\bprisma\s+migrate\s+reset\b",
        r"\bsequelize\s+db:drop\b",
        r"\bkubectl\s+delete\b",
        r"\bterraform\s+destroy\b",
        r"\bpulumi\s+destroy\b",
        r"\bgit(?:\s+-C\s+\S+)?\s+branch\b[^\n;&|]*(?:-d|-D|--delete)\b[^\n;&|]*(?<![A-Za-z0-9._/-])(?:refs/heads/)?(?:main|master|develop|development|trunk|prod|production|staging)(?![A-Za-z0-9._/-])",
        r"\bgit\s+push\b.*(--force\b|--force-with-lease|\s-f\b)",
        r"\bgit(?:\s+-C\s+\S+)?\s+push\b[^\n;&|]*(?:--delete|-d)\b[^\n;&|]*(?<![A-Za-z0-9._/-])(?:refs/heads/)?(?:main|master|develop|development|trunk|prod|production|staging)(?![A-Za-z0-9._/-])",
        r"\bgit(?:\s+-C\s+\S+)?\s+push\b[^\n;&|]*(?<!\S):(?:refs/heads/)?(?:main|master|develop|development|trunk|prod|production|staging)(?![A-Za-z0-9._/-])",
        r"\bgit\s+filter-branch\b",
        r"\bgit\s+filter-repo\b",
        r"\bgit\s+update-ref\s+-d\b",
        r"\bgit\s+reflog\s+expire\b",
    ]

for pattern in blocked:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "파괴 명령 차단: 수동 절차와 사용자 명시 승인이 필요"
            }
        }, ensure_ascii=False))
        sys.exit(0)
PY
