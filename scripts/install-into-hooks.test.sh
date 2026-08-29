#!/usr/bin/env bash
# Dedicated installer test for scripts/install-into.sh's Codex/Claude/Kiro hook wiring.
#
# Scope: this file plus scripts/install-into.sh are the only files this test touches or
# asserts against. It never reads/writes anything under .ai/runtime, .ai/generated (source
# repo), or any other worker's in-progress files — every assertion runs against a disposable
# scratch git repo created by this script (mktemp -d + trap cleanup, matching the existing
# scripts/rollback-drill.sh convention).
#
# Covers:
#   1. Fresh install produces the expected Codex/Claude/Kiro managed hook events.
#   2. Repeated `upgrade` is idempotent (byte-identical .codex/hooks.json, .claude/settings.json,
#      and .kiro/hooks/code-brain.json across two consecutive runs).
#   3. Version gates: Codex SessionEnd (>=0.117.0) / Interrupt (>=0.150.0, official
#      rust-v0.150.0 release notes) and Claude StopFailure (>=2.1.78) / TeammateIdle
#      (>=2.1.33) / TaskCreated (>=2.1.84) / FileChanged+CwdChanged (>=2.1.83) turn on and
#      off correctly via the AI_*_CLI_VERSION_OVERRIDE env vars, and a downgrade cleanly
#      strips only Code Brain's own entries for events that lose gate support.
#   4. AI_CODEX_HOOK_INTERRUPT=0 forces Interrupt off even when the detected version
#      qualifies (explicit escape hatch), and never forces it on below the floor version.
#   5. Explicit per-hook `timeout` values are present on every Code Brain-managed command
#      hook across Codex/Claude/Kiro, respecting the tiered ceiling (hot-path <=5s,
#      observation-only <=2s, Codex SessionEnd fixed at 2s under its own 3s hard cap).
#   6. PreToolUse matchers cover file-write tools (Codex: apply_patch|Edit|Write; Claude:
#      Edit|Write|MultiEdit|NotebookEdit; Kiro: "shell|write|.*") in addition to shell/Bash.
#   7. A pre-existing user-authored hook file in each ecosystem survives install/upgrade
#      completely unmodified (byte-for-byte), proving the merge never touches non-Code-Brain
#      entries or files — in particular the live .kiro/hooks/continuous-improvement-
#      continuation.json shape (a "Stop" trigger, "agent" action hook) is simulated and must
#      never be read, merged into, or overwritten.
#   8. Codex additionalContextLimit is set to exactly 5000 on SessionStart/SubagentStart and
#      2500 on UserPromptSubmit (the only events Codex can actually emit additionalContext
#      from), and absent everywhere else.
#   9. Antigravity's managed command-hook timeouts respect the same hot-path/observation
#      tiers as Codex/Claude/Kiro (Stop <=5s; PostToolUse/PreInvocation <=2s).
#  10. Uninstall removes the Code Brain-owned Kiro file and dangling shim commands while
#      preserving sibling user hook files and foreign Claude/Codex entries.
#  11. A partial pre-manifest Code Brain runtime is safely adopted by `upgrade` only when
#      independent runtime, Codex-hook, and MCP markers all match; private memory survives.
#  12. Source-side `.ai/outputs` artifacts are excluded, and one upgrade removes only output
#      paths recorded by an older install manifest while preserving target-created outputs.
#  13. A later upgrade failure restores a just-pruned legacy output through the installer
#      transaction, proving the migration cannot leave a partial cleanup.
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$ROOT/scripts/install-into.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TARGET="$TMP/target-repo"
mkdir -p "$TARGET"
(cd "$TARGET" && git init -q && git config user.email test@example.com && git config user.name "install-into-hooks-test")

PY="$ROOT/.ai/runtime/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$PY" ]]; then
  echo "install-into-hooks.test failed: no python interpreter found" >&2
  exit 2
fi

fail() {
  echo "install-into-hooks.test FAILED: $1" >&2
  exit 1
}

run_install() {
  # AI_INSTALL_DEFER_RUNTIME=1 stops before bootstrap/uv-sync/session-start — this test only
  # asserts on the managed config files the merge_* functions write, not the full runtime.
  # AI_CODEX_HOOK_AUTO_TRUST=0 keeps this test hermetic: auto_trust_codex_hooks() otherwise
  # defaults to reading the OPERATOR's real $XDG_CONFIG_HOME/code-brain/codex-hook-trust.json
  # outside CI, so a developer's own private trust policy (irrelevant to this test's scratch
  # repo, and potentially referencing stale/missing project roots) must never affect this
  # test's pass/fail outcome.
  # NOTE: caller-supplied VAR=val overrides (via "$@") must go through the `env` builtin, not
  # a bare `"$@" cmd` prefix — bash only recognizes literal, unquoted VAR=val WORDS as command
  # prefix assignments at parse time; a quoted "$@" expansion never gets re-classified as an
  # assignment-word after expansion, so `"$@" bash "$INSTALL_SH" ...` always fails with
  # "command not found" on the first override (verified 2026-08-29 — this was a real bug, not
  # a hypothetical one; every call site that passes an override, e.g. `run_upgrade
  # AI_CODEX_CLI_VERSION_OVERRIDE=0.150.0`, was previously broken).
  env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 "$@" bash "$INSTALL_SH" install "$TARGET" >"$TMP/install.log" 2>&1 || {
    cat "$TMP/install.log" >&2
    fail "install command failed (env: $*)"
  }
}

run_upgrade() {
  env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 "$@" bash "$INSTALL_SH" upgrade "$TARGET" >"$TMP/upgrade.log" 2>&1 || {
    cat "$TMP/upgrade.log" >&2
    fail "upgrade command failed (env: $*)"
  }
}

json_get() {
  # json_get <file> <python-expr-using-d> — expr must be a single Python expression;
  # it is substituted directly into `print($2)`.
  "$PY" -c "
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
print($2)
" "$1"
}

json_check() {
  # json_check <file> <python-script-using-d> — script is a full multi-statement Python
  # body (not wrapped in print(...)); it must print its own result. Use this instead of
  # json_get whenever the check needs more than one expression (loops, accumulator lists,
  # etc.) — json_get's single-line `print($2)` substitution is a syntax error for those.
  "$PY" -c "
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
$2
" "$1"
}

file_md5() {
  md5 -q "$1" 2>/dev/null || md5sum "$1" | awk '{print $1}'
}

echo "== 1. seed pre-existing user-authored hook files (must survive untouched) =="
mkdir -p "$TARGET/.kiro/hooks"
cat >"$TARGET/.kiro/hooks/continuous-improvement-continuation.json" <<'EOF'
{"version":"v1","hooks":[{"name":"Infinite continuous-improvement continuation","enabled":true,"trigger":"Stop","description":"user-owned, must never be touched","action":{"type":"agent","prompt":"USER OWNED CONTENT"}}]}
EOF
kiro_user_hook_md5_before="$(file_md5 "$TARGET/.kiro/hooks/continuous-improvement-continuation.json")"

mkdir -p "$TARGET/.claude"
cat >"$TARGET/.claude/settings.json" <<'EOF'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo user-foreign-hook"}]}]}}
EOF

mkdir -p "$TARGET/.codex"
cat >"$TARGET/.codex/hooks.json" <<'EOF'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo user-foreign-codex-hook"}]}]}}
EOF

echo "== 2. fresh install =="
run_install

[[ -f "$TARGET/.codex/hooks.json" ]] || fail "codex hooks.json not written"
[[ -f "$TARGET/.claude/settings.json" ]] || fail "claude settings.json not written"
[[ -f "$TARGET/.kiro/hooks/code-brain.json" ]] || fail "kiro code-brain.json not written"

echo "== 3. pre-existing foreign/user entries survive install =="
kiro_user_hook_md5_after="$(file_md5 "$TARGET/.kiro/hooks/continuous-improvement-continuation.json")"
[[ "$kiro_user_hook_md5_before" == "$kiro_user_hook_md5_after" ]] \
  || fail "install modified the pre-existing user Kiro hook file"

foreign_claude_survived="$(json_get "$TARGET/.claude/settings.json" "any(
    isinstance(h, dict) and h.get('command') == 'echo user-foreign-hook'
    for e in d['hooks'].get('PreToolUse', []) for h in e.get('hooks', [])
)")"
[[ "$foreign_claude_survived" == "True" ]] || fail "Claude foreign PreToolUse entry was dropped"

foreign_codex_survived="$(json_get "$TARGET/.codex/hooks.json" "any(
    isinstance(h, dict) and h.get('command') == 'echo user-foreign-codex-hook'
    for e in d['hooks'].get('PreToolUse', []) for h in e.get('hooks', [])
)")"
[[ "$foreign_codex_survived" == "True" ]] || fail "Codex foreign PreToolUse entry was dropped"

echo "== 4. Kiro code-brain.json is well-formed and scoped to the doctor-expected event set =="
kiro_triggers="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "sorted(h['trigger'] for h in d['hooks'])")"
[[ "$kiro_triggers" == "['PostToolUse', 'PreToolUse', 'SessionStart', 'Stop', 'UserPromptSubmit']" ]] \
  || fail "unexpected Kiro trigger set: $kiro_triggers"
kiro_version="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "d['version']")"
[[ "$kiro_version" == "v1" ]] || fail "Kiro hook file version must be v1, got: $kiro_version"
# timeout is a TOP-LEVEL field on each hook row (sibling of trigger/action/enabled), not
# inside action and not timeout_ms (official Kiro CLI 3 migration schema).
kiro_timeout_shape_ok="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "all(
    'timeout' in h and 'timeout' not in h.get('action', {}) and 'timeout_ms' not in h
    for h in d['hooks']
)")"
[[ "$kiro_timeout_shape_ok" == "True" ]] || fail "Kiro hook timeout must be a top-level row field, not inside action/timeout_ms"

echo "== 5. explicit per-hook timeout present on every Code Brain command hook, tiered correctly =="
codex_timeouts_ok="$(json_check "$TARGET/.codex/hooks.json" "
missing = []
over = []
for name, entries in d['hooks'].items():
    hot = name in {'PreToolUse', 'UserPromptSubmit', 'PermissionRequest', 'Stop'}
    ceiling = 5 if hot else 2
    for e in entries:
        for h in e.get('hooks', []):
            cmdv = h.get('command', '')
            if '.ai/bin/ai-hook' not in cmdv:
                continue
            t = h.get('timeout')
            if t is None:
                missing.append(name)
            elif t > ceiling:
                over.append((name, t, ceiling))
print('OK' if not missing and not over else f'missing={missing} over={over}')
")"
[[ "$codex_timeouts_ok" == "OK" ]] || fail "Codex hook timeout policy violated: $codex_timeouts_ok"

claude_timeouts_ok="$(json_check "$TARGET/.claude/settings.json" "
missing = []
over = []
hot_events = {'PreToolUse', 'UserPromptSubmit', 'PermissionRequest', 'Stop', 'SubagentStop',
              'TaskCompleted', 'TeammateIdle'}
for name, entries in d['hooks'].items():
    ceiling = 5 if name in hot_events else 2
    for e in entries:
        for h in e.get('hooks', []):
            cmdv = h.get('command', '')
            if '.ai/bin/ai-hook' not in cmdv:
                continue
            t = h.get('timeout')
            if t is None:
                missing.append(name)
            elif t > ceiling:
                over.append((name, t, ceiling))
print('OK' if not missing and not over else f'missing={missing} over={over}')
")"
[[ "$claude_timeouts_ok" == "OK" ]] || fail "Claude hook timeout policy violated: $claude_timeouts_ok"

kiro_timeouts_ok="$(json_check "$TARGET/.kiro/hooks/code-brain.json" "
missing = []
over = []
hot = {'PreToolUse', 'Stop'}
for h in d['hooks']:
    ceiling = 5 if h['trigger'] in hot else 2
    t = h.get('timeout')
    if t is None:
        missing.append(h['trigger'])
    elif t > ceiling:
        over.append((h['trigger'], t, ceiling))
print('OK' if not missing and not over else f'missing={missing} over={over}')
")"
[[ "$kiro_timeouts_ok" == "OK" ]] || fail "Kiro hook timeout policy violated: $kiro_timeouts_ok"

echo "== 6. PreToolUse matchers cover file-write tools in addition to shell =="
codex_pretooluse_matcher="$(json_get "$TARGET/.codex/hooks.json" "d['hooks']['PreToolUse'][-1]['matcher']")"
for tok in apply_patch Edit Write Bash; do
  case "$codex_pretooluse_matcher" in
    *"$tok"*) ;;
    *) fail "Codex PreToolUse matcher missing '$tok': $codex_pretooluse_matcher" ;;
  esac
done

claude_pretooluse_matcher="$(json_get "$TARGET/.claude/settings.json" "d['hooks']['PreToolUse'][-1]['matcher']")"
for tok in Bash Edit Write MultiEdit NotebookEdit; do
  case "$claude_pretooluse_matcher" in
    *"$tok"*) ;;
    *) fail "Claude PreToolUse matcher missing '$tok': $claude_pretooluse_matcher" ;;
  esac
done

# Bash must be present on Claude's PostToolUse matcher (not just the file-write tool names):
# completion_guard observes test/lint/build command results via PostToolUse, and those run
# through the Bash tool, not a file-write tool. Regressing this silently blinds that path.
claude_posttooluse_matcher="$(json_get "$TARGET/.claude/settings.json" "d['hooks']['PostToolUse'][-1]['matcher']")"
case "$claude_posttooluse_matcher" in
  *Bash*) ;;
  *) fail "Claude PostToolUse matcher missing 'Bash' (breaks completion_guard's test/lint/build observation): $claude_posttooluse_matcher" ;;
esac

# Kiro's official v1 schema puts `matcher` at the TOP LEVEL of the hook row (sibling of
# trigger/action/timeout/enabled), never nested inside action — assert against the row, not
# row['action']['matcher'], so a regression that nests it back inside action is caught.
kiro_pretooluse_row_has_no_action_matcher="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "
'matcher' not in [h for h in d['hooks'] if h['trigger'] == 'PreToolUse'][0]['action']
")"
[[ "$kiro_pretooluse_row_has_no_action_matcher" == "True" ]] \
  || fail "Kiro PreToolUse matcher must not be nested inside action"
# Kiro's official docs (updated 2026-08-21) state `hooks[].matcher` is OPTIONAL and an
# omitted matcher means always-match. PreToolUse/PostToolUse are toolName-scoped triggers
# there and Code Brain's guard/observer hooks must see every tool call, so the installer
# omits the `matcher` key entirely for both rows rather than writing a matcher string (a
# bare literal "*" is invalid JS RegExp syntax — "Nothing to repeat" — and would silently
# stop matching forever; a real pattern would be an unnecessary, narrower-than-required
# constraint the docs say is not needed for always-match).
kiro_pretooluse_has_no_matcher="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "
'matcher' not in [h for h in d['hooks'] if h['trigger'] == 'PreToolUse'][0]
")"
[[ "$kiro_pretooluse_has_no_matcher" == "True" ]] \
  || fail "Kiro PreToolUse must omit matcher entirely for always-match (per official docs)"
kiro_posttooluse_has_no_matcher="$(json_get "$TARGET/.kiro/hooks/code-brain.json" "
'matcher' not in [h for h in d['hooks'] if h['trigger'] == 'PostToolUse'][0]
")"
[[ "$kiro_posttooluse_has_no_matcher" == "True" ]] \
  || fail "Kiro PostToolUse must omit matcher entirely for always-match (per official docs)"
# Regression guard against the underlying bug class (a bare "*" or any invalid JS regex
# reintroduced on ANY hook row, Kiro or otherwise, and against any Kiro row that starts
# carrying a real matcher string in the future): actually compile every present matcher
# with a real JS RegExp when node is available on the test host. Soft-skip when node is
# unavailable rather than failing the whole suite on an unrelated environment gap.
if command -v node >/dev/null 2>&1; then
  kiro_matchers_json="$(json_check "$TARGET/.kiro/hooks/code-brain.json" "
print(json.dumps([h.get('matcher') for h in d['hooks'] if h.get('matcher') is not None]))
")"
  node -e '
    const matchers = JSON.parse(process.argv[1]);
    for (const m of matchers) {
      try { new RegExp(m); }
      catch (e) { console.error("invalid JS RegExp matcher " + JSON.stringify(m) + ": " + e.message); process.exit(1); }
    }
  ' "$kiro_matchers_json" || fail "a Kiro matcher failed to compile as a real JavaScript RegExp (see stderr above)"
  bare_star_check='
    try { new RegExp("*"); console.error("bare * unexpectedly compiled"); process.exit(1); }
    catch (e) { process.exit(0); }
  '
  node -e "$bare_star_check" || fail "sanity check failed: bare literal "*" should be invalid JS RegExp"
fi

echo "== 7. Codex SessionEnd matcher is 'other', not Claude's SessionStart-shaped value =="
codex_session_end_matcher="$(json_get "$TARGET/.codex/hooks.json" "d['hooks'].get('SessionEnd', [{}])[0].get('matcher')")"
[[ "$codex_session_end_matcher" == "other" ]] || fail "Codex SessionEnd matcher should be 'other', got: $codex_session_end_matcher"

echo "== 8. Interrupt is absent by default (local Codex CLI version, no override) =="
interrupt_present_default="$(json_get "$TARGET/.codex/hooks.json" "'Interrupt' in d['hooks']")"
if command -v codex >/dev/null 2>&1; then
  local_codex_version="$(codex --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
else
  local_codex_version=""
fi
# Only assert Interrupt-absent when we can independently confirm the local Codex CLI (if any)
# is below 0.150.0; otherwise this assertion would be environment-dependent noise.
# NOTE: capture the Python version-compare exit code directly into a variable rather than
# checking $? after a separate statement — an intervening `|| true` (or any other command)
# between the heredoc and a bare `$?` check silently pins $? to that intervening command's
# exit status, not the heredoc's. `if "$PY" ... ; then` avoids that trap entirely.
if [[ -n "$local_codex_version" ]]; then
  if "$PY" - "$local_codex_version" <<'PY'
import sys
parts = [int(p) for p in sys.argv[1].split(".")[:3]]
sys.exit(0 if parts < [0, 150, 0] else 1)
PY
  then
    [[ "$interrupt_present_default" == "False" ]] || fail "Interrupt should be absent below Codex 0.150.0"
  fi
fi

echo "== 9. version gate: Codex Interrupt turns on at >=0.150.0, off below, override forces off =="
run_upgrade AI_CODEX_CLI_VERSION_OVERRIDE=0.150.0
interrupt_on="$(json_get "$TARGET/.codex/hooks.json" "'Interrupt' in d['hooks']")"
[[ "$interrupt_on" == "True" ]] || fail "Interrupt did not enable at override 0.150.0"

run_upgrade AI_CODEX_CLI_VERSION_OVERRIDE=0.149.0
interrupt_off="$(json_get "$TARGET/.codex/hooks.json" "'Interrupt' in d['hooks']")"
[[ "$interrupt_off" == "False" ]] || fail "Interrupt did not disable below floor version 0.150.0"

run_upgrade AI_CODEX_CLI_VERSION_OVERRIDE=0.150.0 AI_CODEX_HOOK_INTERRUPT=0
interrupt_forced_off="$(json_get "$TARGET/.codex/hooks.json" "'Interrupt' in d['hooks']")"
[[ "$interrupt_forced_off" == "False" ]] || fail "AI_CODEX_HOOK_INTERRUPT=0 did not force Interrupt off"

echo "== 10. version gate: Codex SessionEnd on/off around its 0.117.0 floor =="
run_upgrade AI_CODEX_CLI_VERSION_OVERRIDE=0.100.0
session_end_off="$(json_get "$TARGET/.codex/hooks.json" "'SessionEnd' in d['hooks']")"
[[ "$session_end_off" == "False" ]] || fail "SessionEnd did not disable below floor version 0.117.0"

run_upgrade AI_CODEX_CLI_VERSION_OVERRIDE=0.117.0
session_end_on="$(json_get "$TARGET/.codex/hooks.json" "'SessionEnd' in d['hooks']")"
[[ "$session_end_on" == "True" ]] || fail "SessionEnd did not enable at override 0.117.0"

echo "== 11. version gate: Claude StopFailure/TeammateIdle/TaskCreated/FileChanged/CwdChanged =="
run_upgrade AI_CLAUDE_CLI_VERSION_OVERRIDE=2.0.0
claude_all_off="$(json_get "$TARGET/.claude/settings.json" "all(
    k not in d['hooks'] for k in ('StopFailure', 'TeammateIdle', 'TaskCreated', 'FileChanged', 'CwdChanged')
)")"
[[ "$claude_all_off" == "True" ]] || fail "one or more version-gated Claude events did not disable at 2.0.0"

run_upgrade AI_CLAUDE_CLI_VERSION_OVERRIDE=2.1.220
claude_all_on="$(json_get "$TARGET/.claude/settings.json" "all(
    k in d['hooks'] for k in ('StopFailure', 'TeammateIdle', 'TaskCreated', 'FileChanged', 'CwdChanged')
)")"
[[ "$claude_all_on" == "True" ]] || fail "one or more version-gated Claude events did not enable at 2.1.220"

echo "== 12. individual Claude floors: 2.1.33 enables TeammateIdle but not StopFailure/TaskCreated/FileChanged =="
run_upgrade AI_CLAUDE_CLI_VERSION_OVERRIDE=2.1.33
claude_partial="$(json_get "$TARGET/.claude/settings.json" "(
    'TeammateIdle' in d['hooks'],
    'StopFailure' in d['hooks'],
    'TaskCreated' in d['hooks'],
    'FileChanged' in d['hooks'],
)")"
[[ "$claude_partial" == "(True, False, False, False)" ]] \
  || fail "unexpected gate state at Claude 2.1.33: $claude_partial"

echo "== 13. restore to local/default versions before idempotency check =="
run_upgrade

echo "== 14. repeated upgrade at a fixed version is byte-identical (idempotent) =="
codex_md5_1="$(file_md5 "$TARGET/.codex/hooks.json")"
claude_md5_1="$(file_md5 "$TARGET/.claude/settings.json")"
kiro_md5_1="$(file_md5 "$TARGET/.kiro/hooks/code-brain.json")"

run_upgrade
codex_md5_2="$(file_md5 "$TARGET/.codex/hooks.json")"
claude_md5_2="$(file_md5 "$TARGET/.claude/settings.json")"
kiro_md5_2="$(file_md5 "$TARGET/.kiro/hooks/code-brain.json")"

run_upgrade
codex_md5_3="$(file_md5 "$TARGET/.codex/hooks.json")"
claude_md5_3="$(file_md5 "$TARGET/.claude/settings.json")"
kiro_md5_3="$(file_md5 "$TARGET/.kiro/hooks/code-brain.json")"

[[ "$codex_md5_1" == "$codex_md5_2" && "$codex_md5_2" == "$codex_md5_3" ]] \
  || fail "repeated upgrade is not idempotent for .codex/hooks.json"
[[ "$claude_md5_1" == "$claude_md5_2" && "$claude_md5_2" == "$claude_md5_3" ]] \
  || fail "repeated upgrade is not idempotent for .claude/settings.json"
[[ "$kiro_md5_1" == "$kiro_md5_2" && "$kiro_md5_2" == "$kiro_md5_3" ]] \
  || fail "repeated upgrade is not idempotent for .kiro/hooks/code-brain.json"

echo "== 15. pre-existing user/foreign entries still intact after all upgrade cycles =="
kiro_user_hook_md5_final="$(file_md5 "$TARGET/.kiro/hooks/continuous-improvement-continuation.json")"
[[ "$kiro_user_hook_md5_before" == "$kiro_user_hook_md5_final" ]] \
  || fail "repeated upgrades eventually modified the pre-existing user Kiro hook file"

foreign_claude_survived_final="$(json_get "$TARGET/.claude/settings.json" "any(
    isinstance(h, dict) and h.get('command') == 'echo user-foreign-hook'
    for e in d['hooks'].get('PreToolUse', []) for h in e.get('hooks', [])
)")"
[[ "$foreign_claude_survived_final" == "True" ]] || fail "Claude foreign PreToolUse entry lost after upgrades"

echo "== 16. only the Code Brain-owned Kiro file exists next to the user's file (no stray files) =="
kiro_hook_files="$("$PY" -c "
import os
print(sorted(os.listdir('$TARGET/.kiro/hooks')))
")"
[[ "$kiro_hook_files" == "['code-brain.json', 'continuous-improvement-continuation.json']" ]] \
  || fail "unexpected .kiro/hooks/ directory listing: $kiro_hook_files"

echo "== 17. Codex additionalContextLimit is exactly 5000/5000/2500 on SessionStart/SubagentStart/UserPromptSubmit and absent elsewhere =="
codex_context_limits_ok="$(json_check "$TARGET/.codex/hooks.json" "
expected = {'SessionStart': 5000, 'SubagentStart': 5000, 'UserPromptSubmit': 2500}
bad = []
for name, entries in d['hooks'].items():
    for e in entries:
        for h in e.get('hooks', []):
            cmdv = h.get('command', '')
            if '.ai/bin/ai-hook' not in cmdv:
                continue
            limit = h.get('additionalContextLimit')
            if name in expected:
                if limit != expected[name]:
                    bad.append((name, 'expected', expected[name], 'got', limit))
            elif limit is not None:
                bad.append((name, 'unexpected additionalContextLimit', limit))
print('OK' if not bad else f'bad={bad}')
")"
[[ "$codex_context_limits_ok" == "OK" ]] || fail "Codex additionalContextLimit policy violated: $codex_context_limits_ok"

echo "== 18. Antigravity managed command-hook timeouts respect the hot-path/observation tiers =="
if [[ -f "$TARGET/.agents/hooks.json" ]]; then
  antigravity_timeouts_ok="$(json_check "$TARGET/.agents/hooks.json" "
spec = d.get('code-brain', {})
hot = {'Stop'}
bad = []
def walk(name, value):
    if isinstance(value, dict):
        if value.get('type') == 'command' and isinstance(value.get('command'), str):
            if '.ai/bin/ai-hook' not in value['command']:
                return
            ceiling = 5 if name in hot else 2
            t = value.get('timeout')
            if t is None or t > ceiling:
                bad.append((name, t, ceiling))
            return
        for v in value.values():
            walk(name, v)
    elif isinstance(value, list):
        for v in value:
            walk(name, v)
for name, entries in spec.items():
    walk(name, entries)
print('OK' if not bad else f'bad={bad}')
")"
  [[ "$antigravity_timeouts_ok" == "OK" ]] || fail "Antigravity hook timeout policy violated: $antigravity_timeouts_ok"
else
  echo "  (skipped: .agents/hooks.json not written by this installer run)"
fi

echo "== 19. Kiro command uses the PowerShell/.ps1 shim on a Windows target (no separate commandWindows field exists in Kiro's v1 schema) =="
WIN_TARGET="$TMP/win-target-repo"
mkdir -p "$WIN_TARGET"
(cd "$WIN_TARGET" && git init -q && git config user.email test@example.com && git config user.name "install-into-hooks-test")
AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 AI_INSTALL_TARGET_WINDOWS=1 bash "$INSTALL_SH" install "$WIN_TARGET" >"$TMP/win-install.log" 2>&1 || {
  cat "$TMP/win-install.log" >&2
  fail "Windows-target install command failed"
}
win_kiro_stop_command="$(json_get "$WIN_TARGET/.kiro/hooks/code-brain.json" "
[h for h in d['hooks'] if h['trigger'] == 'Stop'][0]['action']['command']
")"
case "$win_kiro_stop_command" in
  *"ai-hook.ps1"*"Stop"*) ;;
  *) fail "Windows Kiro Stop command should invoke ai-hook.ps1: $win_kiro_stop_command" ;;
esac
case "$win_kiro_stop_command" in
  powershell*) ;;
  *) fail "Windows Kiro Stop command should start with powershell: $win_kiro_stop_command" ;;
esac
win_kiro_action_has_no_command_windows="$(json_get "$WIN_TARGET/.kiro/hooks/code-brain.json" "
all('commandWindows' not in h['action'] and 'commandWindows' not in h for h in d['hooks'])
")"
[[ "$win_kiro_action_has_no_command_windows" == "True" ]] \
  || fail "Kiro v1 schema has no commandWindows field; the Windows branch must live inside action.command itself"

echo "== 20. uninstall removes only Code Brain's Kiro hook and leaves no dangling shim command =="
env AI_CODEX_HOOK_AUTO_TRUST=0 bash "$INSTALL_SH" uninstall "$TARGET" >"$TMP/uninstall.log" 2>&1 || {
  cat "$TMP/uninstall.log" >&2
  fail "uninstall command failed"
}
[[ ! -e "$TARGET/.kiro/hooks/code-brain.json" && ! -L "$TARGET/.kiro/hooks/code-brain.json" ]] \
  || fail "uninstall left the Code Brain-owned Kiro hook file behind"
[[ ! -e "$TARGET/.ai/bin/ai-hook" && ! -L "$TARGET/.ai/bin/ai-hook" ]] \
  || fail "uninstall left the managed hook shim behind"
kiro_user_hook_md5_uninstalled="$(file_md5 "$TARGET/.kiro/hooks/continuous-improvement-continuation.json")"
[[ "$kiro_user_hook_md5_before" == "$kiro_user_hook_md5_uninstalled" ]] \
  || fail "uninstall modified or removed the sibling user Kiro hook file"
foreign_claude_after_uninstall="$(json_get "$TARGET/.claude/settings.json" "any(
    isinstance(h, dict) and h.get('command') == 'echo user-foreign-hook'
    for e in d['hooks'].get('PreToolUse', []) for h in e.get('hooks', [])
)")"
[[ "$foreign_claude_after_uninstall" == "True" ]] || fail "uninstall removed the foreign Claude hook"
foreign_codex_after_uninstall="$(json_get "$TARGET/.codex/hooks.json" "any(
    isinstance(h, dict) and h.get('command') == 'echo user-foreign-codex-hook'
    for e in d['hooks'].get('PreToolUse', []) for h in e.get('hooks', [])
)")"
[[ "$foreign_codex_after_uninstall" == "True" ]] || fail "uninstall removed the foreign Codex hook"
[[ ! -d "$TARGET/.code-brain-install-transaction" ]] || fail "uninstall left transaction residue"

echo "== 21. partial pre-manifest Code Brain runtime upgrades without deleting private memory =="
UNRELATED_TARGET="$TMP/unrelated-ai-target"
mkdir -p "$UNRELATED_TARGET/.ai/runtime"
(cd "$UNRELATED_TARGET" && git init -q && git config user.email test@example.com && git config user.name "install-into-hooks-test")
cp "$ROOT/.ai/runtime/pyproject.toml" "$UNRELATED_TARGET/.ai/runtime/pyproject.toml"
if env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 \
  bash "$INSTALL_SH" upgrade "$UNRELATED_TARGET" >"$TMP/unrelated-upgrade.log" 2>&1; then
  fail "a runtime marker alone must not authorize partial legacy adoption"
fi
grep -Fq "Code Brain is not installed" "$TMP/unrelated-upgrade.log" \
  || fail "incomplete legacy markers did not fail with the expected diagnostic"
[[ ! -e "$UNRELATED_TARGET/.ai/generated/install-manifest.json" ]] \
  || fail "incomplete legacy markers mutated the target"

LEGACY_TARGET="$TMP/partial-legacy-target"
mkdir -p \
  "$LEGACY_TARGET/.ai/runtime/tests" \
  "$LEGACY_TARGET/.ai/memory" \
  "$LEGACY_TARGET/.codex"
(cd "$LEGACY_TARGET" && git init -q && git config user.email test@example.com && git config user.name "install-into-hooks-test")
cp "$ROOT/.ai/runtime/pyproject.toml" "$LEGACY_TARGET/.ai/runtime/pyproject.toml"
cp "$ROOT/.codex/hooks.json" "$LEGACY_TARGET/.codex/hooks.json"
cp "$ROOT/.codex/config.toml" "$LEGACY_TARGET/.codex/config.toml"
printf 'legacy managed copy\n' >"$LEGACY_TARGET/.ai/runtime/tests/test_codex_hook_auto_trust.py"
printf '{"private":"keep"}\n' >"$LEGACY_TARGET/.ai/memory/user-sentinel.jsonl"
env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 \
  bash "$INSTALL_SH" upgrade "$LEGACY_TARGET" >"$TMP/legacy-upgrade.log" 2>&1 || {
    cat "$TMP/legacy-upgrade.log" >&2
    fail "partial legacy upgrade command failed"
  }
[[ -f "$LEGACY_TARGET/.ai/generated/install-manifest.json" ]] \
  || fail "partial legacy upgrade did not create the install manifest"
[[ -x "$LEGACY_TARGET/.ai/bin/ai-hook" ]] \
  || fail "partial legacy upgrade did not install the hook router"
cmp -s \
  "$ROOT/.ai/runtime/tests/test_codex_hook_auto_trust.py" \
  "$LEGACY_TARGET/.ai/runtime/tests/test_codex_hook_auto_trust.py" \
  || fail "partial legacy upgrade did not replace a stale managed runtime file"
[[ "$(cat "$LEGACY_TARGET/.ai/memory/user-sentinel.jsonl")" == '{"private":"keep"}' ]] \
  || fail "partial legacy upgrade modified private memory"

echo "== 22. source output artifacts never propagate; prior managed leaks are pruned safely =="
mkdir -p "$LEGACY_TARGET/.ai/outputs/old-source-report"
printf 'copied by an old installer\n' >"$LEGACY_TARGET/.ai/outputs/old-source-report/result.log"
printf 'target-owned, keep\n' >"$LEGACY_TARGET/.ai/outputs/user-result.log"
printf 'tracked target artifact, keep\n' >"$LEGACY_TARGET/.ai/outputs/tracked-result.log"
git -C "$LEGACY_TARGET" add -- .ai/outputs/tracked-result.log
"$PY" - "$LEGACY_TARGET/.ai/generated/install-manifest.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["files"].append(".ai/outputs/old-source-report/result.log")
payload["files"].append(".ai/outputs/tracked-result.log")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 \
  bash "$INSTALL_SH" upgrade "$LEGACY_TARGET" >"$TMP/output-migration-upgrade.log" 2>&1 || {
    cat "$TMP/output-migration-upgrade.log" >&2
    fail "output-leak migration upgrade failed"
  }
[[ ! -e "$LEGACY_TARGET/.ai/outputs/old-source-report/result.log" ]] \
  || fail "upgrade retained an output artifact owned by the previous install manifest"
[[ ! -d "$LEGACY_TARGET/.ai/outputs/old-source-report" ]] \
  || fail "upgrade retained an empty retired output directory"
[[ "$(cat "$LEGACY_TARGET/.ai/outputs/user-result.log")" == 'target-owned, keep' ]] \
  || fail "upgrade modified a target-owned output absent from the previous manifest"
[[ "$(cat "$LEGACY_TARGET/.ai/outputs/tracked-result.log")" == 'tracked target artifact, keep' ]] \
  || fail "upgrade modified a Git-tracked target output recorded by the previous manifest"
manifest_outputs="$(json_get "$LEGACY_TARGET/.ai/generated/install-manifest.json" "[
    p for p in d['files'] if p.startswith('.ai/outputs/')
]")"
[[ "$manifest_outputs" == "['.ai/outputs/.gitkeep']" ]] \
  || fail "install manifest still contains source output artifacts: $manifest_outputs"

echo "== 23. failed upgrade rolls back output-leak cleanup atomically =="
mkdir -p "$LEGACY_TARGET/.ai/outputs/old-source-report"
printf 'restore me after rollback\n' >"$LEGACY_TARGET/.ai/outputs/old-source-report/rollback.log"
CONFLICT_REL=".ai/runtime/tests/test_codex_hook_auto_trust.py"
printf 'target-owned collision\n' >"$LEGACY_TARGET/$CONFLICT_REL"
"$PY" - "$LEGACY_TARGET/.ai/generated/install-manifest.json" "$CONFLICT_REL" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
conflict = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
payload["files"] = [entry for entry in payload["files"] if entry != conflict]
payload["files"].append(".ai/outputs/old-source-report/rollback.log")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if env AI_INSTALL_DEFER_RUNTIME=1 AI_CODEX_HOOK_AUTO_TRUST=0 \
  bash "$INSTALL_SH" upgrade "$LEGACY_TARGET" >"$TMP/output-migration-rollback.log" 2>&1; then
  fail "output migration rollback fixture unexpectedly upgraded successfully"
fi
grep -Fq "refusing to overwrite existing untracked target file $CONFLICT_REL" \
  "$TMP/output-migration-rollback.log" \
  || fail "rollback fixture did not reach the expected post-prune collision"
[[ "$(cat "$LEGACY_TARGET/.ai/outputs/old-source-report/rollback.log")" == 'restore me after rollback' ]] \
  || fail "failed upgrade did not restore the pruned manifest-owned output"
[[ "$(cat "$LEGACY_TARGET/$CONFLICT_REL")" == 'target-owned collision' ]] \
  || fail "failed upgrade modified the target-owned collision file"
[[ ! -d "$LEGACY_TARGET/.code-brain-install-transaction" ]] \
  || fail "failed output migration left transaction residue"

echo "install-into-hooks.test: all checks passed"
