#!/usr/bin/env bash
set -euo pipefail
umask 077

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-install}"

# Host Python for this installer's inline scripts. Prefer the source runtime's
# venv interpreter: merge_antigravity_mcp_json imports ai_core, whose
# requires-python is >=3.11, so an older system python3 (e.g. macOS ships 3.9)
# would fail to import it. Fall back to uv's project python, then any
# python3/python. These scripts used to call bare `python`, which is absent on
# systems that ship only `python3`.
py() {
  if [[ -x "$SOURCE_ROOT/.ai/runtime/.venv/bin/python" ]]; then
    "$SOURCE_ROOT/.ai/runtime/.venv/bin/python" "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run --project "$SOURCE_ROOT/.ai/runtime" python "$@"
  else
    local _py
    _py="$(command -v python3 || command -v python || true)"
    if [[ -z "$_py" ]]; then
      echo "install-into failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/install-into.sh <target-git-repo>
  scripts/install-into.sh install <target-git-repo>
  scripts/install-into.sh upgrade <target-git-repo>
  scripts/install-into.sh uninstall <target-git-repo>

Installs, upgrades, or removes Code Brain in an existing project.
Managed files are recorded in .ai/generated/install-manifest.json.
Existing unrelated target files are never overwritten.
EOF
}

if [[ "$ACTION" == "-h" || "$ACTION" == "--help" ]]; then
  usage
  exit 2
fi

if [[ "$ACTION" == "install" || "$ACTION" == "upgrade" || "$ACTION" == "uninstall" ]]; then
  TARGET_ARG="${2:-}"
else
  ACTION="install"
  TARGET_ARG="${1:-}"
fi

if [[ -z "$TARGET_ARG" ]]; then
  usage
  exit 2
fi

TARGET_ROOT="$(cd "$TARGET_ARG" && pwd -P)"

if ! git -C "$TARGET_ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "install-into failed: target is not inside a git repository: $TARGET_ROOT" >&2
  echo "  hint: run 'git init' in the target, then re-run install-into" >&2
  exit 2
fi

TARGET_TOP="$(cd "$(git -C "$TARGET_ROOT" rev-parse --show-toplevel)" && pwd -P)"
if [[ "$TARGET_TOP" != "$TARGET_ROOT" ]]; then
  echo "install-into failed: pass the git repository root: $TARGET_TOP" >&2
  exit 2
fi

managed_files() {
  (
    cd "$SOURCE_ROOT"
    {
      if git rev-parse --show-toplevel >/dev/null 2>&1; then
        git ls-files --cached --others --exclude-standard -- \
          .ai \
          .githooks \
          .claude/commands \
          .codex/prompts \
          .agents/skills \
          scripts/env-check.sh \
          scripts/preflight.sh
      else
        for path in .ai .githooks .claude/commands .codex/prompts .agents/skills; do
          [[ -e "$path" ]] && find "$path" -type f
        done
        for path in scripts/env-check.sh scripts/preflight.sh; do
          [[ -f "$path" ]] && printf '%s\n' "$path"
        done
      fi
    } | grep -vxE "\.ai/secret_scan_allowlist\.txt|\.ai/generated/install-manifest\.json" \
      | awk '!(($0 ~ /^\.ai\/memory\// || $0 ~ /^\.ai\/runtime\/state\//) && $0 !~ /\.gitkeep$/)' \
      | while IFS= read -r rel; do
        [[ -f "$rel" ]] && printf '%s\n' "$rel"
      done
  ) || true
  # ^ never propagate the SOURCE repo's private runtime memory/state DATA (audit chain, decisions,
  #   sessions, evidence, prompt-growth, worker heartbeats). Seeding it pollutes the target project
  #   and corrupts its audit chain. Directory structure still propagates via the .gitkeep files,
  #   which ARE kept; the runtime creates each project's own memory on first use.
  printf '%s\n' "bootstrap-code-brain.sh"
}

# User-owned files seeded on first install but never managed afterwards.
# Manifest does NOT track these — uninstall will leave them alone.
# Root agent instruction files are seeded when missing; if the target already
# has user-authored instructions (common in long-lived repos), we never touch
# them — those files are part of the project's contract, not Code Brain's.
seed_user_owned_files() {
  local seeds=(".ai/secret_scan_allowlist.txt")
  for rel in "${seeds[@]}"; do
    local src="$SOURCE_ROOT/$rel"
    local dst="$TARGET_ROOT/$rel"
    if [[ -f "$src" && ! -e "$dst" ]]; then
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    fi
  done
  seed_agents_md
  seed_claude_md
}

# Root AGENTS.md/CLAUDE.md mirror the tracked .ai/AGENTS.md contract so agents
# that only auto-load one filename still receive the same rules. They are seed-only
# for user-authored targets and git-ignored to avoid churn.
seed_agents_md() {
  local dst="$TARGET_ROOT/AGENTS.md"
  if [[ ! -e "$dst" ]] || is_code_brain_agents_stub "$dst"; then
    write_agent_contract "$dst"
  fi
  local gi="$TARGET_ROOT/.gitignore"
  if [[ -f "$gi" ]]; then
    grep -qxF '/AGENTS.md' "$gi" 2>/dev/null || printf '\n# Code Brain-managed auto-loaded memory mirror (regenerated each session)\n/AGENTS.md\n' >>"$gi"
  else
    printf '/AGENTS.md\n' >"$gi"
  fi
}

is_code_brain_agents_stub() {
  local dst="$1"
  [[ -f "$dst" ]] && grep -qxF 'Canonical agent instructions live in `.ai/AGENTS.md`.' "$dst"
}

is_code_brain_claude_stub() {
  local dst="$1"
  [[ -f "$dst" ]] && {
    grep -qxF 'Canonical Claude instructions live in `.ai/AGENTS.md`.' "$dst" \
      || grep -qxF 'Full repo-local contract: `.ai/AGENTS.md`.' "$dst"
  }
}

write_agent_contract() {
  local dst="$1"
  if [[ -f "$SOURCE_ROOT/.ai/AGENTS.md" ]]; then
    cp "$SOURCE_ROOT/.ai/AGENTS.md" "$dst"
  else
    cat >"$dst" <<'MD'
# Code Brain Agent Contract

Repo-local agent contract missing: `.ai/AGENTS.md`.
MD
  fi
}

seed_claude_md() {
  local dst="$TARGET_ROOT/CLAUDE.md"
  if [[ ! -e "$dst" ]] || is_code_brain_claude_stub "$dst"; then
    write_agent_contract "$dst"
  fi
}

merged_config_files() {
  printf '%s\n' ".mcp.json" ".codex/config.toml" ".agents/mcp_config.json" ".agents/hooks.json"
}

manifest_path() {
  printf '%s\n' "$TARGET_ROOT/.ai/generated/install-manifest.json"
}

legacy_code_brain_install() {
  [[ -x "$TARGET_ROOT/.ai/bin/ai" || -f "$TARGET_ROOT/.ai/AGENTS.md" ]]
}

copy_managed_files() {
  # Materialize the complete managed set in one Python process. The previous
  # Bash loop spawned dirname/mkdir/cp for every file (300 files in this repo),
  # which dominated fresh-install time on macOS. This preserves the existing
  # manifest/legacy/marker overwrite rules while adding symlink confinement.
  py -c '
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path

source_root = Path(sys.argv[1]).resolve()
target_root = Path(sys.argv[2]).resolve()
action = sys.argv[3]
manifest = Path(sys.argv[4])
rels = [line.strip() for line in sys.stdin if line.strip()]

manifest_files: set[str] = set()
if manifest.is_file():
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_files = {item for item in payload.get("files", []) if isinstance(item, str)}
    except Exception:
        manifest_files = set()

legacy_install = os.access(target_root / ".ai" / "bin" / "ai", os.X_OK) or (
    target_root / ".ai" / "AGENTS.md"
).is_file()
managed_prefixes = (
    ".ai/",
    ".githooks/",
    ".claude/commands/",
    ".codex/prompts/",
    ".agents/skills/",
)
managed_exact = {"scripts/env-check.sh", "scripts/preflight.sh"}


def confined(root: Path, path: Path, rel: str, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        print(f"install-into failed: {label} path escapes project root: {rel}", file=sys.stderr)
        raise SystemExit(3)
    return resolved


def is_managed_existing(rel: str, dst: Path) -> bool:
    if rel in manifest_files:
        return True
    managed_rel = rel in managed_exact or rel.startswith(managed_prefixes)
    if action == "upgrade" and not manifest.is_file() and legacy_install and managed_rel:
        return True
    try:
        return b"managed-by: code-brain" in dst.read_bytes()
    except OSError:
        return False


for rel in rels:
    rel_path = Path(rel)
    if rel == "bootstrap-code-brain.sh":
        continue
    if rel_path.is_absolute() or ".." in rel_path.parts:
        print(f"install-into failed: invalid managed path {rel}", file=sys.stderr)
        raise SystemExit(3)
    src = source_root / rel_path
    dst = target_root / rel_path
    src_resolved = confined(source_root, src, rel, "source")
    dst_resolved = confined(target_root, dst, rel, "target")
    if src_resolved == dst_resolved:
        continue
    if action == "upgrade" and rel.startswith(".ai/memory/") and dst.exists():
        continue
    if not src.is_file():
        print(f"install-into failed: missing source file {rel}", file=sys.stderr)
        raise SystemExit(2)
    if dst.exists():
        if not dst.is_file():
            print(f"install-into failed: refusing to overwrite non-file target {rel}", file=sys.stderr)
            raise SystemExit(3)
        identical = filecmp.cmp(src, dst, shallow=False)
        if not identical and not is_managed_existing(rel, dst):
            print(
                f"install-into failed: refusing to overwrite existing untracked target file {rel}",
                file=sys.stderr,
            )
            raise SystemExit(3)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=True)
' "$SOURCE_ROOT" "$TARGET_ROOT" "$ACTION" "$(manifest_path)" < <(managed_files)
}

write_bootstrap() {
  local src="$SOURCE_ROOT/bootstrap-code-brain.sh"
  if [[ ! -f "$src" ]]; then
    echo "install-into failed: missing source file bootstrap-code-brain.sh" >&2
    exit 2
  fi
  cp "$src" "$TARGET_ROOT/bootstrap-code-brain.sh"
  chmod +x "$TARGET_ROOT/bootstrap-code-brain.sh"
}

write_install_manifest() {
  mkdir -p "$TARGET_ROOT/.ai/generated"
  py -c '
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
source_root = Path(sys.argv[2])
files = [line.strip() for line in sys.stdin if line.strip()]
try:
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip()
except Exception:
    source = None
source_repo_url = os.environ.get("CODE_BRAIN_REPO_URL")
if not source_repo_url:
    try:
        source_repo_url = subprocess.check_output(["git", "config", "--get", "remote.origin.url"], cwd=source_root, text=True).strip() or None
    except Exception:
        source_repo_url = None
source_ref = os.environ.get("CODE_BRAIN_REF")
if not source_ref:
    try:
        source_ref = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=source_root, text=True).strip() or None
    except Exception:
        source_ref = None
payload = {
    "schema_version": 2,
    "tool": "code-brain",
    "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "project": root.name,
    "files": sorted(set(files)),
    "merged_config_files": [".mcp.json", ".codex/config.toml", ".claude/settings.json", ".codex/hooks.json", ".agents/mcp_config.json", ".agents/hooks.json"],
    "source_git_sha": source,
    "source_ref": source_ref,
    "source_repo_url": source_repo_url,
    "upgrade_channel": "github" if source_repo_url else "local",
    "upgrade_command": ".ai/bin/ai upgrade latest --json",
}
print(json.dumps(payload, indent=2, sort_keys=True))
' "$TARGET_ROOT" "$SOURCE_ROOT" >"$(manifest_path)" < <(managed_files)
}

restore_managed_owner_if_root() {
  if [[ "$(id -u)" != "0" ]]; then
    return 0
  fi
  if [[ ! -e "$TARGET_ROOT/.ai" ]]; then
    return 0
  fi
  local owner_spec
  owner_spec="$(stat -c '%u:%g' "$TARGET_ROOT/.ai" 2>/dev/null || stat -f '%u:%g' "$TARGET_ROOT/.ai" 2>/dev/null || true)"
  if [[ -z "$owner_spec" ]]; then
    return 0
  fi
  # Sanity check: if .ai/ owner is a UID that does not exist on this host
  # (typically a macOS UID 501 transplanted to a linux host via rsync/cp -a),
  # propagating that UID to every chown call leaves every file unreadable.
  # Honor AI_INSTALL_OWNER if set; otherwise fall back to the SUDO_USER (when
  # run via sudo), then the invoker's own UID. Skip recursive chown only when
  # we genuinely cannot determine a safe owner.
  local _uid="${owner_spec%%:*}"
  if ! getent passwd "$_uid" >/dev/null 2>&1; then
    local _fallback=""
    if [[ -n "${AI_INSTALL_OWNER:-}" ]]; then
      _fallback="$AI_INSTALL_OWNER"
    elif [[ -n "${SUDO_USER:-}" ]] && getent passwd "$SUDO_USER" >/dev/null 2>&1; then
      _fallback="$SUDO_USER:$SUDO_USER"
    fi
    if [[ -n "$_fallback" ]]; then
      echo "install-into: .ai/ owner UID $_uid not on this host; falling back to $_fallback (override with AI_INSTALL_OWNER)" >&2
      owner_spec="$_fallback"
    else
      echo "install-into: skipping owner restore — .ai/ owner UID $_uid unknown and no AI_INSTALL_OWNER/SUDO_USER fallback" >&2
      return 0
    fi
  fi
  local path
  # Chown the entire .ai/ tree so any subdirectory created since the previous
  # upgrade (precall_rules, skills, agents_catalog, ...) ends up readable by
  # the target user. Restricting to a hand-maintained allowlist regressed
  # before — when a new subdir was added in a later release, the original
  # target owner lost read access on root-run upgrades.
  #
  # IMPORTANT: exclude .ai/runtime/.venv — venvs are owner-sensitive (pyvenv.cfg,
  # site-packages, bin/python shebang resolution all assume a stable owner).
  # A blanket chown -R caused hook failures across already-installed targets
  # (observed user-visible symptom: "hook venv 오류" requiring sudo rm -rf
  # .ai/runtime/.venv as recovery). The venv is created/owned by the user who
  # first ran `uv sync` and must stay that way.
  if [[ -e "$TARGET_ROOT/.ai" ]]; then
    if [[ -d "$TARGET_ROOT/.ai/runtime/.venv" ]]; then
      find "$TARGET_ROOT/.ai" \
        -path "$TARGET_ROOT/.ai/runtime/.venv" -prune \
        -o -exec chown "$owner_spec" {} +
      # Selectively repair editable-install artifacts left as root by a previous
      # root-run `uv sync`. Three artifacts block `import ai_core` when owned by
      # root with mode 600: the editable .pth, the dist-info dir, and bin/ai.
      # Touching only these keeps the venv binaries themselves owner-stable.
      local _uid="${owner_spec%%:*}"
      find "$TARGET_ROOT/.ai/runtime/.venv/lib" -name "*.pth" \
        -not -uid "$_uid" -exec chown "$owner_spec" {} + 2>/dev/null || true
      find "$TARGET_ROOT/.ai/runtime/.venv/lib" -type d -name "*.dist-info" \
        -not -uid "$_uid" -exec chown -R "$owner_spec" {} + 2>/dev/null || true
      if [[ -f "$TARGET_ROOT/.ai/runtime/.venv/bin/ai" ]]; then
        local _bin_uid
        _bin_uid="$(stat -c '%u' "$TARGET_ROOT/.ai/runtime/.venv/bin/ai" 2>/dev/null || stat -f '%u' "$TARGET_ROOT/.ai/runtime/.venv/bin/ai" 2>/dev/null || echo "$_uid")"
        if [[ "$_bin_uid" != "$_uid" ]]; then
          chown "$owner_spec" "$TARGET_ROOT/.ai/runtime/.venv/bin/ai" 2>/dev/null || true
        fi
      fi
    else
      chown -R "$owner_spec" "$TARGET_ROOT/.ai"
    fi
  fi
  for path in \
    "$TARGET_ROOT/.githooks" \
    "$TARGET_ROOT/.claude/commands" \
    "$TARGET_ROOT/.codex/prompts"
  do
    if [[ -e "$path" ]]; then
      chown -R "$owner_spec" "$path"
    fi
  done
  while IFS= read -r rel; do
    if [[ "$rel" == .ai/memory/* ]]; then
      continue
    fi
    if [[ -e "$TARGET_ROOT/$rel" ]]; then
      chown "$owner_spec" "$TARGET_ROOT/$rel"
    fi
  done < <(managed_files)
  for path in \
    "$TARGET_ROOT/.mcp.json" \
    "$TARGET_ROOT/.codex/config.toml" \
    "$TARGET_ROOT/.codex/hooks.json" \
    "$TARGET_ROOT/.claude/settings.json" \
    "$TARGET_ROOT/.agents" \
    "$TARGET_ROOT/.agents/mcp_config.json" \
    "$TARGET_ROOT/.agents/hooks.json" \
    "$TARGET_ROOT/.agents/skills" \
    "$TARGET_ROOT/AGENTS.md" \
    "$TARGET_ROOT/CLAUDE.md" \
    "$TARGET_ROOT/bootstrap-code-brain.sh"
  do
    if [[ -e "$path" ]]; then
      chown -R "$owner_spec" "$path" 2>/dev/null || chown "$owner_spec" "$path"
    fi
  done
}

configure_project() {
  py - "$TARGET_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = root / ".ai" / "config.yaml"
text = config.read_text(encoding="utf-8")
lines = []
replaced = False
for line in text.splitlines():
    if line.startswith("project_name:"):
        lines.append(f"project_name: {root.name}")
        replaced = True
    else:
        lines.append(line)
if not replaced:
    lines.insert(1, f"project_name: {root.name}")
config.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

merge_mcp_json() {
  local dst="$TARGET_ROOT/.mcp.json"
  py - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
# Compact tools on by default: tools/list ships only the ~15 hot core tools; the rest load on
# demand via tool_search. Big per-session schema-token cut, no capability loss. (AI_MCP_COMPACT_TOOLS)
desired = {"command": ".ai/bin/ai-mcp", "args": [], "env": {"AI_MCP_COMPACT_TOOLS": "1"}}
if dst.exists():
    try:
        payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"install-into failed: existing {dst} is not valid JSON")
    if not isinstance(payload, dict):
        raise SystemExit(f"install-into failed: existing {dst} is not a JSON object")
else:
    payload = {}
servers = payload.setdefault("mcpServers", {})
if not isinstance(servers, dict):
    raise SystemExit(f"install-into failed: existing {dst}.mcpServers must be a JSON object")
servers["code-brain"] = desired
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

merge_codex_config() {
  local dst="$TARGET_ROOT/.codex/config.toml"
  py - "$dst" <<'PY'
import re
import sys
from pathlib import Path

dst = Path(sys.argv[1])
block = (
    "[mcp_servers.code-brain]\n"
    "command = \".ai/bin/ai-mcp\"\n"
    "args = []\n"
    # Compact tools on by default (parity with .mcp.json): only hot core tools in tools/list,
    # rest load on demand via tool_search. Per-session schema-token cut, no capability loss.
    "env = { AI_CODE_BRAIN_PROFILE = \"usage\", AI_MCP_COMPACT_TOOLS = \"1\" }\n"
)
existing = dst.read_text(encoding="utf-8") if dst.exists() else ""

def strip_section(text: str, header: str) -> str:
    """Remove a top-level TOML section by scanning lines, not regex.
    Section starts at a line equal to header (whitespace-trimmed) and ends at
    the next line that begins a new TOML table header `[` at column 0.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == header:
            i += 1
            while i < len(lines):
                nxt = lines[i].lstrip()
                if nxt.startswith("[") and not nxt.startswith("[]"):
                    break
                i += 1
            # Drop trailing blank lines that belonged to the removed section.
            while out and out[-1].strip() == "":
                out.pop()
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)

cleaned = strip_section(existing, "[mcp_servers.code-brain]").rstrip()
# Drop any orphan empty `[]` lines left by older buggy merges.
cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip() != "[]").rstrip()
if cleaned:
    new_text = cleaned + "\n\n" + block
else:
    new_text = block

def ensure_features_hooks(text: str) -> str:
    """Idempotently set [features].hooks = true and migrate the deprecated
    `codex_hooks` key to `hooks` if present, without disturbing other
    user-defined keys in the [features] table or other sections."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    found_section = False
    set_in_section = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped == "[features]":
            found_section = True
            out.append(line)
            i += 1
            section_lines: list[str] = []
            while i < n:
                inner = lines[i]
                inner_stripped = inner.lstrip()
                if inner_stripped.startswith("[") and not inner_stripped.startswith("[]"):
                    break
                section_lines.append(inner)
                i += 1
            # Drop any deprecated `codex_hooks` lines (migrated to `hooks`).
            section_lines = [
                sl for sl in section_lines
                if not (sl.strip().startswith("codex_hooks") and "=" in sl.strip())
            ]
            replaced = False
            for j, sl in enumerate(section_lines):
                sl_stripped = sl.strip()
                if sl_stripped.startswith("hooks") and "=" in sl_stripped:
                    section_lines[j] = "hooks = true"
                    replaced = True
                    break
            if not replaced:
                # Append before trailing blank lines so the file stays tidy.
                while section_lines and section_lines[-1].strip() == "":
                    section_lines.pop()
                section_lines.append("hooks = true")
            out.extend(section_lines)
            set_in_section = True
            continue
        out.append(line)
        i += 1
    if not found_section:
        joined = "\n".join(out).rstrip()
        suffix = "\n\n[features]\nhooks = true\n"
        return (joined + suffix) if joined else suffix.lstrip()
    return "\n".join(out).rstrip() + "\n"

new_text = ensure_features_hooks(new_text)
# Canonicalize the managed MCP section at the end. On an empty target the old
# merge emitted MCP then features, while the second merge emitted features then
# MCP, causing one no-op upgrade to change bytes and invalidate the code index.
without_managed = strip_section(new_text, "[mcp_servers.code-brain]").rstrip()
new_text = without_managed + "\n\n" + block if without_managed else block

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(new_text, encoding="utf-8")
PY
}

merge_claude_settings() {
  local dst="$TARGET_ROOT/.claude/settings.json"
  py - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
managed = {
    "PreToolUse": [
        {"matcher": "Bash",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PreToolUse"}]}
    ],
    "PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostToolUse"}]}
    ],
    "SessionStart": [
        {"matcher": "startup|resume|clear|compact",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SessionStart"}]}
    ],
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook UserPromptSubmit"}]}
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook Stop"}]}
    ],
    "SubagentStop": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SubagentStop"}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PreCompact"}]}
    ],
    "SessionEnd": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SessionEnd"}]}
    ],
    "Notification": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook Notification"}]}
    ],
    "PostCompact": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostCompact"}]}
    ],
    "CwdChanged": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook CwdChanged"}]}
    ],
    "ConfigChange": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook ConfigChange"}]}
    ],
    "PermissionDenied": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PermissionDenied"}]}
    ],
    "InstructionsLoaded": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook InstructionsLoaded"}]}
    ],
    "SubagentStart": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SubagentStart"}]}
    ],
    "TaskCreated": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook TaskCreated"}]}
    ],
    "TaskCompleted": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook TaskCompleted"}]}
    ],
    "FileChanged": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook FileChanged"}]}
    ],
    "PostToolUseFailure": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostToolUseFailure"}]}
    ],
}
# Windows parity: give every Claude hook a commandWindows that runs the .ps1 shim via
# powershell (Claude Code sets CLAUDE_PROJECT_DIR on Windows too; fall back to cwd). The
# Unix `command` stays the default; hosts pick commandWindows on Windows. Derived from
# each command's event (last token) so the 19-event dict above stays the single source.
def _claude_cmd_win(unix_cmd):
    event = unix_cmd.rsplit(" ", 1)[-1]
    return (
        'powershell -NoProfile -Command "$ROOT = $env:CLAUDE_PROJECT_DIR; '
        'if (-not $ROOT) { $ROOT = (Get-Location).Path }; '
        '& \\"$ROOT/.ai/bin/ai-hook.ps1\\" ' + event + '"'
    )
for _entries in managed.values():
    for _entry in _entries:
        for _handler in _entry.get("hooks", []):
            if isinstance(_handler, dict) and "command" in _handler and "commandWindows" not in _handler:
                _handler["commandWindows"] = _claude_cmd_win(_handler["command"])
if dst.exists():
    try:
        payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"install-into failed: existing {dst} is not valid JSON")
    if not isinstance(payload, dict):
        raise SystemExit(f"install-into failed: existing {dst} is not a JSON object")
else:
    payload = {}
hooks = payload.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"install-into failed: existing {dst}.hooks must be a JSON object")
def _has_code_brain_entry(entries):
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str) and "/.ai/bin/ai-hook" in hook["command"]:
                return True
    return False
def _strip_code_brain(entries):
    out = []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry); continue
        new_hooks = [h for h in entry.get("hooks", []) or [] if not (isinstance(h, dict) and isinstance(h.get("command"), str) and ".ai/bin/ai-hook" in h["command"])]
        if new_hooks:
            new_entry = dict(entry)
            new_entry["hooks"] = new_hooks
            out.append(new_entry)
        elif "hooks" not in entry:
            out.append(entry)
    return out
for hook_name, managed_entries in managed.items():
    existing = hooks.get(hook_name) if isinstance(hooks.get(hook_name), list) else []
    cleaned = _strip_code_brain(existing)
    hooks[hook_name] = cleaned + managed_entries
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

merge_codex_hooks_json() {
  local dst="$TARGET_ROOT/.codex/hooks.json"
  py - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])

def cmd(event):
    return 'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; "$ROOT/.ai/bin/ai-hook" ' + event

def cmd_win(event):
    return (
        'powershell -NoProfile -Command "$ROOT = (git rev-parse --show-toplevel 2>$null); '
        'if (-not $ROOT) { $ROOT = (Get-Location).Path }; '
        '& \\"$ROOT/.ai/bin/ai-hook.ps1\\" ' + event + '"'
    )

def H(event, matcher=None, msg=None):
    handler = {"type": "command", "command": cmd(event), "commandWindows": cmd_win(event)}
    if msg:
        handler["statusMessage"] = msg
    entry = {"hooks": [handler]}
    if matcher is not None:
        entry["matcher"] = matcher
    return [entry]

managed_codex_hooks = {
    "PreToolUse": H("PreToolUse", matcher="Bash|Shell|exec_command|functions.exec_command|run_command", msg="Checking Code Brain command routing"),
    "PostToolUse": H("PostToolUse", matcher="Bash|Shell|exec_command|functions.exec_command|apply_patch|Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep|run_command|replace_file_content|multi_replace_file_content|write_to_file|view_file|grep_search|list_dir", msg="Recording Code Brain tool result"),
    "SessionStart": H("SessionStart", matcher="startup|resume|clear|compact", msg="Loading Code Brain session context"),
    "UserPromptSubmit": H("UserPromptSubmit", msg="Loading Code Brain prompt context"),
    "Stop": H("Stop", msg="Recording Code Brain stop event"),
    "SubagentStart": H("SubagentStart", msg="Loading Code Brain subagent context"),
    "SubagentStop": H("SubagentStop", msg="Recording Code Brain subagent stop"),
    "PreCompact": H("PreCompact", msg="Saving Code Brain compact snapshot"),
    "PostCompact": H("PostCompact", msg="Recording Code Brain compact completion"),
    "PermissionRequest": H("PermissionRequest", matcher="Bash|Shell|exec_command|functions.exec_command|run_command|ask_permission", msg="Checking Code Brain approval policy"),
}
if dst.exists():
    try:
        existing_payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"install-into failed: existing {dst} is not valid JSON")
    if not isinstance(existing_payload, dict):
        raise SystemExit(f"install-into failed: existing {dst} is not a JSON object")
else:
    existing_payload = {}
existing_hooks = existing_payload.get("hooks", {})
if not isinstance(existing_hooks, dict):
    raise SystemExit(f"install-into failed: existing {dst}.hooks must be a JSON object")
# Codex's hooks parser accepts ONLY a top-level `hooks` key — any extra key (e.g. an
# annotation `_note`) makes it reject the whole file ("unknown field `_note`, expected
# `hooks`"). Rebuild with hooks only, dropping any stale top-level keys older installs wrote.
payload = {"hooks": existing_hooks}
hooks = payload["hooks"]

def _has_code_brain_command(hook_value):
    if isinstance(hook_value, dict):
        cmd = hook_value.get("command")
        if isinstance(cmd, str) and ".ai/bin/ai-hook" in cmd:
            return True
        for handler in hook_value.get("hooks", []) or []:
            if isinstance(handler, dict):
                cmd = handler.get("command")
                if isinstance(cmd, str) and ".ai/bin/ai-hook" in cmd:
                    return True
    return False

for name, managed_entries in managed_codex_hooks.items():
    existing = hooks.get(name)
    if isinstance(existing, list):
        kept = [e for e in existing if not _has_code_brain_command(e)]
    else:
        # Legacy: a single object value (older buggy install). Replace entirely.
        kept = []
    hooks[name] = kept + managed_entries
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

merge_antigravity_mcp_json() {
  local dst="$TARGET_ROOT/.agents/mcp_config.json"
  py - "$dst" "$SOURCE_ROOT" <<'PY'
import sys
from pathlib import Path

dst = Path(sys.argv[1])
source_root = Path(sys.argv[2])
sys.path.insert(0, str(source_root / ".ai" / "runtime" / "src"))
from ai_core.mcp_config import merge_antigravity_mcp_json

merge_antigravity_mcp_json(dst)
PY
}

merge_antigravity_hooks_json() {
  local dst="$TARGET_ROOT/.agents/hooks.json"
  py - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
# Antigravity 1.0.x hooks.json schema (verified against the agy hooks UI, which
# writes ~/.gemini/antigravity-cli/hooks.json): the file is a top-level map of
# {"<hook-name>": JSONHookSpec}. A JSONHookSpec has one field per supported
# lifecycle EVENT, and Antigravity supports exactly five:
#   PreToolUse, PostToolUse, PreInvocation, PostInvocation, Stop
# There is NO SessionStart / UserPromptSubmit — those Claude events are unknown to
# Antigravity (they parse as a named hook with zero handlers). Each event maps to
# null or a list of matcher-groups: [{"matcher": <regex>, "hooks": [{"type":
# "command", "command": <shell>, "timeout": <int>}]}]. The legacy Claude-shaped
# wrapper ({"_note":..., "hooks": {...}}) is unparseable by Antigravity
# ("cannot unmarshal string into jsonhook.JSONHookSpec") and is dropped here.
#
# Antigravity does not pass CLAUDE_PROJECT_DIR, so resolve the repo root via git.
# Memory injection for agy is delivered via the managed AGENTS.md block
# (ai_core.agents_md), NOT these hooks: Antigravity command-hook stdout cannot
# inject model context. These hooks cover the side effects that do work —
# command routing (PreToolUse), tool-result recording (PostToolUse), and
# session-end recording + AGENTS.md memory refresh (Stop).
def cmd(event: str) -> str:
    return (
        'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
        f'"$ROOT/.ai/bin/ai-hook" {event}'
    )

def matchers(event: str, timeout: int):
    return [{"matcher": "", "hooks": [{"type": "command", "command": cmd(event), "timeout": timeout}]}]

# NOTE: no PreToolUse hook for Antigravity. Its jsonhook contract is deny-by-default —
# unless the hook returns an approve schema agy recognizes, EVERY tool call is denied
# (verified live: empty stdout, "{}", and a Claude-style permissionDecision:allow were all
# treated as deny, hard-stalling the worker). Code Brain's PreToolUse therefore broke agy
# rather than protecting it. PostToolUse (redaction/recording) and Stop (memory refresh) work
# fine. Pre-execution risk for agy workers is covered by the loopd dispatch approval-gate.
code_brain_spec = {
    "PreToolUse": None,
    "PostToolUse": matchers("PostToolUse", 15),
    "PreInvocation": None,
    "PostInvocation": None,
    "Stop": matchers("Stop", 20),
}

if dst.exists():
    try:
        payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
else:
    payload = {}

# Preserve user-authored named hooks (dict values); drop our own entry and the
# legacy "_note"/"hooks" wrapper keys, then re-add the Code Brain entry.
cleaned = {
    name: spec
    for name, spec in payload.items()
    if name not in ("code-brain", "_note", "hooks") and isinstance(spec, dict)
}
cleaned["code-brain"] = code_brain_spec

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

ensure_persistent_scaffold() {
  mkdir -p \
    "$TARGET_ROOT/.ai/generated" \
    "$TARGET_ROOT/.ai/memory/audit" \
    "$TARGET_ROOT/.ai/memory/queue/.tmp" \
    "$TARGET_ROOT/.ai/memory/queue/processing" \
    "$TARGET_ROOT/.ai/memory/queue/dead"
  [[ -e "$TARGET_ROOT/.ai/memory/audit-index.jsonl" ]] || : >"$TARGET_ROOT/.ai/memory/audit-index.jsonl"
  [[ -e "$TARGET_ROOT/.ai/memory/queue/.tmp/.gitkeep" ]] || : >"$TARGET_ROOT/.ai/memory/queue/.tmp/.gitkeep"
  [[ -e "$TARGET_ROOT/.ai/memory/queue/processing/.gitkeep" ]] || : >"$TARGET_ROOT/.ai/memory/queue/processing/.gitkeep"
  [[ -e "$TARGET_ROOT/.ai/memory/queue/dead/.gitkeep" ]] || : >"$TARGET_ROOT/.ai/memory/queue/dead/.gitkeep"
}

prune_orphans() {
  # Remove CB-managed command/prompt/skill files that a PRIOR install recorded but the current
  # version no longer ships (e.g. retired cb-loop*/cb-pool prompts). Manifest-gated: only files
  # the previous install-manifest listed are eligible, so user-authored files (never in the
  # manifest) are never touched. Runs before the copy/manifest rewrite. Fail-soft.
  local manifest; manifest="$(manifest_path)"
  [[ -f "$manifest" ]] || return 0
  local newlist; newlist="$(mktemp)" || return 0
  managed_files >"$newlist" 2>/dev/null
  py - "$TARGET_ROOT" "$manifest" "$newlist" <<'PY'
import json, sys
from pathlib import Path
target = Path(sys.argv[1]); manifest = Path(sys.argv[2]); newlist = Path(sys.argv[3])
new_managed = {ln.strip() for ln in newlist.read_text(encoding="utf-8").splitlines() if ln.strip()}
# Safety: if the source list came back empty (git/listing failure), prune NOTHING — never
# let an empty "currently shipped" set delete every managed file.
if not new_managed:
    raise SystemExit(0)
try:
    prev = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
except Exception:
    prev = []
PRUNE_DIRS = (".claude/commands", ".codex/prompts", ".agents/skills")
# Known legacy command basenames CB used to ship and has retired (they don't carry the cb- prefix,
# so the namespace rule below can't catch them). Manifest rewrites long ago dropped these, so
# manifest-gating alone misses them — list them explicitly.
RETIRED = {"ai-runbook.md", "automation-hook-slow.md", "git-runbook.md"}


def is_cb_orphan(rel: str, name: str) -> bool:
    """A CB-managed file the current version no longer ships. Conservative discriminators only:
    (1) recorded in the PREVIOUS manifest, or (2) the cb- namespace (CB's reserved prefix), or
    (3) an explicitly retired legacy basename. User files (no cb- prefix, not in manifest/list)
    are never matched."""
    if rel in new_managed:
        return False
    return rel in prev_set or name.startswith("cb-") or name in RETIRED


prev_set = {r for r in prev if isinstance(r, str)}
candidates: list[str] = []
# prev-manifest entries under the managed dirs (recently retired)
for rel in prev_set:
    if any(rel.startswith(d + "/") for d in PRUNE_DIRS):
        candidates.append(rel)
# plus a live scan of the target dirs (catches orphans already dropped from the manifest)
for d in PRUNE_DIRS:
    base = target / d
    if base.is_dir():
        for p in base.rglob("*"):
            if p.is_file():
                candidates.append(p.relative_to(target).as_posix())
removed = []
for rel in sorted(set(candidates)):
    name = rel.rsplit("/", 1)[-1]
    if not is_cb_orphan(rel, name):
        continue
    p = target / rel
    if p.is_file():
        try:
            p.unlink()
            removed.append(rel)
        except OSError:
            pass
if removed:
    print("pruned " + str(len(removed)) + " orphan command(s): " + ", ".join(sorted(removed)), file=sys.stderr)
PY
  rm -f "$newlist"
}

install_or_upgrade() {
  prune_orphans
  copy_managed_files
  seed_user_owned_files
  merge_mcp_json
  merge_codex_config
  merge_claude_settings
  merge_codex_hooks_json
  merge_antigravity_mcp_json
  merge_antigravity_hooks_json
  configure_project
  ensure_persistent_scaffold
  write_bootstrap
  chmod +x "$TARGET_ROOT/.ai/bin/ai" "$TARGET_ROOT/.ai/bin/ai-hook" "$TARGET_ROOT/.ai/bin/ai-mcp"
  chmod +x "$TARGET_ROOT/.githooks/post-merge" "$TARGET_ROOT/.githooks/post-checkout"
  chmod +x "$TARGET_ROOT/scripts/env-check.sh" "$TARGET_ROOT/scripts/preflight.sh"
  write_install_manifest

  case "${AI_INSTALL_DEFER_RUNTIME:-0}" in
    1|true|TRUE|yes|YES|on|ON)
      echo "install-into: runtime activation deferred; run bootstrap-code-brain.sh and session start in the target" >&2
      restore_managed_owner_if_root
      return 0
      ;;
  esac

  cd "$TARGET_ROOT"
  # When install-into runs as root (typical on shared servers like the Phalanx
  # llm host where cc is the operator), running bootstrap/uv-sync/session as
  # root would create root-owned files inside .venv and .ai/cache that the
  # operator user cannot use. Resolve the intended target user and drop privs
  # for the runtime-touching steps so every artifact lands with the correct
  # ownership the first time. Falls through to direct execution when not root
  # or when no safe fallback user can be determined.
  local _run_as=""
  if [[ "$(id -u)" == "0" ]]; then
    local _ai_uid
    _ai_uid="$(stat -c '%u' "$TARGET_ROOT/.ai" 2>/dev/null || stat -f '%u' "$TARGET_ROOT/.ai" 2>/dev/null || echo "")"
    local _run_user=""
    if [[ -n "$_ai_uid" ]] && getent passwd "$_ai_uid" >/dev/null 2>&1; then
      _run_user="$(getent passwd "$_ai_uid" | cut -d: -f1)"
    elif [[ -n "${AI_INSTALL_OWNER:-}" ]]; then
      _run_user="${AI_INSTALL_OWNER%%:*}"
    elif [[ -n "${SUDO_USER:-}" ]] && getent passwd "$SUDO_USER" >/dev/null 2>&1; then
      _run_user="$SUDO_USER"
    fi
    if [[ -n "$_run_user" ]] && id -u "$_run_user" >/dev/null 2>&1; then
      echo "install-into: root detected; running bootstrap/session as $_run_user (override with AI_INSTALL_OWNER)" >&2
      _run_as="sudo -u $_run_user -H"
    else
      echo "install-into: root detected but no safe target user found; running bootstrap as root (venv may need manual chown later)" >&2
    fi
  fi
  # Venv self-heal: when the existing .venv/bin/python symlink points at a
  # missing or unreadable interpreter (typical after a host's uv cache moved,
  # the original installer's $HOME was wiped, or the venv was created by a
  # different user whose Python directory the target user cannot read),
  # bootstrap will reuse the broken venv and every hook ends in
  # "command not found". Detect that up front and tear down the venv so the
  # next uv sync inside bootstrap rebuilds with an interpreter the target
  # user can actually read. Only the broken-symlink case triggers removal.
  local _venv_py="$TARGET_ROOT/.ai/runtime/.venv/bin/python"
  if [[ -L "$_venv_py" ]]; then
    local _venv_ok=1
    if [[ -n "$_run_as" ]]; then
      $_run_as test -x "$_venv_py" || _venv_ok=0
    else
      [[ -x "$_venv_py" ]] || _venv_ok=0
    fi
    if [[ "$_venv_ok" == "0" ]]; then
      echo "install-into: venv interpreter unreachable (broken symlink target); recreating .venv" >&2
      rm -rf "$TARGET_ROOT/.ai/runtime/.venv"
    fi
  fi
  # session start below runs the complete doctor checks after rebuilding the
  # code and audit indexes, so avoid separate CLI startup and doctor scans.
  $_run_as env AI_BOOTSTRAP_LOW_MEMORY=1 ./bootstrap-code-brain.sh --skip-doctor --skip-render --low-memory
  local -a _session_args=(session start --agent operator --rebuild auto --repair-audit-index --render-manifest)
  case "${AI_INSTALL_STRICT:-0}" in
    1|true|TRUE|yes|YES|on|ON)
      _session_args+=(--strict)
      echo "install-into: strict first-session health enabled" >&2
      ;;
  esac
  _session_args+=(--json)
  $_run_as .ai/bin/ai "${_session_args[@]}"
  restore_managed_owner_if_root
}

uninstall() {
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "$manifest" ]]; then
    echo "install-into failed: install manifest not found: $manifest" >&2
    exit 4
  fi
  if [[ "$(git -C "$TARGET_ROOT" config --get core.hooksPath || true)" == ".githooks" ]]; then
    git -C "$TARGET_ROOT" config --unset core.hooksPath || true
  fi
  py - "$TARGET_ROOT" "$manifest" <<'PY'
import json
import re
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = Path(sys.argv[2])
payload = json.loads(manifest.read_text(encoding="utf-8"))
for rel in sorted(payload.get("files", []), key=lambda item: item.count("/"), reverse=True):
    path = root / rel
    if path.is_file() or path.is_symlink():
        path.unlink()
mcp = root / ".mcp.json"
if mcp.exists():
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and "code-brain" in servers:
            servers.pop("code-brain", None)
            if servers:
                mcp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            else:
                mcp.unlink()
codex_cfg = root / ".codex" / "config.toml"
if codex_cfg.exists():
    text = codex_cfg.read_text(encoding="utf-8")
    def _strip_section(t: str, header: str) -> str:
        lines = t.splitlines()
        out: list[str] = []
        i = 0
        while i < len(lines):
            if lines[i].strip() == header:
                i += 1
                while i < len(lines):
                    nxt = lines[i].lstrip()
                    if nxt.startswith("[") and not nxt.startswith("[]"):
                        break
                    i += 1
                while out and out[-1].strip() == "":
                    out.pop()
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)
    cleaned = _strip_section(text, "[mcp_servers.code-brain]")
    cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip() != "[]").strip()
    if cleaned:
        codex_cfg.write_text(cleaned + "\n", encoding="utf-8")
    else:
        codex_cfg.unlink()
        codex_dir = codex_cfg.parent
        try:
            codex_dir.rmdir()
        except OSError:
            pass
claude_settings = root / ".claude" / "settings.json"
if claude_settings.exists():
    try:
        settings = json.loads(claude_settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        settings = None
    if isinstance(settings, dict):
        hooks_block = settings.get("hooks")
        if isinstance(hooks_block, dict):
            cleaned_hooks = {}
            for hook_name, entries in list(hooks_block.items()):
                if not isinstance(entries, list):
                    cleaned_hooks[hook_name] = entries
                    continue
                kept = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        kept.append(entry); continue
                    new_hooks = [h for h in entry.get("hooks", []) or [] if not (isinstance(h, dict) and isinstance(h.get("command"), str) and ".ai/bin/ai-hook" in h["command"])]
                    if new_hooks:
                        nh = dict(entry); nh["hooks"] = new_hooks
                        kept.append(nh)
                    elif "hooks" not in entry:
                        kept.append(entry)
                if kept:
                    cleaned_hooks[hook_name] = kept
            settings["hooks"] = cleaned_hooks
            if not settings["hooks"]:
                settings.pop("hooks")
        if settings:
            claude_settings.write_text(json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            claude_settings.unlink()
agent_mcp = root / ".agents" / "mcp_config.json"
if agent_mcp.exists():
    try:
        data = json.loads(agent_mcp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and "code-brain" in servers:
            servers.pop("code-brain", None)
            if servers:
                agent_mcp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            else:
                agent_mcp.unlink()
agent_hooks = root / ".agents" / "hooks.json"
if agent_hooks.exists():
    try:
        cfg = json.loads(agent_hooks.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        cfg = None
    if isinstance(cfg, dict):
        hb = cfg.get("hooks")
        if isinstance(hb, dict):
            for name in list(hb.keys()):
                entries = hb.get(name)
                if isinstance(entries, list):
                    kept = [e for e in entries if not any(
                        isinstance(h, dict) and isinstance(h.get("command"), str) and ".ai/bin/ai-hook" in h["command"]
                        for h in (e.get("hooks") or []) if isinstance(e, dict)
                    )]
                    if kept:
                        hb[name] = kept
                    else:
                        hb.pop(name, None)
            if not hb:
                cfg.pop("hooks")
        keys_left = [k for k in cfg.keys() if k != "_note"]
        if keys_left:
            agent_hooks.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            agent_hooks.unlink()
codex_hooks = root / ".codex" / "hooks.json"
if codex_hooks.exists():
    try:
        cfg = json.loads(codex_hooks.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        cfg = None
    if isinstance(cfg, dict):
        hb = cfg.get("hooks")
        if isinstance(hb, dict):
            for name in list(hb.keys()):
                entries = hb.get(name)
                if isinstance(entries, list):
                    kept = [e for e in entries if not (isinstance(e, dict) and isinstance(e.get("command"), str) and "/.ai/bin/ai-hook" in e["command"])]
                    if kept:
                        hb[name] = kept
                    else:
                        hb.pop(name, None)
                elif isinstance(entries, dict) and isinstance(entries.get("command"), str) and "/.ai/bin/ai-hook" in entries["command"]:
                    hb.pop(name, None)
            if not hb:
                cfg.pop("hooks")
        keys_left = [k for k in cfg.keys() if k != "_note"]
        if keys_left:
            codex_hooks.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            codex_hooks.unlink()
for rel in (".ai", ".githooks"):
    path = root / rel
    if path.exists():
        shutil.rmtree(path)
for rel in (".claude/commands", ".codex/prompts", ".agents/skills", ".agents"):
    path = root / rel
    if path.exists() and path.is_dir() and not any(path.iterdir()):
        path.rmdir()
        try:
            path.parent.rmdir()
        except OSError:
            pass
for rel in ("scripts",):
    path = root / rel
    try:
        path.rmdir()
    except OSError:
        pass
PY
}

case "$ACTION" in
  install)
    if [[ -f "$(manifest_path)" ]]; then
      echo "install-into failed: Code Brain already installed; use upgrade" >&2
      exit 5
    fi
    install_or_upgrade
    echo "code-brain installed: $TARGET_ROOT"
    echo "next: cd '$TARGET_ROOT' && .ai/bin/ai session start --agent codex --json"
    ;;
  upgrade)
    if [[ ! -f "$(manifest_path)" ]] && ! legacy_code_brain_install; then
      echo "install-into failed: Code Brain is not installed; use install" >&2
      exit 4
    fi
    install_or_upgrade
    echo "code-brain upgraded: $TARGET_ROOT"
    ;;
  uninstall)
    uninstall
    echo "code-brain uninstalled: $TARGET_ROOT"
    ;;
esac
