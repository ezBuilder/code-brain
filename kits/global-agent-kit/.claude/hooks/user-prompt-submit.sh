#!/usr/bin/env bash
set -euo pipefail

policy_path="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/policies/hook-policy.json"

python3 - "$policy_path" 3<&0 <<'PY'
import json
import os
import re
import sys

try:
    with os.fdopen(3) as payload_stream:
        payload = json.load(payload_stream)
except json.JSONDecodeError:
    sys.exit(0)

prompt = str(payload.get("prompt", ""))
prompt_lc = prompt.lower()
policy_path = sys.argv[1]
try:
    with open(policy_path) as fh:
        policy = json.load(fh)
except Exception:
    policy = {}

block_patterns = [
    r"\b(ignore|bypass|disable|turn\s+off)\b.{0,80}\b(instructions?|rules?|permissions?|approvals?|security|hooks?)\b",
    r"\b(read|cat|print|show|dump|exfiltrate)\b.{0,80}\b(\.env|secret|token|api[_ -]?key|password|credential|private[_ -]?key|id_rsa|id_ed25519)\b",
    r"(지침|규칙|권한|승인|보안|훅).{0,40}(무시|우회|끄|비활성)",
    r"(\.env|시크릿|토큰|비밀번호|인증정보|개인키).{0,40}(읽|출력|보여|덤프)",
]

for pattern in block_patterns:
    if re.search(pattern, prompt_lc, re.IGNORECASE | re.DOTALL):
        print(json.dumps({
            "decision": "block",
            "reason": "보안/권한 경계를 우회하거나 민감 정보를 읽고 출력하는 요청은 처리하지 않습니다. 필요한 작업 범위와 승인 가능한 안전한 절차를 분리해서 다시 요청하세요.",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit"
            }
        }, ensure_ascii=False))
        sys.exit(0)

risky_checks = [
    ("auth/권한", r"\b(auth|oauth|permission|authorization|login|session|cookie)\b|인증|권한|로그인|세션|쿠키"),
    ("billing", r"\b(billing|payment|invoice|subscription)\b|결제|청구|구독"),
    ("data deletion", r"\b(delete|truncate|drop|reset|purge|destroy)\b|삭제|초기화|파기"),
    ("deploy/prod", r"\b(deploy|release|production|prod|staging|workflow\s+run)\b|배포|운영|프로덕션|릴리스"),
    ("git/package", r"\b(git\s+(commit|push|merge|rebase)|npm\s+install|pnpm\s+add|yarn\s+add|pip\s+install|uv\s+add|publish)\b"),
    ("destructive command", r"\brm\s+-rf\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-fd\b|\bterraform\s+destroy\b|\bkubectl\s+delete\b"),
]

ambiguous_patterns = [
    r"^\s*(ㄱㄱ|go|do it|진행|해줘|처리해줘)\s*$",
    r"\b(fix|handle|clean\s+up|refactor|improve)\s+(it|everything|all)\b",
    r"(전부|다|전체).{0,20}(고쳐|처리|정리|개선)",
]

labels = [label for label, pattern in risky_checks if re.search(pattern, prompt_lc, re.IGNORECASE)]
for keyword in policy.get("approval_required", {}).get("request_keywords", []):
    if keyword.lower() in prompt_lc and "policy:" + keyword not in labels:
        labels.append("policy:" + keyword)
is_ambiguous = any(re.search(pattern, prompt_lc, re.IGNORECASE | re.DOTALL) for pattern in ambiguous_patterns)

if not labels and not is_ambiguous:
    sys.exit(0)

suffix = []
if labels:
    suffix.append("detected=" + ", ".join(labels[:4]))
if is_ambiguous:
    suffix.append("ambiguous_request=true")

context = (
    "Kit workflow reminder: for risky or ambiguous requests, inspect repo state and local instructions first, "
    "preserve unrelated dirty changes, keep security/approval gates intact, ask only when the missing approval "
    "cannot be discovered safely, and verify before claiming completion."
)
if suffix:
    context += " (" + "; ".join(suffix) + ")"

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context
    }
}, ensure_ascii=False))
PY
