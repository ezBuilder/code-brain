#!/usr/bin/env bash
set -euo pipefail
umask 077

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-install}"

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
    git ls-files --cached --others --exclude-standard -- \
      .ai \
      .githooks \
      .claude/commands \
      .codex/prompts \
      scripts/env-check.sh \
      scripts/preflight.sh
  ) | grep -vx ".ai/secret_scan_allowlist.txt" || true
  printf '%s\n' "bootstrap-code-brain.sh"
}

# User-owned files seeded on first install but never managed afterwards.
# Manifest does NOT track these — uninstall will leave them alone.
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
}

merged_config_files() {
  printf '%s\n' ".mcp.json" ".codex/config.toml"
}

manifest_path() {
  printf '%s\n' "$TARGET_ROOT/.ai/generated/install-manifest.json"
}

is_managed_existing_file() {
  local rel="$1"
  local manifest
  manifest="$(manifest_path)"
  [[ -f "$manifest" ]] && python - "$manifest" "$rel" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
rel = sys.argv[2]
payload = json.loads(manifest.read_text(encoding="utf-8"))
raise SystemExit(0 if rel in payload.get("files", []) else 1)
PY
}

copy_file() {
  local rel="$1"
  local src="$SOURCE_ROOT/$rel"
  local dst="$TARGET_ROOT/$rel"
  if [[ "$rel" == "bootstrap-code-brain.sh" ]]; then
    return 0
  fi
  if [[ "$ACTION" == "upgrade" && "$rel" == .ai/memory/* && -e "$dst" ]]; then
    return 0
  fi
  if [[ ! -f "$src" ]]; then
    echo "install-into failed: missing source file $rel" >&2
    exit 2
  fi
  if [[ -e "$dst" ]] && ! cmp -s "$src" "$dst" && ! is_managed_existing_file "$rel"; then
    echo "install-into failed: refusing to overwrite existing untracked target file $rel" >&2
    exit 3
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
}

write_bootstrap() {
  cat >"$TARGET_ROOT/bootstrap-code-brain.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
./scripts/preflight.sh --check-only >/dev/null
./scripts/env-check.sh >/dev/null
uv sync --project .ai/runtime
if git rev-parse --git-dir >/dev/null 2>&1; then
  git config core.hooksPath .githooks
fi
uv run --project .ai/runtime ai render --manifest-only --json >/dev/null
uv run --project .ai/runtime ai doctor --json >/dev/null
EOF
  chmod +x "$TARGET_ROOT/bootstrap-code-brain.sh"
}

write_install_manifest() {
  mkdir -p "$TARGET_ROOT/.ai/generated"
  python -c '
import json
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
payload = {
    "schema_version": 1,
    "tool": "code-brain",
    "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "project": root.name,
    "files": sorted(set(files)),
    "merged_config_files": [".mcp.json", ".codex/config.toml", ".claude/settings.json", ".codex/hooks.json"],
    "source_git_sha": source,
}
print(json.dumps(payload, indent=2, sort_keys=True))
' "$TARGET_ROOT" "$SOURCE_ROOT" >"$(manifest_path)" < <(managed_files)
}

configure_project() {
  python - "$TARGET_ROOT" <<'PY'
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
  python - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
desired = {"command": ".ai/bin/ai-mcp", "args": [], "env": {}}
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
  python - "$dst" <<'PY'
import re
import sys
from pathlib import Path

dst = Path(sys.argv[1])
block = (
    "[mcp_servers.code-brain]\n"
    "command = \".ai/bin/ai-mcp\"\n"
    "args = []\n"
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

def ensure_features_codex_hooks(text: str) -> str:
    """Idempotently set [features].codex_hooks = true without disturbing
    other user-defined keys in the [features] table or other sections."""
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
            replaced = False
            for j, sl in enumerate(section_lines):
                sl_stripped = sl.strip()
                if sl_stripped.startswith("codex_hooks") and "=" in sl_stripped:
                    section_lines[j] = "codex_hooks = true"
                    replaced = True
                    break
            if not replaced:
                # Append before trailing blank lines so the file stays tidy.
                while section_lines and section_lines[-1].strip() == "":
                    section_lines.pop()
                section_lines.append("codex_hooks = true")
            out.extend(section_lines)
            set_in_section = True
            continue
        out.append(line)
        i += 1
    if not found_section:
        joined = "\n".join(out).rstrip()
        suffix = "\n\n[features]\ncodex_hooks = true\n"
        return (joined + suffix) if joined else suffix.lstrip()
    return "\n".join(out).rstrip() + "\n"

new_text = ensure_features_codex_hooks(new_text)

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(new_text, encoding="utf-8")
PY
}

merge_claude_settings() {
  local dst="$TARGET_ROOT/.claude/settings.json"
  python - "$dst" <<'PY'
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
        {"hooks": [{"type": "command", "command": "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook SessionStart"}]}
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
  python - "$dst" <<'PY'
import json
import sys
from pathlib import Path

dst = Path(sys.argv[1])
managed_codex_hooks = {
    "PreToolUse": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook PreToolUse", "matchers": ["Bash"]}],
    "PostToolUse": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook PostToolUse", "matchers": ["Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep"]}],
    "SessionStart": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook SessionStart"}],
    "UserPromptSubmit": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook UserPromptSubmit"}],
    "Stop": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook Stop"}],
    "SubagentStop": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook SubagentStop"}],
    "PermissionRequest": [{"command": "${CODEX_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-.}}/.ai/bin/ai-hook PermissionRequest"}],
}
if dst.exists():
    try:
        payload = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"install-into failed: existing {dst} is not valid JSON")
    if not isinstance(payload, dict):
        raise SystemExit(f"install-into failed: existing {dst} is not a JSON object")
else:
    payload = {"_note": "Codex hook schema may differ across versions. Verify with your codex CLI release. PreToolUse currently supports deny rules only — input rewriting awaits upstream updatedInput support."}
hooks = payload.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"install-into failed: existing {dst}.hooks must be a JSON object")

def _has_code_brain_command(hook_value):
    if isinstance(hook_value, dict):
        cmd = hook_value.get("command")
        return isinstance(cmd, str) and "/.ai/bin/ai-hook" in cmd
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

install_or_upgrade() {
  while IFS= read -r rel; do
    copy_file "$rel"
  done < <(managed_files)
  seed_user_owned_files
  merge_mcp_json
  merge_codex_config
  merge_claude_settings
  merge_codex_hooks_json
  configure_project
  write_bootstrap
  chmod +x "$TARGET_ROOT/.ai/bin/ai" "$TARGET_ROOT/.ai/bin/ai-hook" "$TARGET_ROOT/.ai/bin/ai-mcp"
  chmod +x "$TARGET_ROOT/.githooks/post-merge" "$TARGET_ROOT/.githooks/post-checkout"
  chmod +x "$TARGET_ROOT/scripts/env-check.sh" "$TARGET_ROOT/scripts/preflight.sh"
  write_install_manifest

  cd "$TARGET_ROOT"
  ./bootstrap-code-brain.sh >/dev/null
  .ai/bin/ai audit rebuild-index --json >/dev/null
  .ai/bin/ai session start --agent operator --rebuild always --json >/dev/null
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
  python - "$TARGET_ROOT" "$manifest" <<'PY'
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
for rel in (".claude/commands", ".codex/prompts"):
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
    if [[ ! -f "$(manifest_path)" ]]; then
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
