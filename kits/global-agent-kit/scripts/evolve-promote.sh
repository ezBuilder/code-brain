#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DRY_RUN=0
SELF_TEST=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/evolve-promote.sh --dry-run [--self-test]

Print a safe promotion proposal for Claude/Codex kit assets.
This command never writes to global Claude or Codex paths.

Options:
  --dry-run    Required. Print proposed install actions only.
  --self-test  Run local smoke tests in a temporary HOME.
USAGE
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --self-test) SELF_TEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$SELF_TEST" -eq 0 && "$DRY_RUN" -ne 1 ]]; then
  echo "promotion requires --dry-run; refusing to write global rules" >&2
  usage >&2
  exit 2
fi

require_file() {
  local path="$1"
  [[ -f "$path" ]] || { echo "required file missing: $path" >&2; exit 2; }
}

asset_status() {
  local source="$1"
  local target="$2"

  if [[ ! -e "$target" && ! -L "$target" ]]; then
    printf 'missing'
  elif [[ -f "$source" && -f "$target" ]] && cmp -s "$source" "$target"; then
    printf 'unchanged'
  elif [[ -d "$source" && -d "$target" ]] && diff -qr "$source" "$target" >/dev/null; then
    printf 'unchanged'
  else
    printf 'would-update'
  fi
}

print_asset() {
  local label="$1"
  local source="$2"
  local target="$3"
  local status

  if [[ -d "$source" ]]; then
    status="$(asset_status "$source" "$target")"
  else
    require_file "$source"
    status="$(asset_status "$source" "$target")"
  fi
  printf '%-18s %-12s %s -> %s\n' "$label" "$status" "$source" "$target"
}

run_proposal() {
  cat <<'EOF'
Code Brain Evolution promotion proposal (dry-run)

No global files will be written by this command.
Review the proposed changes, then use ./install.sh --all --dry-run or an explicit installer command if promotion is approved.

Assets:
EOF
  print_asset "Claude rule" "$ROOT_DIR/rules/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
  print_asset "Claude hooks" "$ROOT_DIR/.claude/hooks" "$HOME/.claude/hooks"
  print_asset "Claude policies" "$ROOT_DIR/policies" "$HOME/.claude/policies"
  print_asset "Claude agents" "$ROOT_DIR/.claude/agents" "$HOME/.claude/agents"
  print_asset "Claude skills" "$ROOT_DIR/.claude/skills" "$HOME/.claude/skills"
  print_asset "Claude commands" "$ROOT_DIR/.claude/commands" "$HOME/.claude/commands"
  print_asset "Claude settings" "$ROOT_DIR/.claude/settings.json" "$HOME/.claude/settings.json"
  print_asset "Codex rule" "$ROOT_DIR/rules/AGENTS.md" "$HOME/.codex/AGENTS.md"

  cat <<'EOF'

Suggested verification before any approved installer run:
  ./scripts/evolve-snapshot.sh snapshot
  ./install.sh --all --dry-run
  ./scripts/validate.sh
EOF
}

run_self_test() {
  local tmp_home
  tmp_home="$(mktemp -d)"
  trap "rm -rf '$tmp_home'" EXIT

  mkdir -p "$tmp_home/.claude" "$tmp_home/.codex"
  HOME="$tmp_home" "$0" --dry-run >/tmp/evolve-promote-self-test.out
  grep -q "No global files will be written" /tmp/evolve-promote-self-test.out
  grep -q "Codex rule" /tmp/evolve-promote-self-test.out
  echo "evolve-promote self-test ok"
}

if [[ "$SELF_TEST" -eq 1 ]]; then
  run_self_test
else
  run_proposal
fi
