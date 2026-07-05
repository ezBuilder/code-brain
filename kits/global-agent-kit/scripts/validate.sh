#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

required_files=(
  "CLAUDE.md"
  "README.md"
  "rules/CLAUDE.md"
  "rules/AGENTS.md"
  "docs/AI_ARCHITECTURE.md"
  "docs/AI_CONTEXT.md"
  "docs/AI_HOOKS.md"
  "docs/AI_SECURITY.md"
  "docs/AI_SUBAGENTS.md"
  "docs/AI_TESTING.md"
  "docs/AI_INTEGRATIONS.md"
  "docs/AI_DEV_LOOP.md"
  "docs/AI_RESEARCH.md"
  "docs/AI_EVOLUTION.md"
  "docs/AI_TOKEN_OPTIMIZATION.md"
  ".claudeignore"
  ".claude/settings.json"
  ".claude/hooks/block-dangerous.sh"
  ".claude/hooks/protect-secrets.sh"
  ".claude/hooks/session-context.sh"
  ".claude/hooks/user-prompt-submit.sh"
  ".claude/hooks/post-tool-use.sh"
  ".claude/commands/kit-doctor.md"
  ".claude/commands/kit-research.md"
  ".claude/commands/kit-upgrade-loop.md"
  ".claude/skills/lean-review/SKILL.md"
  ".claude/skills/lean-debt/SKILL.md"
  "scripts/doctor.sh"
  "scripts/codex-doctor.sh"
  "scripts/harness.sh"
  "scripts/research-snapshot.sh"
  "scripts/dev-loop.sh"
  "scripts/evolve-capture.sh"
  "scripts/evolve-promote.sh"
  "scripts/evolve-score.py"
  "scripts/evolve-snapshot.sh"
  "scripts/hook-policy-check.py"
  "policies/hook-policy.json"
)

for file in "${required_files[@]}"; do
  [[ -f "$file" ]] || { echo "missing required file: $file" >&2; exit 1; }
done

bash -n install.sh
bash -n scripts/validate.sh
bash -n scripts/doctor.sh
bash -n scripts/codex-doctor.sh
bash -n scripts/harness.sh
bash -n scripts/research-snapshot.sh
bash -n scripts/dev-loop.sh
bash -n scripts/evolve-capture.sh
bash -n scripts/evolve-promote.sh
bash -n scripts/evolve-snapshot.sh
python3 -m py_compile scripts/evolve-score.py
bash -n .claude/hooks/block-dangerous.sh
bash -n .claude/hooks/protect-secrets.sh
bash -n .claude/hooks/session-context.sh
bash -n .claude/hooks/user-prompt-submit.sh
bash -n .claude/hooks/post-tool-use.sh
python3 -m json.tool policies/hook-policy.json >/dev/null
python3 -m py_compile scripts/hook-policy-check.py
python3 scripts/hook-policy-check.py --command 'rm -rf /' >/dev/null && {
  echo "hook policy failed to deny destructive command" >&2
  exit 1
}

python3 -m json.tool .claude/settings.json >/dev/null

if find . -name .DS_Store -print | grep -q .; then
  echo ".DS_Store files must not be present in the kit" >&2
  find . -name .DS_Store -print >&2
  exit 1
fi

if rg -n '<(명령|path|domain|언어|npm|web|role|stack)' CLAUDE.md README.md rules docs .claude; then
  echo "unresolved placeholder remains" >&2
  exit 1
fi

if rg -n 'Cadence|cadence' CLAUDE.md README.md rules docs .claude; then
  echo "unvalidated Cadence guidance must not be present" >&2
  exit 1
fi

if ! rg -q "AI_ARCHITECTURE.md" CLAUDE.md README.md; then
  echo "AI_ARCHITECTURE.md is not indexed" >&2
  exit 1
fi

if ! rg -q "AI_INTEGRATIONS.md" CLAUDE.md README.md; then
  echo "AI_INTEGRATIONS.md is not indexed" >&2
  exit 1
fi

if ! rg -q "AI_DEV_LOOP.md" CLAUDE.md README.md; then
  echo "AI_DEV_LOOP.md is not indexed" >&2
  exit 1
fi

if ! rg -q "AI_HOOKS.md" CLAUDE.md README.md; then
  echo "AI_HOOKS.md is not indexed" >&2
  exit 1
fi

if ! rg -q "AI_RESEARCH.md" CLAUDE.md README.md; then
  echo "AI_RESEARCH.md is not indexed" >&2
  exit 1
fi

if ! rg -q "AI_EVOLUTION.md" README.md; then
  echo "AI_EVOLUTION.md is not indexed in README.md" >&2
  exit 1
fi

if ! rg -q "AI_TOKEN_OPTIMIZATION.md" CLAUDE.md README.md; then
  echo "AI_TOKEN_OPTIMIZATION.md is not indexed" >&2
  exit 1
fi

python3 - <<'PY'
from pathlib import Path

required_phrases = [
    "Prefer no code, stdlib/native, installed dependency, one-liner",
    "never remove validation, security, accessibility, data-loss handling, or explicit requirements",
    "cb-simplify: <ceiling>; revisit when <trigger>",
]

claude = Path("rules/CLAUDE.md").read_text(encoding="utf-8")
agents = Path("rules/AGENTS.md").read_text(encoding="utf-8")

for phrase in required_phrases:
    for label, text in (("rules/CLAUDE.md", claude), ("rules/AGENTS.md", agents)):
        if phrase not in text:
            raise SystemExit(f"{label} missing lean invariant: {phrase}")

def comparable(text):
    return (
        text.replace("# CLAUDE.md", "# RULE.md", 1)
        .replace("# AGENTS.md", "# RULE.md", 1)
        .replace("Global Claude rules.", "Global agent rules.", 1)
        .replace("Global Codex rules.", "Global agent rules.", 1)
    )

if comparable(claude) != comparable(agents):
    raise SystemExit("rules/CLAUDE.md and rules/AGENTS.md drifted beyond title")

for skill in ("lean-review", "lean-debt"):
    path = Path(".claude/skills") / skill / "SKILL.md"
    if not path.is_file():
        raise SystemExit(f"missing lean skill: {path}")
PY

python3 - <<'PY'
import json
from pathlib import Path

settings = json.loads(Path(".claude/settings.json").read_text())
deny = set(settings.get("permissions", {}).get("deny", []))
forbidden_deny = {
    "Read(./.env.*)",
    "Bash(git push *)",
    "Write",
    "Edit",
    "MultiEdit",
    "Bash",
}
found = sorted(deny & forbidden_deny)
if found:
    raise SystemExit(f"settings.json still contains over-broad deny rules: {found}")
PY

tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_home"' EXIT

mkdir -p "$tmp_home/.claude" "$tmp_home/.codex"
# Seed a legacy markerless copy of the rule above genuine user content, mirroring
# pre-marker installs. The installer must collapse it into one managed block
# rather than appending beside it (which duplicated the whole ruleset).
{ cat rules/CLAUDE.md; printf '\n\n# Personal Claude Rules\n\nKeep this custom Claude instruction.\n'; } >"$tmp_home/.claude/CLAUDE.md"
{ cat rules/AGENTS.md; printf '\n\n# Personal Codex Rules\n\nKeep this custom Codex instruction.\n'; } >"$tmp_home/.codex/AGENTS.md"
cat >"$tmp_home/.claude/settings.json" <<'JSON'
{
  "autoMemoryEnabled": true,
  "permissions": {
    "ask": [
      "Bash(custom deploy *)"
    ]
  },
  "hooks": {
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/bin/true"
          }
        ]
      }
    ]
  }
}
JSON

HOME="$tmp_home" ./install.sh --all --yes >/dev/null
HOME="$tmp_home" ./install.sh --all --yes >/dev/null
HOME="$tmp_home" ./scripts/doctor.sh >/dev/null
HOME="$tmp_home" ./scripts/codex-doctor.sh --self-test >/dev/null
HOME="$tmp_home" ./scripts/evolve-capture.sh --self-test >/dev/null
HOME="$tmp_home" ./scripts/evolve-score.py --self-test >/dev/null
HOME="$tmp_home" ./scripts/evolve-promote.sh --self-test >/dev/null
HOME="$tmp_home" ./scripts/evolve-snapshot.sh --self-test >/dev/null

test -f "$tmp_home/.claude/CLAUDE.md"
test -f "$tmp_home/.codex/AGENTS.md"
grep -q "Keep this custom Claude instruction." "$tmp_home/.claude/CLAUDE.md"
grep -q "Keep this custom Codex instruction." "$tmp_home/.codex/AGENTS.md"
test "$(grep -c 'code-brain-global-kit:start' "$tmp_home/.claude/CLAUDE.md")" -eq 1
test "$(grep -c 'code-brain-global-kit:start' "$tmp_home/.codex/AGENTS.md")" -eq 1
# Rule body must survive exactly once: legacy markerless copy collapsed, not duplicated.
test "$(grep -c 'If Code Brain is missing or stale, fall back and say so.' "$tmp_home/.claude/CLAUDE.md")" -eq 1
test "$(grep -c 'If Code Brain is missing or stale, fall back and say so.' "$tmp_home/.codex/AGENTS.md")" -eq 1
test -x "$tmp_home/.claude/hooks/block-dangerous.sh"
test -x "$tmp_home/.claude/hooks/protect-secrets.sh"
test -x "$tmp_home/.claude/hooks/session-context.sh"
test -x "$tmp_home/.claude/hooks/user-prompt-submit.sh"
test -x "$tmp_home/.claude/hooks/post-tool-use.sh"
test -f "$tmp_home/.claude/policies/hook-policy.json"
test -f "$tmp_home/.claude/agents/security-reviewer.md"
test -f "$tmp_home/.claude/skills/implement-feature/SKILL.md"
test -f "$tmp_home/.claude/skills/lean-review/SKILL.md"
test -f "$tmp_home/.claude/skills/lean-debt/SKILL.md"
test -f "$tmp_home/.claude/commands/kit-upgrade-loop.md"

python3 - "$tmp_home/.claude/settings.json" "$tmp_home/.claude/hooks" <<'PY'
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text())
hook_dir = Path(sys.argv[2])

if settings.get("autoMemoryEnabled") is not True:
    raise SystemExit("installer did not preserve existing top-level settings")

perms = settings.get("permissions", {})
ask = perms.get("ask", [])
if "Bash(custom deploy *)" not in ask:
    raise SystemExit("installer did not preserve existing permission ask rules")

deny = perms.get("deny", [])
if "Bash(git branch -D *)" not in deny or "Bash(git push * --force*)" not in deny:
    raise SystemExit("installer did not merge permission deny rules")

if perms.get("defaultMode") != "bypassPermissions":
    raise SystemExit("installer did not carry the bypassPermissions default mode")

notification = settings.get("hooks", {}).get("Notification", [])
if not notification:
    raise SystemExit("installer did not preserve existing hooks")

commands = []
for entries in settings.get("hooks", {}).values():
    for entry in entries:
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str):
                commands.append(command)

required = {
    str(hook_dir / "block-dangerous.sh"),
    str(hook_dir / "protect-secrets.sh"),
    str(hook_dir / "session-context.sh"),
    str(hook_dir / "user-prompt-submit.sh"),
    str(hook_dir / "post-tool-use.sh"),
}
missing = sorted(required - set(commands))
if missing:
    raise SystemExit(f"installer did not rewrite hook commands: {missing}")
PY

echo "validate ok"
