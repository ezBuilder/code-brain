#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit"
SNAPSHOT_ROOT="$STATE_DIR/evolution-snapshots"
RETENTION="${CODE_BRAIN_GLOBAL_KIT_SNAPSHOT_RETENTION:-20}"

usage() {
  cat <<'USAGE'
Usage: ./scripts/evolve-snapshot.sh <command> [options]

Commands:
  snapshot                 Copy installed Claude/Codex kit assets into local state
  list                     List local snapshots
  restore-dry-run SNAPSHOT Print a restore plan only; never writes global paths
  --self-test              Run local smoke tests in a temporary HOME

Environment:
  CODE_BRAIN_GLOBAL_KIT_SNAPSHOT_RETENTION  Number of snapshots to keep; default 20, 0 disables pruning
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! [[ "$RETENTION" =~ ^[0-9]+$ ]]; then
  echo "CODE_BRAIN_GLOBAL_KIT_SNAPSHOT_RETENTION must be a non-negative integer" >&2
  exit 2
fi

is_secret_like_path() {
  local lower
  lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    *".env"*|*"id_rsa"*|*"id_ed25519"*|*"token"*|*"password"*|*"credential"*|*"private_key"*|*/secret/*|*/secrets/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

safe_copy() {
  local source="$1"
  local dest="$2"
  local entry rel target

  [[ -e "$source" || -L "$source" ]] || return 0
  if is_secret_like_path "$source"; then
    echo "skip possible secret path: $source" >&2
    return 0
  fi
  if [[ -L "$source" ]]; then
    echo "skip symlink path: $source" >&2
    return 0
  fi

  mkdir -p "$(dirname "$dest")"
  if [[ -d "$source" ]]; then
    mkdir -p "$dest"
    while IFS= read -r -d '' entry; do
      if is_secret_like_path "$entry" || [[ -L "$entry" ]]; then
        echo "skip unsafe snapshot path: $entry" >&2
        continue
      fi
      rel="${entry#$source/}"
      target="$dest/$rel"
      if [[ -d "$entry" ]]; then
        mkdir -p "$target"
      elif [[ -f "$entry" ]]; then
        mkdir -p "$(dirname "$target")"
        cp -p "$entry" "$target"
      fi
    done < <(find "$source" -mindepth 1 -print0)
  else
    cp -p "$source" "$dest"
  fi
  printf 'snapshot: %s -> %s\n' "$source" "$dest"
}

snapshot_assets() {
  local snapshot_dir="$1"
  mkdir -p "$snapshot_dir"

  cat >"$snapshot_dir/MANIFEST.txt" <<EOF
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
root_dir=$ROOT_DIR
mode=installed-assets-only
restore=restore-dry-run-only
EOF

  safe_copy "$HOME/.claude/CLAUDE.md" "$snapshot_dir/.claude/CLAUDE.md"
  safe_copy "$HOME/.claude/settings.json" "$snapshot_dir/.claude/settings.json"
  safe_copy "$HOME/.claude/hooks" "$snapshot_dir/.claude/hooks"
  safe_copy "$HOME/.claude/policies" "$snapshot_dir/.claude/policies"
  safe_copy "$HOME/.claude/agents" "$snapshot_dir/.claude/agents"
  safe_copy "$HOME/.claude/skills" "$snapshot_dir/.claude/skills"
  safe_copy "$HOME/.claude/commands" "$snapshot_dir/.claude/commands"
  safe_copy "$HOME/.codex/AGENTS.md" "$snapshot_dir/.codex/AGENTS.md"

  echo "snapshot complete: $snapshot_dir"
}

prune_snapshots() {
  [[ "$RETENTION" -eq 0 ]] && return 0
  [[ -d "$SNAPSHOT_ROOT" ]] || return 0

  python3 - "$SNAPSHOT_ROOT" "$RETENTION" <<'PY'
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
retention = int(sys.argv[2])
snapshots = sorted(
    [p for p in root.iterdir() if p.is_dir()],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
for old in snapshots[retention:]:
    if old.parent != root:
        raise SystemExit(f"refusing to prune outside snapshot root: {old}")
    shutil.rmtree(old)
    print(f"prune snapshot: {old}")
PY
}

resolve_snapshot() {
  local name="$1"
  if [[ "$name" = /* ]]; then
    printf '%s\n' "$name"
  else
    printf '%s/%s\n' "$SNAPSHOT_ROOT" "$name"
  fi
}

restore_line() {
  local snapshot_path="$1"
  local target="$2"
  local status

  if [[ ! -e "$snapshot_path" && ! -L "$snapshot_path" ]]; then
    return 0
  fi

  if [[ ! -e "$target" && ! -L "$target" ]]; then
    status="would-create"
  elif [[ -f "$snapshot_path" && -f "$target" ]] && cmp -s "$snapshot_path" "$target"; then
    status="unchanged"
  elif [[ -d "$snapshot_path" && -d "$target" ]] && diff -qr "$snapshot_path" "$target" >/dev/null; then
    status="unchanged"
  else
    status="would-update"
  fi

  printf '%-12s %s -> %s\n' "$status" "$snapshot_path" "$target"
}

restore_dry_run() {
  local snapshot_dir
  snapshot_dir="$(resolve_snapshot "$1")"
  [[ -d "$snapshot_dir" ]] || { echo "snapshot not found: $snapshot_dir" >&2; exit 2; }

  cat <<EOF
Code Brain Evolution restore dry-run

Snapshot: $snapshot_dir
No global files will be written. This command only prints the restore plan.

Assets:
EOF
  restore_line "$snapshot_dir/.claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
  restore_line "$snapshot_dir/.claude/settings.json" "$HOME/.claude/settings.json"
  restore_line "$snapshot_dir/.claude/hooks" "$HOME/.claude/hooks"
  restore_line "$snapshot_dir/.claude/policies" "$HOME/.claude/policies"
  restore_line "$snapshot_dir/.claude/agents" "$HOME/.claude/agents"
  restore_line "$snapshot_dir/.claude/skills" "$HOME/.claude/skills"
  restore_line "$snapshot_dir/.claude/commands" "$HOME/.claude/commands"
  restore_line "$snapshot_dir/.codex/AGENTS.md" "$HOME/.codex/AGENTS.md"
}

run_self_test() {
  local tmp_home snapshot_name
  tmp_home="$(mktemp -d)"
  trap "rm -rf '$tmp_home'" EXIT

  mkdir -p "$tmp_home/.claude/hooks" "$tmp_home/.codex"
  printf 'claude rule\n' >"$tmp_home/.claude/CLAUDE.md"
  printf '{}\n' >"$tmp_home/.claude/settings.json"
  printf '#!/usr/bin/env bash\nexit 0\n' >"$tmp_home/.claude/hooks/test.sh"
  chmod +x "$tmp_home/.claude/hooks/test.sh"
  printf 'codex rule\n' >"$tmp_home/.codex/AGENTS.md"

  HOME="$tmp_home" XDG_STATE_HOME="$tmp_home/state" "$0" snapshot >/tmp/evolve-snapshot-self-test.out
  snapshot_name="$(HOME="$tmp_home" XDG_STATE_HOME="$tmp_home/state" "$0" list | tail -n 1)"
  [[ -n "$snapshot_name" ]]
  HOME="$tmp_home" XDG_STATE_HOME="$tmp_home/state" "$0" restore-dry-run "$snapshot_name" >/tmp/evolve-restore-self-test.out
  grep -q "No global files will be written" /tmp/evolve-restore-self-test.out
  grep -q ".codex/AGENTS.md" /tmp/evolve-restore-self-test.out
  echo "evolve-snapshot self-test ok"
}

command="${1:-}"
case "$command" in
  snapshot)
    snapshot_dir="$SNAPSHOT_ROOT/$(date +%Y%m%d-%H%M%S)"
    snapshot_assets "$snapshot_dir"
    prune_snapshots
    ;;
  list)
    [[ -d "$SNAPSHOT_ROOT" ]] || exit 0
    find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1 -type d -print | sort
    ;;
  restore-dry-run)
    [[ $# -eq 2 ]] || { echo "restore-dry-run requires a snapshot name or path" >&2; usage >&2; exit 2; }
    restore_dry_run "$2"
    ;;
  --self-test)
    run_self_test
    ;;
  "")
    usage >&2
    exit 2
    ;;
  *)
    echo "unknown command: $command" >&2
    usage >&2
    exit 2
    ;;
esac
