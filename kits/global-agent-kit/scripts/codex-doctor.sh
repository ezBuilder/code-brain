#!/usr/bin/env bash
set -euo pipefail

HOME_DIR="$HOME"
SELF_TEST=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/codex-doctor.sh [--home /path/to/home] [--self-test]

Read-only diagnostics for ~/.codex/config.toml and ~/.codex/hooks.json.
USAGE
}

while (($#)); do
  case "$1" in
    --home)
      shift
      [[ $# -gt 0 ]] || { echo "--home requires a path" >&2; exit 2; }
      HOME_DIR="$1"
      ;;
    --self-test)
      SELF_TEST=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

run_diagnostics() {
  python3 - "$HOME_DIR" <<'PY'
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    print("fail: Python 3.11+ tomllib is required for TOML diagnostics", file=sys.stderr)
    raise SystemExit(1)

home = Path(sys.argv[1]).expanduser()
codex_dir = home / ".codex"
config_path = codex_dir / "config.toml"
hooks_path = codex_dir / "hooks.json"
status = 0
warn_count = 0

HOOK_EVENTS = {
    "preToolUse",
    "postToolUse",
    "permissionRequest",
    "preCompact",
    "postCompact",
    "sessionStart",
    "userPromptSubmit",
    "stop",
}
HOOK_METADATA_KEYS = {"state"}
VALID_APPROVAL = {"untrusted", "on-request", "on-failure", "never"}
VALID_SANDBOX = {"read-only", "workspace-write", "danger-full-access"}


def ok(message):
    print(f"ok: {message}")


def info(message):
    print(f"info: {message}")


def warn(message):
    global warn_count
    warn_count += 1
    print(f"warn: {message}")


def fail(message):
    global status
    status = 1
    print(f"fail: {message}", file=sys.stderr)


def is_table(value):
    return isinstance(value, dict)


def load_config():
    if not config_path.exists():
        info(f"Codex config missing: {config_path}")
        return None
    if not config_path.is_file():
        fail(f"Codex config is not a regular file: {config_path}")
        return None
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception as exc:
        fail(f"config.toml parse error: {exc}")
        return None
    if not is_table(data):
        fail("config.toml must parse to a TOML table")
        return None
    ok("config.toml parses as TOML")
    return data


def value_at(table, dotted):
    current = table
    for part in dotted.split("."):
        if not is_table(current) or part not in current:
            return None
        current = current[part]
    return current


def check_mode(table, key, valid, label):
    value = value_at(table, key)
    if value is None:
        return
    if not isinstance(value, str):
        fail(f"{key} must be a string")
        return
    if value not in valid:
        fail(f"{key} has unsupported {label}: {value}")
    elif value == "on-failure":
        warn("approval_policy=on-failure is deprecated; prefer on-request or never")
    else:
        ok(f"{key} is supported: {value}")


def check_profiles(config):
    profiles = config.get("profiles")
    selected = config.get("profile")

    if selected is not None:
        if not isinstance(selected, str) or not selected.strip():
            fail("profile must be a non-empty string when set")
        elif not is_table(profiles) or selected not in profiles:
            fail(f"profile points to missing [profiles.{selected}]")
        else:
            ok(f"default profile exists: {selected}")

    if profiles is None:
        return
    if not is_table(profiles):
        fail("profiles must be a TOML table")
        return

    for name, profile in profiles.items():
        if not isinstance(name, str) or not name.strip():
            fail("profile names must be non-empty")
            continue
        if not is_table(profile):
            fail(f"[profiles.{name}] must be a TOML table")
            continue
        check_mode(profile, "approval_policy", VALID_APPROVAL, "approval policy")
        check_mode(profile, "sandbox_mode", VALID_SANDBOX, "sandbox mode")
        if "ask_for_approval" in profile:
            warn(f"[profiles.{name}].ask_for_approval looks like a CLI flag name; use approval_policy")


def validate_handler(handler, location):
    if not is_table(handler):
        fail(f"{location} must be an object")
        return
    handler_type = handler.get("type")
    if handler_type is not None and handler_type != "command":
        warn(f"{location}.type={handler_type!r} may be ignored; command handlers are the supported path")
    command = handler.get("command")
    if not isinstance(command, str) or not command.strip():
        fail(f"{location}.command must be a non-empty string")
    elif command.startswith("/"):
        warn(f"{location}.command starts with '/'; hooks run shell commands, not slash commands")
    if handler.get("async") is True:
        warn(f"{location}.async is not currently supported by Codex hooks")
    timeout = handler.get("timeout")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        fail(f"{location}.timeout must be a positive integer when set")


def validate_hook_group(group, location):
    if not is_table(group):
        fail(f"{location} must be an object")
        return
    matcher = group.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        fail(f"{location}.matcher must be a string when set")
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        fail(f"{location}.hooks must be an array")
        return
    if not hooks:
        warn(f"{location}.hooks is empty")
    for index, handler in enumerate(hooks):
        validate_handler(handler, f"{location}.hooks[{index}]")


def validate_hooks_table(hooks, label):
    if not is_table(hooks):
        fail(f"{label} hooks must be an object")
        return
    if not hooks:
        warn(f"{label} hooks table is empty")
        return
    event_count = 0
    for event, entries in hooks.items():
        if event in HOOK_METADATA_KEYS:
            info(f"{label}.{event} is managed metadata")
            continue
        if event not in HOOK_EVENTS:
            warn(f"{label} has unknown hook event {event!r}")
            continue
        if not isinstance(entries, list):
            fail(f"{label}.{event} must be an array")
            continue
        event_count += 1
        for index, group in enumerate(entries):
            validate_hook_group(group, f"{label}.{event}[{index}]")
    if event_count == 0:
        info(f"{label} has no hook event entries")


def check_config(config):
    if config is None:
        return
    check_mode(config, "approval_policy", VALID_APPROVAL, "approval policy")
    check_mode(config, "sandbox_mode", VALID_SANDBOX, "sandbox mode")
    if "ask_for_approval" in config:
        warn("ask_for_approval looks like a CLI flag name; use approval_policy in config.toml")
    if value_at(config, "features.codex_hooks") is False or value_at(config, "features.hooks") is False:
        warn("hooks appear disabled by feature flag")
    check_profiles(config)
    if "hooks" in config:
        validate_hooks_table(config["hooks"], "config.toml hooks")
        if hooks_path.exists():
            warn("hooks are configured in both config.toml and hooks.json; keep one source of truth when possible")


def load_hooks_json():
    if not hooks_path.exists():
        info(f"Codex hooks config missing: {hooks_path}")
        return None
    if not hooks_path.is_file():
        fail(f"Codex hooks config is not a regular file: {hooks_path}")
        return None
    try:
        data = json.loads(hooks_path.read_text())
    except Exception as exc:
        fail(f"hooks.json parse error: {exc}")
        return None
    if not is_table(data):
        fail("hooks.json must be a JSON object")
        return None
    ok("hooks.json parses as JSON")
    return data


def check_hooks_json(data):
    if data is None:
        return
    hooks = data.get("hooks")
    misplaced = sorted(key for key in data if key in HOOK_EVENTS)
    if misplaced:
        fail(f"hooks.json event keys must be nested under top-level 'hooks': {misplaced}")
    if hooks is None:
        warn("hooks.json has no top-level 'hooks' object")
        return
    validate_hooks_table(hooks, "hooks.json hooks")


config = load_config()
check_config(config)
hooks_json = load_hooks_json()
check_hooks_json(hooks_json)

print("recommendation: keep diagnostics read-only; use codex /hooks or the Codex app review UI to approve discovered hooks.")
print("recommendation: prefer installing AGENTS.md separately; do not rewrite config.toml with regex or overwrite hooks.json.")
if warn_count:
    print(f"summary: completed with {warn_count} warning(s)")
else:
    print("summary: completed without warnings")

raise SystemExit(status)
PY
}

run_self_test() {
  local tmp_home
  tmp_home="$(mktemp -d)"
  trap 'rm -rf "$tmp_home"' RETURN

  mkdir -p "$tmp_home/.codex"
  cat >"$tmp_home/.codex/config.toml" <<'TOML'
profile = "safe"
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[profiles.safe]
approval_policy = "never"
sandbox_mode = "read-only"
TOML
  cat >"$tmp_home/.codex/hooks.json" <<'JSON'
{
  "hooks": {
    "sessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "printf ready"
          }
        ]
      }
    ],
    "preToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "printf check",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
JSON
  HOME_DIR="$tmp_home" run_diagnostics >/dev/null

  cat >"$tmp_home/.codex/hooks.json" <<'JSON'
{
  "preToolUse": []
}
JSON
  if HOME_DIR="$tmp_home" run_diagnostics >/dev/null 2>&1; then
    echo "self-test failed: invalid top-level hook event was accepted" >&2
    exit 1
  fi

  echo "codex-doctor self-test ok"
}

if [[ "$SELF_TEST" -eq 1 ]]; then
  run_self_test
else
  run_diagnostics
fi
