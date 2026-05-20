#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGETS=()
TARGET_COUNT=0
while (($#)); do
  case "$1" in
    --target)
      shift
      [[ $# -gt 0 ]] || { echo "--target requires a path" >&2; exit 2; }
      TARGETS+=("$1")
      TARGET_COUNT=$((TARGET_COUNT + 1))
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./scripts/doctor.sh [--target /path/to/repo]...

Checks global Claude/Codex installation and optional project-local Claude overrides.
USAGE
      exit 0
      ;;
    *)
      TARGETS+=("$1")
      TARGET_COUNT=$((TARGET_COUNT + 1))
      ;;
  esac
  shift
done

status=0

ok() {
  printf 'ok: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1" >&2
  status=1
}

check_file() {
  local path="$1"
  local label="$2"
  if [[ -f "$path" ]]; then
    ok "$label"
  else
    fail "$label missing: $path"
  fi
}

check_executable() {
  local path="$1"
  local label="$2"
  if [[ -x "$path" ]]; then
    ok "$label"
  else
    fail "$label missing or not executable: $path"
  fi
}

check_file "$HOME/.claude/CLAUDE.md" "Claude global rule"
check_file "$HOME/.codex/AGENTS.md" "Codex global rule"
check_file "$HOME/.claude/settings.json" "Claude settings"
check_executable "$HOME/.claude/hooks/block-dangerous.sh" "dangerous command hook"
check_executable "$HOME/.claude/hooks/protect-secrets.sh" "secret protection hook"
check_executable "$HOME/.claude/hooks/session-context.sh" "session context hook"
check_executable "$HOME/.claude/hooks/user-prompt-submit.sh" "user prompt dispatcher hook"
check_executable "$HOME/.claude/hooks/post-tool-use.sh" "post tool follow-up hook"
check_file "$HOME/.claude/policies/hook-policy.json" "Claude hook policy"
check_file "$HOME/.claude/agents/security-reviewer.md" "security reviewer agent"
check_file "$HOME/.claude/skills/implement-feature/SKILL.md" "implement-feature skill"
check_file "$HOME/.claude/commands/kit-upgrade-loop.md" "kit upgrade loop command"

if [[ -f "$HOME/.claude/settings.json" ]]; then
  python3 - "$HOME/.claude/settings.json" "$HOME/.claude/hooks" <<'PY' || status=1
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text())
hook_dir = Path(sys.argv[2])
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
    print(f"fail: Claude settings missing hook commands: {missing}", file=sys.stderr)
    raise SystemExit(1)
print("ok: Claude hook commands wired")
PY
fi

for ((i = 0; i < TARGET_COUNT; i++)); do
  target="${TARGETS[$i]}"
  settings="$target/.claude/settings.json"
  if [[ ! -f "$settings" ]]; then
    ok "$target has no project .claude/settings.json override"
    continue
  fi

  python3 - "$settings" "$target" <<'PY' || status=1
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
target = sys.argv[2]
settings = json.loads(settings_path.read_text())
deny = set(settings.get("permissions", {}).get("deny", []))
forbidden = {"Write", "Edit", "MultiEdit", "Bash"}
found = sorted(deny & forbidden)
if found:
    print(f"fail: {target} blocks direct tools in .claude/settings.json: {found}", file=sys.stderr)
    raise SystemExit(1)
print(f"ok: {target} project Claude override")
PY
done

if [[ -x "$ROOT_DIR/scripts/codex-doctor.sh" ]]; then
  "$ROOT_DIR/scripts/codex-doctor.sh" >/dev/null || status=1
  if [[ "$status" -eq 0 ]]; then
    ok "Codex config doctor"
  fi
fi

exit "$status"
