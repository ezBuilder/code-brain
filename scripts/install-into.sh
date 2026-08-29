#!/usr/bin/env bash
set -eEuo pipefail
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
Exact Code Brain-managed Codex hooks are trusted after install/upgrade by default.
Set AI_CODEX_HOOK_AUTO_TRUST=0 to leave them review-gated.
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
    } | grep -vxE "\.ai/secret_scan_allowlist\.txt|\.ai/generated/install-manifest\.json|\.ai/eval(/.*)?" \
      | awk '
          $0 ~ /^\.ai\/outputs\// && $0 != ".ai/outputs/.gitkeep" { next }
          ($0 ~ /^\.ai\/memory\// || $0 ~ /^\.ai\/runtime\/state\//) && $0 !~ /\.gitkeep$/ { next }
          { print }
        ' \
      | while IFS= read -r rel; do
        [[ -f "$rel" ]] && printf '%s\n' "$rel"
      done
  ) || true
  # ^ never propagate the SOURCE repo's private runtime memory/state DATA, durable output
  #   artifacts, or user-owned .ai/eval scratch
  #   (audit chain, decisions,
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
    if [[ ! -f "$dst" ]] || ! cmp -s "$SOURCE_ROOT/.ai/AGENTS.md" "$dst"; then
      cp "$SOURCE_ROOT/.ai/AGENTS.md" "$dst"
    fi
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
  if [[ -x "$TARGET_ROOT/.ai/bin/ai" || -f "$TARGET_ROOT/.ai/AGENTS.md" ]]; then
    return 0
  fi
  # Early pre-manifest builds could leave the runtime and host wiring without
  # either modern marker. Require three independent Code Brain signatures
  # before allowing `upgrade` to adopt that partial namespace; a normal
  # unrelated .ai directory must still use the collision-safe fresh install.
  [[ -f "$TARGET_ROOT/.ai/runtime/pyproject.toml" ]] \
    && grep -qxF 'name = "code-brain-runtime"' "$TARGET_ROOT/.ai/runtime/pyproject.toml" \
    && [[ -f "$TARGET_ROOT/.codex/hooks.json" ]] \
    && grep -Fq '.ai/bin/ai-hook' "$TARGET_ROOT/.codex/hooks.json" \
    && [[ -f "$TARGET_ROOT/.codex/config.toml" ]] \
    && grep -Fq '[mcp_servers.code-brain]' "$TARGET_ROOT/.codex/config.toml"
}

copy_managed_files() {
  # Materialize the complete managed set in one Python process. The previous
  # Bash loop spawned dirname/mkdir/cp for every file (300 files in this repo),
  # which dominated fresh-install time on macOS. This preserves the existing
  # manifest/legacy/marker overwrite rules while adding symlink confinement.
  py -c '
import hashlib
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

runtime_marker = target_root / ".ai" / "runtime" / "pyproject.toml"
hooks_marker = target_root / ".codex" / "hooks.json"
config_marker = target_root / ".codex" / "config.toml"
partial_legacy_install = False
try:
    partial_legacy_install = (
        runtime_marker.is_file()
        and b"name = \"code-brain-runtime\"" in runtime_marker.read_bytes().splitlines()
        and hooks_marker.is_file()
        and b".ai/bin/ai-hook" in hooks_marker.read_bytes()
        and config_marker.is_file()
        and b"[mcp_servers.code-brain]" in config_marker.read_bytes()
    )
except OSError:
    partial_legacy_install = False
legacy_install = (
    os.access(target_root / ".ai" / "bin" / "ai", os.X_OK)
    or (target_root / ".ai" / "AGENTS.md").is_file()
    or partial_legacy_install
)
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


def desired_bytes(rel: str, src: Path) -> bytes:
    data = src.read_bytes()
    if rel != ".ai/config.yaml":
        return data
    text = data.decode("utf-8")
    lines = []
    replaced = False
    for line in text.splitlines():
        if line.startswith("project_name:"):
            lines.append(f"project_name: {target_root.name}")
            replaced = True
        elif "index_vendored_runtime" in line:
            continue
        else:
            lines.append(line)
    if not replaced:
        lines.insert(1, f"project_name: {target_root.name}")
    return ("\n".join(lines) + "\n").encode("utf-8")


pending: list[tuple[str, Path, Path, bytes]] = []
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
    desired = desired_bytes(rel, src)
    if dst.exists():
        if not dst.is_file():
            print(f"install-into failed: refusing to overwrite non-file target {rel}", file=sys.stderr)
            raise SystemExit(3)
        identical = dst.read_bytes() == desired
        if identical:
            continue
        if not identical and not is_managed_existing(rel, dst):
            print(
                f"install-into failed: refusing to overwrite existing untracked target file {rel}",
                file=sys.stderr,
            )
            raise SystemExit(3)
    pending.append((rel, src, dst, desired))

# Do not mutate until every source, destination, ownership, and confinement check passed.
# The transaction below remains the late-I/O/runtime rollback rail; this pass prevents a
# predictable conflict near the end of the list from ever creating an intermediate splice.
for rel, src, dst, desired in pending:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if rel == ".ai/config.yaml":
        dst.write_bytes(desired)
        shutil.copymode(src, dst, follow_symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=True)
' "$SOURCE_ROOT" "$TARGET_ROOT" "$ACTION" "$(manifest_path)" < <(managed_files)
}

write_bootstrap() {
  local src="$SOURCE_ROOT/bootstrap-code-brain.sh"
  if [[ ! -f "$src" ]]; then
    echo "install-into failed: missing source file bootstrap-code-brain.sh" >&2
    exit 2
  fi
  if [[ ! -f "$TARGET_ROOT/bootstrap-code-brain.sh" ]] || ! cmp -s "$src" "$TARGET_ROOT/bootstrap-code-brain.sh"; then
    cp "$src" "$TARGET_ROOT/bootstrap-code-brain.sh"
  fi
  [[ -x "$TARGET_ROOT/bootstrap-code-brain.sh" ]] || chmod +x "$TARGET_ROOT/bootstrap-code-brain.sh"
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
    # Public upgrades must follow the stable branch, not whichever development
    # branch happened to build the local installer. Branch-specific channels
    # remain available through the explicit CODE_BRAIN_REF contract.
    source_ref = "main"
payload = {
    "schema_version": 2,
    "tool": "code-brain",
    "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "project": root.name,
    "files": sorted(set(files)),
    "merged_config_files": [".mcp.json", ".codex/config.toml", ".claude/settings.json", ".codex/hooks.json", ".agents/mcp_config.json", ".agents/hooks.json", ".kiro/hooks/code-brain.json"],
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
    "$TARGET_ROOT/.kiro/hooks/code-brain.json" \
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
  py - "$TARGET_ROOT" "$SOURCE_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
source_root = Path(sys.argv[2])
self_install = root.resolve() == source_root.resolve()
config = root / ".ai" / "config.yaml"
text = config.read_text(encoding="utf-8")
lines = []
replaced = False
for line in text.splitlines():
    if line.startswith("project_name:"):
        lines.append(f"project_name: {root.name}")
        replaced = True
    elif "index_vendored_runtime" in line and not self_install:
        # Source-repo-only flag: consumer installs must not index the vendored
        # .ai/runtime payload, so the opt-in never propagates to targets.
        continue
    else:
        lines.append(line)
if not replaced:
    lines.insert(1, f"project_name: {root.name}")
rendered = "\n".join(lines) + "\n"
if config.read_text(encoding="utf-8") != rendered:
    config.write_text(rendered, encoding="utf-8")
PY
}

merge_mcp_json() {
  local dst="$TARGET_ROOT/.mcp.json"
  py - "$dst" <<'PY'
import json
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
target_windows = os.environ.get("AI_INSTALL_TARGET_WINDOWS", "").lower() in {"1", "true", "yes", "on"}
# Compact tools on by default: tools/list ships only the ~15 hot core tools; the rest load on
# demand via tool_search. Big per-session schema-token cut, no capability loss. (AI_MCP_COMPACT_TOOLS)
desired = {
    "command": "powershell" if target_windows else ".ai/bin/ai-mcp",
    "args": ["-NoProfile", "-File", ".ai/bin/ai-mcp.ps1"] if target_windows else [],
    "env": {"AI_CODE_BRAIN_PROFILE": "usage", "AI_MCP_COMPACT_TOOLS": "1"},
}
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
rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
if not dst.exists() or dst.read_text(encoding="utf-8") != rendered:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
PY
}

merge_codex_config() {
  local dst="$TARGET_ROOT/.codex/config.toml"
  py - "$dst" <<'PY'
import os
import re
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
target_windows = os.environ.get("AI_INSTALL_TARGET_WINDOWS", "").lower() in {"1", "true", "yes", "on"}
command = "powershell" if target_windows else ".ai/bin/ai-mcp"
args = '["-NoProfile", "-File", ".ai/bin/ai-mcp.ps1"]' if target_windows else "[]"
block = (
    "[mcp_servers.code-brain]\n"
    f"command = \"{command}\"\n"
    f"args = {args}\n"
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

if not dst.exists() or dst.read_text(encoding="utf-8") != new_text:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new_text, encoding="utf-8")
PY
}

# Detects the local Claude Code CLI version as "MAJOR.MINOR.PATCH" (best-effort).
# Used only to gate Claude hooks.json event keys that a too-old Claude Code
# install may not recognize (see merge_claude_settings). Fails closed: prints
# nothing and returns non-zero when the version cannot be determined, and
# callers must treat that as "unknown" and NOT enable the gated event — Claude
# Code drops malformed/unknown hook entries rather than tolerating them the
# way Codex's hooks.json parser does, so guessing support is the wrong default.
detect_claude_cli_version() {
  local override="${AI_CLAUDE_CLI_VERSION_OVERRIDE:-}"
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi
  command -v claude >/dev/null 2>&1 || return 1
  local raw
  raw="$(claude --version 2>/dev/null | head -1)" || return 1
  # Observed format: "2.1.220 (Claude Code)".
  [[ "$raw" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

# True (rc 0) when the locally detected Claude Code version is >= $1. Reuses
# compare_versions (defined above for the Codex CLI gate); fails closed when
# the version cannot be detected.
claude_cli_version_at_least() {
  local required="$1" detected
  detected="$(detect_claude_cli_version)" || return 1
  [[ "$(compare_versions "$detected" "$required")" != "-1" ]]
}

merge_claude_settings() {
  local dst="$TARGET_ROOT/.claude/settings.json"
  # StopFailure, TeammateIdle, TaskCreated, FileChanged and CwdChanged are real,
  # officially documented Claude Code hook events, but they are newer additions
  # to the event vocabulary than the rest of this file's managed events
  # (StopFailure: v2.1.78, 2026-03-17; TeammateIdle: v2.1.33, 2026-02-06;
  # CwdChanged/FileChanged: v2.1.83, 2026-03-24; TaskCreated: v2.1.84,
  # 2026-03-26 — verified 2026-08-29 against the official changelog and hooks
  # reference). An unknown/unsupported hooks.json key can be dropped or
  # rejected outright by an older Claude Code, unlike Codex's tolerant
  # hooks.json parser, so all five are version-gated rather than shipped
  # unconditionally like the rest of this file's existing (older,
  # already-supported) managed events.
  local stopfailure_enabled="0" teammateidle_enabled="0" taskcreated_enabled="0" filechanged_enabled="0" cwdchanged_enabled="0"
  if claude_cli_version_at_least "2.1.78"; then
    stopfailure_enabled="1"
  fi
  if claude_cli_version_at_least "2.1.33"; then
    teammateidle_enabled="1"
  fi
  if claude_cli_version_at_least "2.1.83"; then
    filechanged_enabled="1"
    cwdchanged_enabled="1"
  fi
  if claude_cli_version_at_least "2.1.84"; then
    taskcreated_enabled="1"
  fi
  py - "$dst" "$stopfailure_enabled" "$teammateidle_enabled" "$taskcreated_enabled" "$filechanged_enabled" "$cwdchanged_enabled" <<'PY'
import json
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
stopfailure_enabled = sys.argv[2] == "1"
teammateidle_enabled = sys.argv[3] == "1"
taskcreated_enabled = sys.argv[4] == "1"
filechanged_enabled = sys.argv[5] == "1"
cwdchanged_enabled = sys.argv[6] == "1"
managed = {
    "PreToolUse": [
        # Widened from "Bash"-only to also cover Claude's file-write tools (Edit/Write/
        # MultiEdit/NotebookEdit — official matcher tool-name list, verified 2026-08-29).
        # Code Brain's PreToolUse handler routes command-shaped input (tool_input.command/
        # CommandLine) through precall/commit-secret rules, AND separately runs
        # stream_guard.evaluate_hook_payload() against the full tool_input text for EVERY
        # PreToolUse call regardless of tool shape (.ai/runtime/src/ai_core/{hooks,
        # stream_guard}.py, verified 2026-08-29) — a secret/dangerous-pattern match there sets
        # decision=block independent of the command-only checks. So this widening does add a
        # real, new block surface: a file-write tool call (Edit/Write/MultiEdit/NotebookEdit)
        # whose file_path/content trips stream_guard can now be denied, the same way a
        # matching Bash command already could be. That is the intended protection, not a side
        # effect to explain away — do not describe this matcher as "observation only" or claim
        # it "cannot introduce a new way to block".
        {"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PreToolUse", "timeout": 5}]}
    ],
    "PostToolUse": [
        # Bash is required here (not just Edit/Write/...): completion_guard observes
        # test/lint/build command results via PostToolUse, and those run through the Bash
        # tool, not a file-write tool. Omitting Bash silently blinds that observation path.
        {"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostToolUse", "timeout": 2}]}
    ],
    "SessionStart": [
        # "fork" was added as a SessionStart source alongside startup/resume/clear/compact
        # (Claude Code hook events reference, verified 2026-08-29). Listing it here is a
        # config-only, fail-safe addition: a Claude Code build that doesn't emit "fork" simply
        # never matches this extra alternative, so nothing regresses on older installs.
        {"matcher": "startup|resume|clear|compact|fork",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SessionStart", "timeout": 2}]}
    ],
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook UserPromptSubmit", "timeout": 5}]}
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook Stop", "timeout": 5}]}
    ],
    "SubagentStop": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SubagentStop", "timeout": 2}]}
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PreCompact", "timeout": 2}]}
    ],
    "SessionEnd": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SessionEnd", "timeout": 2}]}
    ],
    "Notification": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook Notification", "timeout": 2}]}
    ],
    "PostCompact": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostCompact", "timeout": 2}]}
    ],
    "ConfigChange": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook ConfigChange", "timeout": 2}]}
    ],
    "PermissionDenied": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PermissionDenied", "timeout": 2}]}
    ],
    "InstructionsLoaded": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook InstructionsLoaded", "timeout": 2}]}
    ],
    "SubagentStart": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SubagentStart", "timeout": 2}]}
    ],
    # TaskCompleted does NOT support matchers (official hooks reference: "TaskCompleted hooks
    # do not support matchers and fire on every occurrence") — a matcher key here would be
    # silently ignored at best; deliberately omitted rather than added speculatively.
    "TaskCompleted": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook TaskCompleted", "timeout": 5}]}
    ],
    "PostToolUseFailure": [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook PostToolUseFailure", "timeout": 2}]}
    ],
}
# StopFailure/TeammateIdle: neither supports matchers (official hooks reference), and both
# are added only when the locally detected Claude Code version supports them (see the version
# gate above this heredoc).
if stopfailure_enabled:
    managed["StopFailure"] = [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook StopFailure", "timeout": 2}]}
    ]
if teammateidle_enabled:
    managed["TeammateIdle"] = [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook TeammateIdle", "timeout": 5}]}
    ]
# TaskCreated does NOT support matchers (official hooks reference), and — unlike
# TaskCompleted — a hook on it CAN block: exit code 2 rolls back the task's creation
# (verified 2026-08-29). Code Brain's handler only records/observes; it never returns
# a block decision for TaskCreated, so this addition is side-effect free either way.
if taskcreated_enabled:
    managed["TaskCreated"] = [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook TaskCreated", "timeout": 2}]}
    ]
# FileChanged's matcher is NOT a regex/glob: Claude Code splits it on "|" into literal
# filenames to watch (basename match), and only starts the watcher when some FileChanged
# group names at least one file (official hooks reference + hooks guide, verified
# 2026-08-29). Watch exactly the repo-root files whose edits should be recorded/should
# refresh injected context. `watchPaths` (SessionStart/CwdChanged hook output) could
# extend this list dynamically, but this installer does not assume that output shape is
# populated by the current runtime — the static matcher alone is enough to make this
# hook fire instead of a no-matcher entry, which would never watch anything.
if filechanged_enabled:
    managed["FileChanged"] = [
        {"matcher": "AGENTS.md|CLAUDE.md|.ai/config.yaml",
         "hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook FileChanged", "timeout": 2}]}
    ]
if cwdchanged_enabled:
    managed["CwdChanged"] = [
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook CwdChanged", "timeout": 2}]}
    ]
# Windows parity: give every Claude hook a commandWindows that runs the .ps1 shim via
# powershell (Claude Code sets CLAUDE_PROJECT_DIR on Windows too; fall back to cwd). The
# Unix `command` stays the default; hosts pick commandWindows on Windows. Derived from
# each command's event (last token) so the managed dict above stays the single source.
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
# A previous install/upgrade may have written a version-gated event
# (StopFailure/TeammateIdle) that is no longer enabled on this run (e.g. Claude
# Code was downgraded, or the override env var changed). Strip our own
# commands from any gated event we are not currently managing, but never
# touch a foreign/user entry for that event name.
for _gated_name, _enabled in (
    ("StopFailure", stopfailure_enabled),
    ("TeammateIdle", teammateidle_enabled),
    ("TaskCreated", taskcreated_enabled),
    ("FileChanged", filechanged_enabled),
    ("CwdChanged", cwdchanged_enabled),
):
    if _enabled or _gated_name not in hooks:
        continue
    _existing = hooks.get(_gated_name)
    if isinstance(_existing, list):
        _kept = _strip_code_brain(_existing)
        if _kept:
            hooks[_gated_name] = _kept
        else:
            del hooks[_gated_name]
# Env keys the runtime needs in order for the Stop-hook guards to do anything at all.
# Before this, the source kit's .claude/settings.json carried env.AI_LOOP_CONTINUATION=1 but
# the installer only ever merged `hooks`, so consumer settings ended up with the Stop hook
# registered and env absent — the premature-stop guard was dead in every installed project.
# Additive only: an existing user value is never overwritten.
_managed_env = {"AI_LOOP_CONTINUATION": "1"}
_env = payload.setdefault("env", {})
if isinstance(_env, dict):
    for _k, _v in _managed_env.items():
        _env.setdefault(_k, _v)
else:
    raise SystemExit(f"install-into failed: existing {dst}.env must be a JSON object")
dst.parent.mkdir(parents=True, exist_ok=True)
_rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
# Byte-identical renders must not be written: agent settings files are commonly protected by
# the host sandbox/MDM, and a no-op write would fail an otherwise clean upgrade.
if not dst.exists() or dst.read_text(encoding="utf-8") != _rendered:
    dst.write_text(_rendered, encoding="utf-8")
PY
}

# Detects the local Codex CLI version as "MAJOR.MINOR.PATCH" (best-effort).
# Used only to gate hook events whose support varies by installed Codex CLI
# version (see merge_codex_hooks_json). Prints nothing and returns non-zero
# when the version cannot be determined — callers must treat that as "unknown"
# and fail closed (i.e. do not enable version-gated events).
detect_codex_cli_version() {
  local override="${AI_CODEX_CLI_VERSION_OVERRIDE:-}"
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi
  command -v codex >/dev/null 2>&1 || return 1
  local raw
  raw="$(codex --version 2>/dev/null | head -1)" || return 1
  # Observed formats: "codex-cli 0.147.0", "codex 0.147.0".
  [[ "$raw" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

# Compares two "MAJOR.MINOR.PATCH" version strings. Prints -1/0/1 to stdout
# (a < b / a == b / a > b). Missing/non-numeric components are treated as 0,
# matching semver comparison for release versions (no pre-release suffixes
# are expected from `codex --version`).
compare_versions() {
  local a="$1" b="$2"
  local -a av bv
  IFS='.' read -r -a av <<<"$a"
  IFS='.' read -r -a bv <<<"$b"
  local i
  for i in 0 1 2; do
    local ai="${av[$i]:-0}" bi="${bv[$i]:-0}"
    ai="${ai//[!0-9]/}"; bi="${bi//[!0-9]/}"
    ai="${ai:-0}"; bi="${bi:-0}"
    if ((10#$ai > 10#$bi)); then printf '1\n'; return 0; fi
    if ((10#$ai < 10#$bi)); then printf '%s\n' "-1"; return 0; fi
  done
  printf '0\n'
}

# True (rc 0) when the locally detected Codex CLI version is >= $1. Fails
# closed: if the version cannot be detected at all, returns non-zero so
# callers gate the feature OFF rather than assume support.
codex_cli_version_at_least() {
  local required="$1" detected
  detected="$(detect_codex_cli_version)" || return 1
  [[ "$(compare_versions "$detected" "$required")" != "-1" ]]
}

merge_codex_hooks_json() {
  local dst="$TARGET_ROOT/.codex/hooks.json"
  # SessionEnd shipped as a stable Codex hooks.json event ahead of the hooks
  # engine's GA (codex-rs/hooks/src/lib.rs HOOK_EVENT_NAMES; verified 2026-08-29
  # against upstream codex-rs and the installed local CLI, matcher "other",
  # 1s default / 3s max timeout). Gate on a conservative floor version anyway:
  # a fresh Codex CLI install with hooks support at all is >= 0.117.0, and any
  # host below that silently ignores unknown hooks.json keys rather than
  # failing, so the gate is a hygiene/log-noise guard, not a hard requirement.
  local session_end_enabled="0"
  if codex_cli_version_at_least "0.117.0"; then
    session_end_enabled="1"
  fi
  # `Interrupt` shipped as a stable Codex hooks.json event in the rust-v0.150.0
  # release ("New Interrupt hooks can run commands or MCP handlers when an
  # active top-level turn is interrupted", GitHub release notes, verified
  # 2026-08-29 against https://github.com/openai/codex/releases/tag/rust-v0.150.0
  # — this superseded an earlier finding that it was upstream-main-only/alpha;
  # rust-v0.150.0 is a tagged stable release, not an alpha). Gate on that floor
  # version so hosts below it (which do not recognize the event) never receive
  # it; a host below 0.150.0 silently ignores unknown hooks.json keys rather
  # than failing, so this is a hygiene/log-noise guard, not a hard requirement.
  # AI_CODEX_HOOK_INTERRUPT=0 remains an explicit escape hatch to force this
  # off even on a detected-supporting version (e.g. a host that ships 0.150.0+
  # but has a known-bad Interrupt handler); it cannot force Interrupt ON when
  # the detected/overridden version is below the floor — version detection
  # failing must never enable a hook event the local Codex CLI may not support.
  local interrupt_enabled="0"
  if codex_cli_version_at_least "0.150.0"; then
    interrupt_enabled="1"
  fi
  case "${AI_CODEX_HOOK_INTERRUPT:-}" in
    0|false|FALSE|no|NO|off|OFF) interrupt_enabled="0" ;;
  esac
  py - "$dst" "$session_end_enabled" "$interrupt_enabled" "$SOURCE_ROOT/scripts" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
session_end_enabled = sys.argv[2] == "1"
interrupt_enabled = sys.argv[3] == "1"
contract_dir = Path(sys.argv[4])
sys.path.insert(0, str(contract_dir))
from codex_hook_contract import contains_code_brain_command, managed_codex_hooks as render_managed_hooks

# One stdlib-only contract is shared with the trust helper. This prevents the
# installer render and post-install semantic trust validation from drifting.
managed_codex_hooks = render_managed_hooks(
    session_end_enabled=session_end_enabled,
    interrupt_enabled=interrupt_enabled,
)

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

for name, managed_entries in managed_codex_hooks.items():
    existing = hooks.get(name)
    if isinstance(existing, list):
        kept = [e for e in existing if not contains_code_brain_command(e)]
    else:
        # Legacy: a single object value (older buggy install). Replace entirely.
        kept = []
    hooks[name] = kept + managed_entries
# A previous install may have written a version-gated event (SessionEnd/Interrupt)
# that is no longer enabled on this run (e.g. AI_CODEX_HOOK_INTERRUPT unset after
# being set, or a downgraded Codex CLI). Strip our own commands from any gated
# event we are not currently managing, but never touch a foreign/user entry for
# that event name.
for gated_name, enabled in (("SessionEnd", session_end_enabled), ("Interrupt", interrupt_enabled)):
    if enabled or gated_name not in hooks:
        continue
    existing = hooks.get(gated_name)
    if isinstance(existing, list):
        kept = [e for e in existing if not contains_code_brain_command(e)]
        if kept:
            hooks[gated_name] = kept
        else:
            del hooks[gated_name]
rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
if not dst.exists() or dst.read_text(encoding="utf-8") != rendered:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
PY
}

auto_trust_codex_hooks() {
  local auto_trust="${AI_CODEX_HOOK_AUTO_TRUST:-}"
  if [[ -z "$auto_trust" ]]; then
    if [[ -n "${CI:-}" || -n "${GITHUB_ACTIONS:-}" || -n "${GITLAB_CI:-}" || -n "${AI_CI:-}" ]]; then
      auto_trust="0"
    else
      auto_trust="1"
    fi
  fi
  case "$auto_trust" in
    1|true|TRUE|yes|YES|on|ON) ;;
    0|false|FALSE|no|NO|off|OFF) return 0 ;;
    *)
      echo "install-into: AI_CODEX_HOOK_AUTO_TRUST must be a boolean value" >&2
      return 2
      ;;
  esac

  local policy="${AI_CODEX_HOOK_TRUST_POLICY:-}"
  local fallback_managed_target="0"
  if [[ -z "$policy" ]]; then
    local config_home="${XDG_CONFIG_HOME:-}"
    if [[ -z "$config_home" ]]; then
      [[ -n "${HOME:-}" ]] && config_home="$HOME/.config"
    fi
    if [[ -n "$config_home" ]]; then
      local default_policy="$config_home/code-brain/codex-hook-trust.json"
      if [[ -f "$default_policy" || -L "$default_policy" ]]; then
        policy="$default_policy"
        fallback_managed_target="1"
      fi
    fi
  fi
  if [[ -n "${AI_CODEX_HOOK_TRUST_POLICY:-}" && ! -f "$policy" && ! -L "$policy" ]]; then
    return 0
  fi

  local helper="$SOURCE_ROOT/scripts/trust-codex-hooks.py"
  if [[ ! -f "$helper" ]]; then
    echo "install-into: Codex hook trust helper is missing: $helper" >&2
    return 1
  fi
  if [[ -n "$policy" ]]; then
    local -a trust_args=(--cwd "$TARGET_ROOT" --policy "$policy")
    if [[ "$fallback_managed_target" == "1" ]]; then
      trust_args+=(--fallback-managed-target)
    fi
    if ! py "$helper" "${trust_args[@]}"; then
      echo "install-into: managed files committed, but Codex hook trust failed" >&2
      return 1
    fi
  elif ! py "$helper" --cwd "$TARGET_ROOT" --trust-managed-target; then
    echo "install-into: managed files committed, but default Codex hook trust failed" >&2
    return 1
  fi
}

remove_codex_hook_trust_before_uninstall() {
  local enabled="${AI_CODEX_HOOK_AUTO_TRUST:-}"
  if [[ -z "$enabled" ]]; then
    if [[ -n "${CI:-}" || -n "${GITHUB_ACTIONS:-}" || -n "${GITLAB_CI:-}" || -n "${AI_CI:-}" ]]; then
      enabled="0"
    else
      enabled="1"
    fi
  fi
  case "$enabled" in
    1|true|TRUE|yes|YES|on|ON) ;;
    0|false|FALSE|no|NO|off|OFF) return 0 ;;
    *)
      echo "install-into: AI_CODEX_HOOK_AUTO_TRUST must be a boolean value" >&2
      return 2
      ;;
  esac
  local helper="$SOURCE_ROOT/scripts/trust-codex-hooks.py"
  if [[ ! -f "$helper" ]]; then
    echo "install-into: Codex hook trust helper missing; uninstall will leave prior hash state" >&2
    return 0
  fi
  # Removing trust before filesystem mutation is fail-safe: if the later
  # transaction rolls back, the still-installed hooks merely require review.
  # Project trust and every foreign/global hook hash remain untouched.
  if ! py "$helper" --cwd "$TARGET_ROOT" --remove-managed-target; then
    echo "install-into: could not remove managed Codex hook hashes; continuing uninstall" >&2
  fi
}

merge_antigravity_mcp_json() {
  local dst="$TARGET_ROOT/.agents/mcp_config.json"
  py - "$dst" "$SOURCE_ROOT" <<'PY'
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
source_root = Path(sys.argv[2])
sys.path.insert(0, str(source_root / ".ai" / "runtime" / "src"))
from ai_core.mcp_config import code_brain_stdio_entry, merge_antigravity_mcp_json

target_windows = os.environ.get("AI_INSTALL_TARGET_WINDOWS", "").lower() in {"1", "true", "yes", "on"}
merge_antigravity_mcp_json(dst, server_entry=code_brain_stdio_entry(windows=target_windows))
PY
}

merge_antigravity_hooks_json() {
  local dst="$TARGET_ROOT/.agents/hooks.json"
  py - "$dst" <<'PY'
import json
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
target_windows = os.environ.get("AI_INSTALL_TARGET_WINDOWS", "").lower() in {"1", "true", "yes", "on"}
# Antigravity 2.0 / CLI 1.1.x hooks.json schema (official hooks reference,
# verified 2026-08-28): the file is a top-level map of
# {"<hook-name>": JSONHookSpec}. A JSONHookSpec has one field per supported
# lifecycle EVENT, and Antigravity supports exactly five:
#   PreToolUse, PostToolUse, PreInvocation, PostInvocation, Stop
# There is NO SessionStart / UserPromptSubmit — those Claude events are unknown to
# Antigravity (they parse as a named hook with zero handlers). Each event maps to
# null. PreToolUse/PostToolUse use matcher-groups; PreInvocation/PostInvocation/Stop
# use a DIRECT handler list and ignore matchers. The legacy Claude-shaped
# wrapper ({"_note":..., "hooks": {...}}) is unparseable by Antigravity
# ("cannot unmarshal string into jsonhook.JSONHookSpec") and is dropped here.
#
# Antigravity does not pass CLAUDE_PROJECT_DIR, so resolve the repo root via git.
# Memory injection for agy is delivered via the managed AGENTS.md block
# (ai_core.agents_md), NOT these hooks: Antigravity command-hook stdout cannot
# inject model context. These hooks cover the side effects that do work —
# command routing (PreToolUse), tool-result recording (PostToolUse), and
# request-baseline capture (PreInvocation), and session-end recording + AGENTS.md refresh
# (Stop). PreInvocation runs the baseline only when invocationNum=0; later invocations are
# cheap no-ops, and its native output is {"injectSteps": []}.
def cmd(event: str) -> str:
    if target_windows:
        return (
            'powershell -NoProfile -Command "$ROOT = (git rev-parse --show-toplevel 2>$null); '
            'if (-not $ROOT) { $ROOT = (Get-Location).Path }; '
            '& \\"$ROOT/.ai/bin/ai-hook.ps1\\" ' + event + '"'
        )
    return (
        'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
        f'"$ROOT/.ai/bin/ai-hook" {event}'
    )

def matchers(event: str, timeout: int):
    return [{"matcher": "", "hooks": [{"type": "command", "command": cmd(event), "timeout": timeout}]}]

def handlers(event: str, timeout: int):
    return [{"type": "command", "command": cmd(event), "timeout": timeout}]

# NOTE: no PreToolUse hook for Antigravity. Its jsonhook contract is deny-by-default —
# unless the hook returns an approve schema agy recognizes, EVERY tool call is denied
# (verified live: empty stdout, "{}", and a Claude-style permissionDecision:allow were all
# treated as deny, hard-stalling the worker). Code Brain's PreToolUse therefore broke agy
# rather than protecting it. PostToolUse (redaction/recording) and Stop (memory refresh) work
# fine. Pre-execution risk for agy workers is covered by the loopd dispatch approval-gate.
# Timeout ceilings match the doctor's managed-command-hook policy
# (_check_code_brain_command_hooks in .ai/runtime/src/ai_core/doctor.py,
# verified 2026-08-29): "Stop" is in its hot-path set (<=5s); PostToolUse and
# PreInvocation are not (<=2s, observation/baseline-capture only, no
# user-visible turn is waiting on them).
code_brain_spec = {
    "PreToolUse": None,
    "PostToolUse": matchers("PostToolUse", 2),
    "PreInvocation": handlers("PreInvocation", 2),
    "PostInvocation": None,
    "Stop": handlers("Stop", 5),
}

if dst.exists():
    try:
        payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"install-into failed: existing {dst} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"install-into failed: existing {dst} is not a JSON object")
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

rendered = json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
if not dst.exists() or dst.read_text(encoding="utf-8") != rendered:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
PY
}


# Kiro-owned lifecycle hooks: .kiro/hooks/code-brain.json, v1 standalone hook
# file schema (read by Kiro CLI 3.0 early-access `--v3` and Kiro IDE 1.0+; the
# default CLI 2.x engine does not read this directory at all — verified
# 2026-08-29 against kiro.dev/docs/cli/hooks/ and the CLI 3.0 migration
# guide). Writing this file is therefore inert on an unmigrated 2.x install —
# a forward-compatible seed, not a behavior change.
#
# Filename/shape must match .ai/runtime/src/ai_core/doctor.py's
# check_hook_capabilities, which is the authoritative contract this function
# is verified against (that module is owned by another worker and is
# read-only here): fixed path .kiro/hooks/code-brain.json, top-level
# {"version":"v1","hooks":[{name,description,trigger,action:{type,command},
# timeout,enabled}, ...]}, with timeout a TOP-LEVEL field on each hook row
# (sibling of trigger/action/enabled), not inside action and not
# `timeout_ms`.
#
# Scope decision (deliberately conservative):
#   - Writes ONLY .kiro/hooks/code-brain.json (fixed name, never renamed
#     across installs so upgrades find and rewrite the same file). Every
#     other file under .kiro/hooks/ is left completely untouched — this repo
#     already has a live user-authored hook there
#     (continuous-improvement-continuation.json) that must never be read,
#     merged into, or overwritten.
#   - Does NOT touch .kiro/agents/*.json (CLI 2.x embedded-hook format). No
#     agent config exists in a fresh target, and additively splicing a
#     `hooks` block into a user's hand-authored agent JSON is a materially
#     different (structural, non-file-scoped) merge than anything else this
#     installer does; the official CLI 3.0 migration path is `kiro-cli agent
#     migrate`/`/upgrade-agent`, which already owns that conversion.
#
# Five triggers are wired, matching doctor's expected active set exactly:
# SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop.
#   - PreToolUse CAN actually block: with AI_HOOK_AGENT=kiro set,
#     hook_exit_code() (.ai/runtime/src/ai_core/hooks.py, verified
#     2026-08-29) returns exit code 1 for a "block" decision on PreToolUse/
#     UserPromptSubmit for the kiro agent, and Kiro's official Shell Command
#     action contract (kiro.dev/docs/hooks/actions/, verified 2026-08-29)
#     states that exit code 2 from a PreToolUse hook blocks that
#     tool invocation (same for UserPromptSubmit blocking the prompt). This
#     is NOT observe-only — do not describe it that way in this file's
#     comments or in status text.
#   - Stop is pure observation/advisory, with NO block path at all: unlike
#     PreToolUse/UserPromptSubmit, hook_exit_code() never maps a kiro Stop
#     "block" decision to a non-zero exit status (it only special-cases
#     PreToolUse/UserPromptSubmit/PreTaskExec for the kiro agent), so a
#     non-zero exit from this Stop command has no documented Kiro effect on
#     whether the session actually stops. Do not claim this hook can force,
#     request, or otherwise influence continuation — it is recorded/observed
#     only.
#   - SessionStart/UserPromptSubmit inject additionalContext: Kiro's Shell
#     Command action contract states that exit code 0 stdout is added to the
#     agent's context, and kiro_wire_output() (.ai/runtime/src/ai_core/
#     hooks.py) only emits text for these two triggers when the upstream
#     response actually carries an additionalContext value — so wiring them
#     is safe (no-op stdout when there is nothing to inject).
#   - PostToolUse is observe/record only (no block path exists for it on any
#     host in this installer).
#   - AI_HOOK_AGENT=kiro is set explicitly on every command: normalize_agent()
#     only infers "kiro" from an explicit `agent` payload field or from
#     KIRO_CLI/KIRO_HOME env vars, and Kiro's own documented stdin payload
#     (hook_event_name/cwd/session_id/tool_name/...) carries none of those —
#     so without this override a real Kiro payload would normalize to
#     "unknown" and get Codex's JSON wire projection instead of Kiro's plain
#     stdout/exit-code contract.
#   - matcher is a TOP-LEVEL field on the hook row (sibling of trigger/
#     action/timeout/enabled), NOT nested inside action — per the official
#     schema (kiro.dev/docs/hooks/ field reference and the CLI 3.0 migration
#     guide's own examples, both verified 2026-08-21: every sample row shows
#     {"trigger":...,"matcher":...,"action":{...},"timeout":...}). The
#     `matcher` field is OPTIONAL and an omitted matcher means always-match
#     (per Kiro's official docs, updated 2026-08-21). PreToolUse and
#     PostToolUse therefore omit `matcher` entirely rather than passing the
#     bare literal "*": the installed Kiro App's v2 hook engine
#     (kiro.kiro-agent/dist/extension.js, verified 2026-08-29 against the
#     locally installed Kiro.app) compiles any non-empty matcher string
#     directly with JavaScript's `new RegExp(matcher)` — the bare literal
#     "*" is invalid regex syntax ("Nothing to repeat") and a hook whose
#     matcher fails to compile never matches any tool call again (silent,
#     permanent no-op for PreToolUse/PostToolUse, which are toolName-scoped
#     triggers there). Omitting the field avoids that failure mode entirely
#     and matches doctor's kiro PreToolUse/PostToolUse check
#     (_check_code_brain_command_hooks in .ai/runtime/src/ai_core/doctor.py,
#     owned by another worker and read-only here), which now requires the
#     matcher be omitted for these two events.
#   - Windows parity: the command itself branches on AI_INSTALL_TARGET_WINDOWS
#     to invoke ai-hook.ps1 via powershell (Kiro's v1 schema has no separate
#     commandWindows field the way Codex/Claude/Antigravity hooks.json do —
#     there is exactly one `action.command` string per row).
#   - Timeout tiers: PreToolUse/Stop are hot-path (<=5s, matching doctor's
#     hot_path set which includes both by name); SessionStart/
#     UserPromptSubmit/PostToolUse are observation/context-injection only
#     (<=2s).
merge_kiro_hooks() {
  local dst="$TARGET_ROOT/.kiro/hooks/code-brain.json"
  py - "$dst" <<'PY'
import json
import os
import sys
from pathlib import Path

dst = Path(sys.argv[1])
target_windows = os.environ.get("AI_INSTALL_TARGET_WINDOWS", "").lower() in {"1", "true", "yes", "on"}

def cmd(event: str) -> str:
    if target_windows:
        return (
            'powershell -NoProfile -Command "$ROOT = (git rev-parse --show-toplevel 2>$null); '
            'if (-not $ROOT) { $ROOT = (Get-Location).Path }; '
            '$env:AI_HOOK_AGENT = \'kiro\'; '
            '& \"$ROOT/.ai/bin/ai-hook.ps1\" ' + event + '"'
        )
    return (
        'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
        f'AI_HOOK_AGENT=kiro "$ROOT/.ai/bin/ai-hook" {event}'
    )

def hook_row(name, description, trigger, timeout, matcher=None):
    # `matcher` is a TOP-LEVEL field on the hook row (sibling of trigger/action/timeout/
    # enabled), never nested inside action — see the merge_kiro_hooks() docstring above for
    # the official schema citation.
    row = {
        "name": name,
        "description": description,
        "trigger": trigger,
        "action": {"type": "command", "command": cmd(trigger)},
        "timeout": timeout,
        "enabled": True,
    }
    if matcher is not None:
        row["matcher"] = matcher
    return row

payload = {
    "version": "v1",
    "hooks": [
        hook_row(
            "code-brain-session-start",
            "Load Code Brain session context at the start of a Kiro session.",
            "SessionStart",
            2,
        ),
        hook_row(
            "code-brain-user-prompt-submit",
            "Load Code Brain prompt context before the model sees the submitted prompt.",
            "UserPromptSubmit",
            2,
        ),
        hook_row(
            "code-brain-pre-tool-use",
            "Check Code Brain command routing before a tool runs; a block decision denies the tool call (non-zero exit).",
            "PreToolUse",
            5,
        ),
        hook_row(
            "code-brain-post-tool-use",
            "Record Code Brain tool-result context after a tool runs.",
            "PostToolUse",
            2,
        ),
        hook_row(
            "code-brain-stop",
            "Advisory continuation feedback at end of turn (Kiro's Stop trigger cannot hard-block).",
            "Stop",
            5,
        ),
    ],
}

rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
if not dst.exists() or dst.read_text(encoding="utf-8") != rendered:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8")
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
  # version no longer ships (e.g. retired cb-loop*/cb-pool prompts). Also migrate the historical
  # installer bug that treated the source repo's .ai/outputs artifacts as managed runtime files.
  # Both paths are previous-manifest-gated, so target-created output files are never touched.
  # Runs before the copy/manifest rewrite and inside the rollback transaction. Fail-soft.
  local manifest; manifest="$(manifest_path)"
  [[ -f "$manifest" ]] || return 0
  local newlist; newlist="$(mktemp)" || return 0
  managed_files >"$newlist" 2>/dev/null
  py - "$TARGET_ROOT" "$SOURCE_ROOT" "$manifest" "$newlist" <<'PY'
import json, subprocess, sys
from pathlib import Path
target = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve()
manifest = Path(sys.argv[3])
newlist = Path(sys.argv[4])
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

# Old installers accidentally copied every source-side report into every target, then recorded
# those copies in the install manifest. Remove only those prior-manifest-owned paths. Never scan
# the target output tree, and never clean the source repository itself: both rules protect genuine
# project artifacts while making one upgrade remove the propagation leak.
removed_outputs: list[Path] = []
removed_output_bytes = 0
if target != source:
    tracked_outputs: set[str] | None = None
    try:
        tracked = subprocess.run(
            ["git", "-C", str(target), "ls-files", "-z", "--", ".ai/outputs"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if tracked.returncode == 0:
            tracked_outputs = {
                entry.decode("utf-8", errors="surrogateescape")
                for entry in tracked.stdout.split(b"\0")
                if entry
            }
    except OSError:
        tracked_outputs = None

    for rel in sorted(prev_set):
        if (
            tracked_outputs is None
            or not rel.startswith(".ai/outputs/")
            or rel == ".ai/outputs/.gitkeep"
            or rel in new_managed
            or rel in tracked_outputs
        ):
            continue
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            continue
        path = target / rel_path
        try:
            path.parent.resolve(strict=False).relative_to(target)
        except (OSError, ValueError):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            removed_outputs.append(path)
            removed_output_bytes += size
        except OSError:
            continue

    output_root = target / ".ai" / "outputs"
    parents = {
        parent
        for path in removed_outputs
        for parent in path.parents
        if parent != output_root and output_root in parent.parents
    }
    for directory in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

if removed_outputs:
    print(
        f"pruned {len(removed_outputs)} retired managed output artifact(s) "
        f"({removed_output_bytes} bytes)",
        file=sys.stderr,
    )
PY
  rm -f "$newlist"
}

install_transaction_dir() {
  printf '%s\n' "$TARGET_ROOT/.code-brain-install-transaction"
}

write_install_transaction_phase() {
  local txn="$1"
  local phase="$2"
  py - "$txn" "$phase" <<'PY'
import os
import sys
from pathlib import Path

txn = Path(sys.argv[1])
phase = sys.argv[2]
tmp = txn / ".phase.tmp"
with tmp.open("w", encoding="utf-8") as handle:
    handle.write(phase + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, txn / "phase")
if hasattr(os, "O_DIRECTORY"):
    fd = os.open(txn, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PY
}

write_install_transaction_marker() {
  local txn="$1"
  local marker="$2"
  py - "$txn" "$marker" <<'PY'
import os
import sys
from pathlib import Path

txn = Path(sys.argv[1])
marker = sys.argv[2]
if not marker or "/" in marker or "\\" in marker:
    raise SystemExit("invalid transaction marker")
path = txn / marker
with path.open("wb") as handle:
    handle.write(b"1\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
}

begin_install_transaction() {
  local txn
  txn="$(install_transaction_dir)"
  if [[ -e "$txn" || -L "$txn" ]]; then
    echo "install-into failed: unresolved install transaction exists: $txn" >&2
    return 6
  fi
  mkdir "$txn"
  chmod 700 "$txn"
  py - "$TARGET_ROOT" "$txn" "$$" "$ACTION" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
txn = Path(sys.argv[2]).resolve()
payload = {"schema": 1, "target": str(root), "pid": int(sys.argv[3]), "action": sys.argv[4]}
tmp = txn / ".owner.tmp"
with tmp.open("w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, txn / "owner.json")
PY
  write_install_transaction_phase "$txn" "SNAPSHOTTING"
  local prior_hooks_path
  if prior_hooks_path="$(git -C "$TARGET_ROOT" config --get core.hooksPath)"; then
    printf '%s' "$prior_hooks_path" >"$txn/core-hooks-path.value"
    : >"$txn/core-hooks-path.present"
  else
    local hooks_read_rc=$?
    if [[ "$hooks_read_rc" != "1" ]]; then
      echo "install-into failed: cannot read existing core.hooksPath" >&2
      rm -rf "$txn"
      return "$hooks_read_rc"
    fi
    : >"$txn/core-hooks-path.absent"
  fi
  {
    managed_files
    printf '%s\n' \
      ".ai/secret_scan_allowlist.txt" \
      ".ai/generated/install-manifest.json" \
      ".mcp.json" \
      ".codex/config.toml" \
      ".codex/hooks.json" \
      ".claude/settings.json" \
      ".agents/mcp_config.json" \
      ".agents/hooks.json" \
      ".kiro/hooks/code-brain.json" \
      ".gitignore" \
      "AGENTS.md" \
      "CLAUDE.md" \
      "bootstrap-code-brain.sh"
  } | awk 'NF && !seen[$0]++' >"$txn/paths.txt"
  if py - "$TARGET_ROOT" "$txn" <<'PY'
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
txn = Path(sys.argv[2]).resolve()
paths = {
    line.strip()
    for line in (txn / "paths.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
}
install_manifest = root / ".ai" / "generated" / "install-manifest.json"
if install_manifest.is_file():
    try:
        prior = json.loads(install_manifest.read_text(encoding="utf-8"))
        paths.update(item for item in prior.get("files", []) if isinstance(item, str))
    except Exception:
        pass
for rel_dir in (".claude/commands", ".codex/prompts", ".agents/skills"):
    base = root / rel_dir
    if base.is_dir():
        paths.update(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())

records = []
absent_dirs: set[str] = set()
files_root = txn / "files"
for rel in sorted(paths):
    rp = Path(rel)
    if rp.is_absolute() or ".." in rp.parts:
        print(f"install-into failed: unsafe transaction path {rel}", file=sys.stderr)
        raise SystemExit(3)
    path = root / rp
    for parent in path.parents:
        if parent == root:
            break
        if not parent.exists():
            absent_dirs.add(parent.relative_to(root).as_posix())
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError:
        print(f"install-into failed: target path escapes project root: {rel}", file=sys.stderr)
        raise SystemExit(3)
    if path.is_symlink():
        records.append({"rel": rel, "kind": "symlink", "target": os.readlink(path)})
    elif path.is_file():
        backup = files_root / rp
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup, follow_symlinks=False)
        backup.chmod(0o600)
        data = backup.read_bytes()
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
        records.append({
            "rel": rel,
            "kind": "file",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    elif path.exists():
        print(f"install-into failed: transaction target is not a file {rel}", file=sys.stderr)
        raise SystemExit(3)
    else:
        records.append({"rel": rel, "kind": "absent"})
snapshot = txn / "snapshot.json"
temporary = txn / ".snapshot.tmp"
with temporary.open("w", encoding="utf-8") as handle:
    json.dump(
        {"schema": 1, "target": str(root), "records": records, "absent_dirs": sorted(absent_dirs)},
        handle,
        ensure_ascii=False,
        sort_keys=True,
    )
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, snapshot)
if hasattr(os, "O_DIRECTORY"):
    fd = os.open(txn, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PY
  then
    :
  else
    local rc=$?
    rm -rf "$txn"
    return "$rc"
  fi
  write_install_transaction_phase "$txn" "READY"
  printf '%s\n' "$txn"
}

rollback_install_transaction() {
  local txn="$1"
  py - "$TARGET_ROOT" "$txn" <<'PY'
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
txn = Path(sys.argv[2]).resolve()
payload = json.loads((txn / "snapshot.json").read_text(encoding="utf-8"))
if payload.get("schema") != 1 or Path(str(payload.get("target") or "")).resolve() != root:
    raise SystemExit("install-into failed: rollback snapshot target/schema mismatch")
for row in payload.get("records", []):
    if row.get("kind") != "file":
        continue
    rel = str(row.get("rel") or "")
    rp = Path(rel)
    if not rel or rp.is_absolute() or ".." in rp.parts:
        raise SystemExit("install-into failed: unsafe rollback record")
    backup = txn / "files" / rp
    if backup.is_symlink() or not backup.is_file():
        raise SystemExit(f"install-into failed: rollback backup missing: {rel}")
    data = backup.read_bytes()
    if len(data) != int(row.get("size", -1)) or hashlib.sha256(data).hexdigest() != row.get("sha256"):
        raise SystemExit(f"install-into failed: rollback backup integrity mismatch: {rel}")
for row in payload.get("records", []):
    rel = str(row.get("rel") or "")
    rp = Path(rel)
    if not rel or rp.is_absolute() or ".." in rp.parts:
        continue
    path = root / rp
    kind = row.get("kind")
    if kind == "absent":
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        continue
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    if kind == "file":
        shutil.copy2(txn / "files" / rp, path, follow_symlinks=False)
    elif kind == "symlink":
        os.symlink(str(row.get("target") or ""), path)
for rel in sorted(payload.get("absent_dirs", []), key=lambda item: item.count("/"), reverse=True):
    rp = Path(str(rel))
    if rp.is_absolute() or ".." in rp.parts:
        continue
    path = root / rp
    try:
        path.rmdir()
    except OSError:
        pass
PY
  local files_rc=$?
  local hooks_rc=0
  if [[ -f "$txn/core-hooks-path.present" ]]; then
    local prior_hooks_path
    prior_hooks_path="$(cat "$txn/core-hooks-path.value")"
    git -C "$TARGET_ROOT" config core.hooksPath "$prior_hooks_path" || hooks_rc=$?
    if [[ "$hooks_rc" == "0" && "$(git -C "$TARGET_ROOT" config --get core.hooksPath || true)" != "$prior_hooks_path" ]]; then
      hooks_rc=1
    fi
  else
    git -C "$TARGET_ROOT" config --unset-all core.hooksPath >/dev/null 2>&1 || true
    if git -C "$TARGET_ROOT" config --get core.hooksPath >/dev/null 2>&1; then
      hooks_rc=1
    fi
  fi
  [[ "$files_rc" == "0" && "$hooks_rc" == "0" ]]
}

_INSTALL_TXN_DIR=""
_INSTALL_VENV_BACKUP=""
_INSTALL_RUNTIME_PREPARED=0

prepare_runtime_transaction() {
  # The source repository's own venv hosts this installer's inline Python. Moving it out
  # from under a self-upgrade would also remove the rollback interpreter mid-transaction.
  [[ "$TARGET_ROOT" != "$SOURCE_ROOT" ]] || return 0
  local venv="$TARGET_ROOT/.ai/runtime/.venv"
  local backup="$TARGET_ROOT/.ai/runtime/.venv.code-brain-rollback"
  mkdir -p "$TARGET_ROOT/.ai/runtime"
  if [[ -L "$venv" || -L "$backup" ]]; then
    echo "install-into failed: refusing symlinked runtime environment transaction" >&2
    return 3
  fi
  if [[ -e "$backup" && ! -d "$backup" ]]; then
    echo "install-into failed: runtime rollback path is not a directory: $backup" >&2
    return 3
  fi
  if [[ -n "${_INSTALL_TXN_DIR:-}" ]]; then
    [[ -d "$venv" ]] && write_install_transaction_marker "$_INSTALL_TXN_DIR" "runtime-had-venv"
    write_install_transaction_marker "$_INSTALL_TXN_DIR" "runtime-prepared"
  fi
  if [[ -d "$backup" ]]; then
    # A prior process may have died after moving the old venv. Keep that known-good backup,
    # discard only the interrupted replacement, and retry from a fresh environment.
    [[ -e "$venv" ]] && rm -rf "$venv"
  elif [[ -d "$venv" ]]; then
    mv "$venv" "$backup"
  fi
  _INSTALL_VENV_BACKUP="$backup"
  _INSTALL_RUNTIME_PREPARED=1
}

rollback_runtime_transaction() {
  [[ "${_INSTALL_RUNTIME_PREPARED:-0}" == "1" ]] || return 0
  local venv="$TARGET_ROOT/.ai/runtime/.venv"
  [[ -e "$venv" ]] && rm -rf "$venv"
  if [[ -n "${_INSTALL_VENV_BACKUP:-}" && -d "$_INSTALL_VENV_BACKUP" ]]; then
    mv "$_INSTALL_VENV_BACKUP" "$venv"
  fi
  _INSTALL_RUNTIME_PREPARED=0
  _INSTALL_VENV_BACKUP=""
}

commit_runtime_transaction() {
  [[ "${_INSTALL_RUNTIME_PREPARED:-0}" == "1" ]] || return 0
  if [[ -n "${_INSTALL_VENV_BACKUP:-}" && -d "$_INSTALL_VENV_BACKUP" ]]; then
    rm -rf "$_INSTALL_VENV_BACKUP"
  fi
  _INSTALL_RUNTIME_PREPARED=0
  _INSTALL_VENV_BACKUP=""
}

mark_install_transaction_committed() {
  [[ -n "${_INSTALL_TXN_DIR:-}" ]] || return 0
  write_install_transaction_phase "$_INSTALL_TXN_DIR" "COMMITTED"
}

recover_interrupted_install_transaction() {
  local txn
  txn="$(install_transaction_dir)"
  [[ -e "$txn" || -L "$txn" ]] || return 0
  local phase
  phase="$(py - "$TARGET_ROOT" "$txn" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
raw = Path(sys.argv[2])
if raw.is_symlink() or not raw.is_dir():
    raise SystemExit("install-into failed: transaction journal is not a trusted directory")
state = raw.stat()
if os.name != "nt":
    if state.st_uid != os.geteuid() or stat.S_IMODE(state.st_mode) & 0o077:
        raise SystemExit("install-into failed: transaction journal owner/mode is unsafe")
txn = raw.resolve()
try:
    owner = json.loads((txn / "owner.json").read_text(encoding="utf-8"))
    phase = (txn / "phase").read_text(encoding="utf-8").strip()
except (OSError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit("install-into failed: transaction journal is incomplete or corrupt") from exc
if owner.get("schema") != 1 or Path(str(owner.get("target") or "")).resolve() != root:
    raise SystemExit("install-into failed: transaction journal target/schema mismatch")
pid = int(owner.get("pid") or 0)
if pid > 0:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise SystemExit(f"install-into failed: another install transaction may be active (pid {pid})") from exc
    else:
        raise SystemExit(f"install-into failed: another install transaction is active (pid {pid})")
if phase not in {"SNAPSHOTTING", "READY", "COMMITTED"}:
    raise SystemExit("install-into failed: transaction journal has an unknown phase")
print(phase)
PY
)" || return $?

  local venv="$TARGET_ROOT/.ai/runtime/.venv"
  local backup="$TARGET_ROOT/.ai/runtime/.venv.code-brain-rollback"
  if [[ "$phase" == "COMMITTED" ]]; then
    if [[ -L "$backup" || ( -e "$backup" && ! -d "$backup" ) ]]; then
      echo "install-into failed: committed runtime backup is unsafe: $backup" >&2
      return 6
    fi
    [[ -d "$backup" ]] && rm -rf "$backup"
    rm -rf "$txn"
    echo "install-into: finalized a previously committed transaction" >&2
    return 0
  fi
  if [[ "$phase" == "SNAPSHOTTING" ]]; then
    # No target mutation starts until READY is durable.
    rm -rf "$txn"
    echo "install-into: discarded an interrupted pre-mutation snapshot" >&2
    return 0
  fi

  rollback_install_transaction "$txn" || return $?
  if [[ -f "$txn/runtime-prepared" ]]; then
    if [[ -L "$venv" || -L "$backup" || ( -e "$backup" && ! -d "$backup" ) ]]; then
      echo "install-into failed: interrupted runtime transaction contains an unsafe path" >&2
      return 6
    fi
    if [[ -d "$backup" ]]; then
      [[ -e "$venv" ]] && rm -rf "$venv"
      mkdir -p "$(dirname "$venv")"
      mv "$backup" "$venv"
    elif [[ ! -f "$txn/runtime-had-venv" && -e "$venv" ]]; then
      rm -rf "$venv"
    fi
  fi
  rm -rf "$txn"
  echo "install-into: recovered an interrupted transaction; previous files/settings restored" >&2
}

rollback_install_on_error() {
  local rc="${1:-1}"
  trap - ERR INT TERM
  set +e
  if [[ -n "${_INSTALL_TXN_DIR:-}" && -d "$_INSTALL_TXN_DIR" ]]; then
    rollback_install_transaction "$_INSTALL_TXN_DIR"
    local restore_rc=$?
    rollback_runtime_transaction
    local runtime_restore_rc=$?
    if [[ "$runtime_restore_rc" != "0" ]]; then
      restore_rc="$runtime_restore_rc"
    fi
    if [[ "$restore_rc" == "0" ]]; then
      rm -rf "$_INSTALL_TXN_DIR"
      echo "install-into: failed; previous managed files and user settings restored" >&2
    else
      echo "install-into: failed; automatic rollback also failed; backup: $_INSTALL_TXN_DIR" >&2
    fi
  fi
  set -e
  return "$rc"
}

install_or_upgrade_apply() {
  prune_orphans
  copy_managed_files
  seed_user_owned_files
  merge_mcp_json
  merge_codex_config
  merge_claude_settings
  merge_codex_hooks_json
  merge_antigravity_mcp_json
  merge_antigravity_hooks_json
  merge_kiro_hooks
  configure_project
  ensure_persistent_scaffold
  write_bootstrap
  local executable
  for executable in \
    "$TARGET_ROOT/.ai/bin/ai" \
    "$TARGET_ROOT/.ai/bin/ai-hook" \
    "$TARGET_ROOT/.ai/bin/ai-mcp" \
    "$TARGET_ROOT/.githooks/post-merge" \
    "$TARGET_ROOT/.githooks/post-checkout" \
    "$TARGET_ROOT/scripts/env-check.sh" \
    "$TARGET_ROOT/scripts/preflight.sh"
  do
    [[ -x "$executable" ]] || chmod +x "$executable"
  done
  write_install_manifest

  case "${AI_INSTALL_DEFER_RUNTIME:-0}" in
    1|true|TRUE|yes|YES|on|ON)
      echo "install-into: runtime activation deferred; run bootstrap-code-brain.sh and session start in the target" >&2
      restore_managed_owner_if_root
      return 0
      ;;
  esac

  prepare_runtime_transaction

  case "${AI_INSTALL_TARGET_WINDOWS:-0}" in
    1|true|TRUE|yes|YES|on|ON)
      if ! command -v cygpath >/dev/null 2>&1 || ! command -v powershell.exe >/dev/null 2>&1; then
        echo "install-into failed: Windows activation requires Git for Windows (cygpath and powershell.exe)" >&2
        return 2
      fi
      local _source_windows _target_windows
      _source_windows="$(cygpath -w "$SOURCE_ROOT")"
      _target_windows="$(cygpath -w "$TARGET_ROOT")"
      powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
        "$_source_windows/scripts/activate-windows.ps1" -TargetRoot "$_target_windows"
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
  # Explicit installs/upgrades self-heal a spliced audit chain before the
  # session writes another event. GitHub upgrades use the deferred path and
  # perform the same repair in upgrade.py after bootstrap.
  $_run_as .ai/bin/ai audit repair-chain --json >/dev/null
  # session start below runs the complete doctor checks after rebuilding the
  # code and audit indexes, so avoid separate CLI startup and doctor scans.
  $_run_as env AI_BOOTSTRAP_LOW_MEMORY=1 ./bootstrap-code-brain.sh --skip-doctor --skip-render --low-memory
  # Reconcile the disposable episodic index during every public install/upgrade. This also
  # rebases stale watermarks left by legacy lossy audit folding while preserving a history-gap
  # receipt; current raw audit files remain untouched.
  $_run_as .ai/bin/ai memory episodic build --json >/dev/null
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

install_or_upgrade() {
  _INSTALL_TXN_DIR="$(begin_install_transaction)"
  trap 'rollback_install_on_error $?' ERR
  trap 'rollback_install_on_error 130; exit 130' INT
  trap 'rollback_install_on_error 143; exit 143' TERM
  install_or_upgrade_apply
  mark_install_transaction_committed
  commit_runtime_transaction
  trap - ERR INT TERM
  rm -rf "$_INSTALL_TXN_DIR"
  _INSTALL_TXN_DIR=""
  # Trust is user-global external state, so update it only after the target
  # transaction is fully committed and cannot be rolled back underneath it.
  auto_trust_codex_hooks
}

uninstall_apply() {
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "$manifest" ]]; then
    echo "install-into failed: install manifest not found: $manifest" >&2
    exit 4
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
protected_exact = {".ai/secret_scan_allowlist.txt"}
protected_prefixes = (".ai/memory/", ".ai/runtime/state/", ".ai/eval/")
for rel in sorted(payload.get("files", []), key=lambda item: item.count("/"), reverse=True):
    if rel in protected_exact or rel.startswith(protected_prefixes):
        continue
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
        cfg.pop("code-brain", None)
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
                    kept = [e for e in entries if not (
                        isinstance(e, dict) and (
                            (isinstance(e.get("command"), str) and ".ai/bin/ai-hook" in e["command"])
                            or any(
                                isinstance(h, dict)
                                and isinstance(h.get("command"), str)
                                and ".ai/bin/ai-hook" in h["command"]
                                for h in (e.get("hooks") or [])
                            )
                        )
                    )]
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
# Kiro uses one Code Brain-owned standalone file rather than merging rows into
# a shared manifest. Remove that exact file only; every sibling user hook is
# preserved. Without this, uninstall leaves five commands pointing at the
# now-removed .ai/bin/ai-hook shim.
kiro_hooks = root / ".kiro" / "hooks" / "code-brain.json"
if kiro_hooks.is_file() or kiro_hooks.is_symlink():
    kiro_hooks.unlink()
manifest.unlink(missing_ok=True)
for rel in (".ai", ".githooks"):
    path = root / rel
    if path.is_dir():
        for directory in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError:
            pass
for rel in (".claude/commands", ".codex/prompts", ".agents/skills", ".agents", ".kiro/hooks"):
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

uninstall() {
  _INSTALL_TXN_DIR="$(begin_install_transaction)"
  trap 'rollback_install_on_error $?' ERR
  trap 'rollback_install_on_error 130; exit 130' INT
  trap 'rollback_install_on_error 143; exit 143' TERM
  prepare_runtime_transaction
  remove_codex_hook_trust_before_uninstall
  uninstall_apply
  if [[ "$(git -C "$TARGET_ROOT" config --get core.hooksPath || true)" == ".githooks" ]]; then
    git -C "$TARGET_ROOT" config --unset-all core.hooksPath
  fi
  mark_install_transaction_committed
  commit_runtime_transaction
  trap - ERR INT TERM
  rm -rf "$_INSTALL_TXN_DIR"
  _INSTALL_TXN_DIR=""
}

recover_interrupted_install_transaction

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
