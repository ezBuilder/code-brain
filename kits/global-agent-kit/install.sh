#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit"
BACKUP_DIR="$STATE_DIR/backups/$(date +%Y%m%d-%H%M%S)"
BACKUP_RETENTION="${CODE_BRAIN_GLOBAL_KIT_BACKUP_RETENTION:-20}"

INSTALL_CLAUDE=0
INSTALL_CODEX=0
INSTALL_CLAUDE_ASSETS=0
RULES_ONLY=0
DRY_RUN=0
YES=0
MANAGED_START="<!-- code-brain-global-kit:start -->"
MANAGED_END="<!-- code-brain-global-kit:end -->"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--claude|--codex|--all] [--rules-only] [--dry-run] [--yes]

Options:
  --claude   Install Claude global rule plus Claude Code settings, hooks, policies, agents, skills, and commands
  --codex    Install rules/AGENTS.md to ~/.codex/AGENTS.md
  --all      Install Claude assets and Codex global rules
  --rules-only
             Install only CLAUDE.md/AGENTS.md without Claude Code assets
  --dry-run  Print actions without writing files
  --yes      Do not prompt before installing
USAGE
}

while (($#)); do
  case "$1" in
    --claude) INSTALL_CLAUDE=1; INSTALL_CLAUDE_ASSETS=1 ;;
    --codex) INSTALL_CODEX=1 ;;
    --all) INSTALL_CLAUDE=1; INSTALL_CLAUDE_ASSETS=1; INSTALL_CODEX=1 ;;
    --rules-only) RULES_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$RULES_ONLY" -eq 1 ]]; then
  INSTALL_CLAUDE_ASSETS=0
fi

if ! [[ "$BACKUP_RETENTION" =~ ^[0-9]+$ ]]; then
  echo "CODE_BRAIN_GLOBAL_KIT_BACKUP_RETENTION must be a non-negative integer" >&2
  exit 2
fi

if [[ "$INSTALL_CLAUDE" -eq 0 && "$INSTALL_CODEX" -eq 0 ]]; then
  echo "select at least one target: --claude, --codex, or --all" >&2
  usage >&2
  exit 2
fi

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "required file missing: $path" >&2
    exit 2
  fi
}

confirm() {
  if [[ "$DRY_RUN" -eq 1 || "$YES" -eq 1 ]]; then
    return
  fi
  if [[ ! -t 0 ]]; then
    echo "refusing interactive install without --yes on non-TTY stdin" >&2
    exit 2
  fi
  printf 'Install global rules and back up existing files? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "aborted"; exit 1 ;;
  esac
}

install_rule() {
  local source="$1"
  local target="$2"
  local tmp_rule

  require_file "$source"
  echo "merge: $source -> $target"

  tmp_rule="$(mktemp)"
  python3 - "$source" "$target" "$tmp_rule" "$MANAGED_START" "$MANAGED_END" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
output = Path(sys.argv[3])
start = sys.argv[4]
end = sys.argv[5]

source_text = source.read_text(encoding="utf-8").strip() + "\n"
block = f"{start}\n{source_text}{end}\n"

if not target.exists():
    output.write_text(block, encoding="utf-8")
    raise SystemExit(0)

current = target.read_text(encoding="utf-8")
if current.strip() == source_text.strip():
    output.write_text(block, encoding="utf-8")
    raise SystemExit(0)

has_start = start in current
has_end = end in current
if has_start != has_end:
    raise SystemExit(f"managed block markers are incomplete in {target}")

if has_start:
    before, rest = current.split(start, 1)
    _, after = rest.split(end, 1)
    rendered = before.rstrip() + "\n\n" + block + after.lstrip("\n")
else:
    sep = "" if current.endswith("\n") else "\n"
    rendered = current + sep + "\n" + block

output.write_text(rendered, encoding="utf-8")
PY

  if [[ -f "$target" ]] && cmp -s "$tmp_rule" "$target"; then
    echo "skip unchanged: $target"
    rm -f "$tmp_rule"
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ -f "$target" ]]; then
      echo "backup: $target -> $BACKUP_DIR/$(basename "$target")"
    fi
    rm -f "$tmp_rule"
    return
  fi

  mkdir -p "$(dirname "$target")" "$BACKUP_DIR"
  if [[ -f "$target" ]]; then
    cp -p "$target" "$BACKUP_DIR/$(basename "$target")"
  fi

  install -m 0644 "$tmp_rule" "$target"
  rm -f "$tmp_rule"
  verify_managed_rule "$source" "$target"
}

verify_managed_rule() {
  local source="$1"
  local target="$2"

  python3 - "$source" "$target" "$MANAGED_START" "$MANAGED_END" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
start = sys.argv[3]
end = sys.argv[4]

source_text = source.read_text(encoding="utf-8").strip() + "\n"
current = target.read_text(encoding="utf-8")
if start not in current or end not in current:
    raise SystemExit(f"managed block missing in {target}")
body = current.split(start, 1)[1].split(end, 1)[0].lstrip("\n")
if body != source_text:
    raise SystemExit(f"managed block content mismatch in {target}")
PY
}

backup_path() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname "${target#$HOME/}")"
    cp -R -p "$target" "$BACKUP_DIR/${target#$HOME/}"
    echo "backup: $target -> $BACKUP_DIR/${target#$HOME/}"
  fi
}

install_dir() {
  local source="$1"
  local target="$2"

  if [[ ! -d "$source" ]]; then
    echo "required directory missing: $source" >&2
    exit 2
  fi

  echo "install: $source/ -> $target/"
  if [[ -d "$target" ]] && ! find "$source" -type f -print0 | while IFS= read -r -d '' file; do
    rel="${file#$source/}"
    cmp -s "$file" "$target/$rel" || exit 1
  done; then
    :
  elif [[ -d "$target" ]]; then
    echo "skip unchanged: $target/"
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ -e "$target" ]]; then
      echo "backup: $target -> $BACKUP_DIR/${target#$HOME/}"
    fi
    return
  fi

  backup_path "$target"
  mkdir -p "$target"
  cp -R -p "$source"/. "$target"/
}

install_claude_settings() {
  local source="$ROOT_DIR/.claude/settings.json"
  local target="$HOME/.claude/settings.json"
  local tmp_settings

  require_file "$source"
  echo "install: $source -> $target"

  tmp_settings="$(mktemp)"
  trap 'rm -f "$tmp_settings"' RETURN
  python3 - "$source" "$target" "$HOME/.claude/hooks" "$tmp_settings" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
hook_dir = Path(sys.argv[3])
output = Path(sys.argv[4])

incoming = json.loads(source.read_text())
if target.exists():
    current = json.loads(target.read_text())
else:
    current = {}

for entries in incoming.get("hooks", {}).values():
    for entry in entries:
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str):
                hook["command"] = command.replace("./.claude/hooks", str(hook_dir))

merged = dict(current)

permissions = dict(current.get("permissions", {}))
incoming_permissions = incoming.get("permissions", {})
for key in ("deny", "ask", "allow"):
    values = []
    seen = set()
    for item in permissions.get(key, []) + incoming_permissions.get(key, []):
        if item not in seen:
            seen.add(item)
            values.append(item)
    if values:
        permissions[key] = values
if permissions:
    merged["permissions"] = permissions

def _dedupe_event(entry_list):
    # Merge entries that share a matcher (dedupe hook commands within) so an updated
    # kit entry — e.g. a hook command added to an existing matcher group — collapses
    # in place instead of accumulating duplicate matcher groups across re-installs.
    by_matcher = {}
    ordered = []
    for entry in entry_list:
        matcher = entry.get("matcher")
        target = by_matcher.get(matcher)
        if target is None:
            copied = dict(entry)
            copied["hooks"] = list(entry.get("hooks", []))
            by_matcher[matcher] = copied
            ordered.append(copied)
            continue
        seen_cmds = {h.get("command") for h in target.get("hooks", [])}
        for h in entry.get("hooks", []):
            if h.get("command") not in seen_cmds:
                target["hooks"].append(h)
                seen_cmds.add(h.get("command"))
    return ordered

hooks = {}
events = list(current.get("hooks", {}))
for event in incoming.get("hooks", {}):
    if event not in hooks and event not in events:
        events.append(event)
for event in events:
    combined = list(current.get("hooks", {}).get(event, [])) + list(incoming.get("hooks", {}).get(event, []))
    hooks[event] = _dedupe_event(combined)
if hooks:
    merged["hooks"] = hooks

for key, value in incoming.items():
    if key not in {"permissions", "hooks"} and key not in merged:
        merged[key] = value

output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
PY

  if [[ -f "$target" ]] && cmp -s "$tmp_settings" "$target"; then
    echo "skip unchanged: $target"
    rm -f "$tmp_settings"
    trap - RETURN
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ -f "$target" ]]; then
      echo "backup: $target -> $BACKUP_DIR/${target#$HOME/}"
    fi
    rm -f "$tmp_settings"
    trap - RETURN
    return
  fi

  mkdir -p "$(dirname "$target")" "$BACKUP_DIR"
  backup_path "$target"
  install -m 0644 "$tmp_settings" "$target"
  rm -f "$tmp_settings"
  trap - RETURN
  python3 -m json.tool "$target" >/dev/null
}

verify_install() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return
  fi

  if [[ "$INSTALL_CLAUDE" -eq 1 ]]; then
    verify_managed_rule "$ROOT_DIR/rules/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
  fi

  if [[ "$INSTALL_CLAUDE_ASSETS" -eq 1 ]]; then
    test -x "$HOME/.claude/hooks/block-dangerous.sh"
    test -x "$HOME/.claude/hooks/protect-secrets.sh"
    test -x "$HOME/.claude/hooks/session-context.sh"
    test -x "$HOME/.claude/hooks/user-prompt-submit.sh"
    test -x "$HOME/.claude/hooks/post-tool-use.sh"
    test -f "$HOME/.claude/policies/hook-policy.json"
    test -f "$HOME/.claude/agents/security-reviewer.md"
    test -f "$HOME/.claude/skills/implement-feature/SKILL.md"
    test -f "$HOME/.claude/commands/kit-upgrade-loop.md"
    python3 -m json.tool "$HOME/.claude/settings.json" >/dev/null
    python3 - "$HOME/.claude/settings.json" "$HOME/.claude/hooks" <<'PY'
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
    raise SystemExit(f"installed settings missing hook commands: {missing}")
PY
  fi

  if [[ "$INSTALL_CODEX" -eq 1 ]]; then
    verify_managed_rule "$ROOT_DIR/rules/AGENTS.md" "$HOME/.codex/AGENTS.md"
  fi
}

prune_backups() {
  if [[ "$DRY_RUN" -eq 1 || "$BACKUP_RETENTION" -eq 0 ]]; then
    return
  fi

  local backup_root="$STATE_DIR/backups"
  [[ -d "$backup_root" ]] || return

  python3 - "$backup_root" "$BACKUP_RETENTION" <<'PY'
import shutil
import sys
from pathlib import Path

backup_root = Path(sys.argv[1]).resolve()
retention = int(sys.argv[2])
backups = sorted(
    [p for p in backup_root.iterdir() if p.is_dir()],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
for old_backup in backups[retention:]:
    if old_backup.parent != backup_root:
        raise SystemExit(f"refusing to prune outside backup root: {old_backup}")
    shutil.rmtree(old_backup)
    print(f"prune backup: {old_backup}")
PY
}

confirm

if [[ "$INSTALL_CLAUDE" -eq 1 ]]; then
  install_rule "$ROOT_DIR/rules/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
fi

if [[ "$INSTALL_CLAUDE_ASSETS" -eq 1 ]]; then
  install_dir "$ROOT_DIR/.claude/hooks" "$HOME/.claude/hooks"
  install_dir "$ROOT_DIR/policies" "$HOME/.claude/policies"
  install_dir "$ROOT_DIR/.claude/agents" "$HOME/.claude/agents"
  install_dir "$ROOT_DIR/.claude/skills" "$HOME/.claude/skills"
  install_dir "$ROOT_DIR/.claude/commands" "$HOME/.claude/commands"
  install_claude_settings
fi

if [[ "$INSTALL_CODEX" -eq 1 ]]; then
  install_rule "$ROOT_DIR/rules/AGENTS.md" "$HOME/.codex/AGENTS.md"
fi

verify_install
prune_backups

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run complete"
else
  echo "install complete; backups: $BACKUP_DIR"
fi
