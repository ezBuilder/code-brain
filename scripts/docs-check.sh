#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$ROOT"
unset CI GITHUB_ACTIONS GITLAB_CI AI_CI

if [[ ! -f OPERATIONS.md ]]; then
  echo "OPERATIONS.md is missing" >&2
  exit 1
fi

for needle in \
  "ai doctor --strict --json" \
  "ai report status --json" \
  "ai diagnostics bundle --dry-run --json" \
  "ai queue recover-expired --json" \
  "ai queue dead --json" \
  "ai obs health-summary --json" \
  "ai upgrade plan --target-version" \
  "exit code \`16\`" \
  "CI_READ_ONLY" \
  "release_ready" \
  "release-gate.yml" \
  "summary-observe" \
  "summary-parity.py" \
  "RELEASE_GATE_SUMMARY_SCHEMA_VERSION" \
  "dep_advisory" \
  "finding_count" \
  "audit_chain" \
  "prev_sha_mismatch" \
  "dep-advisory.json" \
  "dep-advisory.sh" \
  "release-gate.summary.json" \
  "ai report release-gate-summary" \
  "queue status" \
  "oldest_pending_age_seconds" \
  "oldest_processing_age_seconds" \
  "worker stop --force" \
  "worker health" \
  "PRODUCTION_HARDENING_BACKLOG.md" \
  "./scripts/release-gate.sh" \
  "scripts/lockfile-check.sh" \
  "make env-check" \
  "make preflight" \
  "make lockfile-check" \
  "make lock-check" \
  "make session-start" \
  "ai obs search --refresh-stale" \
  "secret_scan_allowlist.txt" \
  "no_token_estimates" \
  "ai exec run" \
  "sandbox_execute" \
  "session_resume" \
  "ai memory decision add" \
  "ai memory todo add" \
  "ai memory session append" \
  "record_decision" \
  "record_todo" \
  "append_session_note" \
  "PreToolUse" \
  "precall" \
  ".claude/settings.json" \
  ".codex/hooks.json" \
  "auto-routing" \
  "porter unicode61" \
  "mcp_methods_registered" \
  ".claude/commands/cb-" \
  ".codex/prompts/cb-" \
  "/cb-usage" \
  "/cb-health" \
  "/cb-search" \
  "/cb-doctor" \
  "bash scripts/install.sh /path/to/project" \
  "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\install.ps1 C:\\path\\to\\project" \
  "New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically." \
  "ai audit rebuild-index" \
  "ai session start" \
  "uv lock --check --project .ai/runtime" \
  "make lint" \
  "make release-gate" \
  "make stress-bounds" \
  "scripts/stress-bounds.py" \
  "make clean-all" \
  "bootstrap.ps1" \
  "bootstrap-idempotency.sh" \
  "reproducibility-check.sh" \
  "release-notes.md"
do
  if ! grep -Fq "$needle" OPERATIONS.md README.md RELEASE.md; then
    echo "documented operation missing: $needle" >&2
    exit 1
  fi
done

uv run --project .ai/runtime ai version >/dev/null
uv run --project .ai/runtime ai index rebuild --json >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai report status --skip-usage --json >/dev/null
uv run --project .ai/runtime ai obs metrics --skip-usage --json >/dev/null
uv run --project .ai/runtime ai obs health-summary --json >/dev/null
uv run --project .ai/runtime ai obs slo --json >/dev/null
uv run --project .ai/runtime ai queue status --json >/dev/null
uv run --project .ai/runtime ai queue dead --json --limit 1 >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --dry-run --skip-usage --json >/dev/null
uv run --project .ai/runtime ai upgrade plan --target-version 0.1.1 --json >/dev/null
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json >/dev/null
uv run --project .ai/runtime ai report release-notes >/dev/null
uv run --project .ai/runtime ai report release-gate-summary --git-sha "$(git rev-parse HEAD)" --skip-usage --json >/dev/null
uv run --project .ai/runtime python -c 'from ai_core.report import RELEASE_GATE_SUMMARY_SCHEMA_VERSION; assert RELEASE_GATE_SUMMARY_SCHEMA_VERSION == 3'
uv run --project .ai/runtime python -c 'from ai_core.report import release_gate_summary; from pathlib import Path; s=release_gate_summary(Path("."), include_usage=False); assert set(s["dep_advisory"]) == {"finding_count", "mode", "generated_at", "skipped"}; assert set(s["operational_bounds"]) == {"ok", "doctor_groups", "transcripts", "sandbox", "runner"}; assert s["operational_bounds"]["sandbox"]["bounded"] is True; assert s["operational_bounds"]["runner"]["bounded"] is True'
uv run --project .ai/runtime python -c 'from ai_core.doctor import check_audit_chain; from pathlib import Path; r=check_audit_chain(Path(".")); assert r.ok'
CODE_BRAIN_DEP_ADVISORY_OFFLINE=1 ./scripts/dep-advisory.sh >/dev/null
./scripts/env-check.sh >/dev/null
./scripts/preflight.sh --check-only --json >/dev/null
./scripts/lockfile-check.sh >/dev/null
make -n env-check >/dev/null
make -n preflight >/dev/null
make -n lockfile-check >/dev/null
make -n lock-check >/dev/null
make -n session-start >/dev/null
make -n install-into TARGET=/tmp/code-brain-docs-target >/dev/null
make -n upgrade-in TARGET=/tmp/code-brain-docs-target >/dev/null
make -n uninstall-from TARGET=/tmp/code-brain-docs-target >/dev/null
make -n lint >/dev/null
make -n quick >/dev/null
make -n stress-bounds >/dev/null
make -n package >/dev/null
make -n verify-artifacts >/dev/null
make -n install-check >/dev/null
make -n reproducibility-check >/dev/null
make -n tamper-check >/dev/null
make -n rollback-drill >/dev/null
make -n bootstrap-idempotency >/dev/null
make -n release-gate >/dev/null
make -n clean-cache >/dev/null
make -n clean-artifacts >/dev/null
make -n clean-all >/dev/null

CI=true uv run --project .ai/runtime ai obs metrics --skip-usage --json >/dev/null
CI=true uv run --project .ai/runtime ai obs health-summary --json >/dev/null
CI=true uv run --project .ai/runtime ai diagnostics bundle --dry-run --skip-usage --json >/dev/null

set +e
CI=true uv run --project .ai/runtime ai worker stop --force --json >/tmp/code-brain-worker-stop-ci.out 2>/tmp/code-brain-worker-stop-ci.err
status=$?
set -e
if [[ "$status" -ne 16 ]]; then
  echo "expected CI worker stop rejection exit 16, got $status" >&2
  cat /tmp/code-brain-worker-stop-ci.err >&2
  exit 1
fi

set +e
CI=true uv run --project .ai/runtime ai render >/tmp/code-brain-docs-ci-write.out 2>/tmp/code-brain-docs-ci-write.err
status=$?
set -e
if [[ "$status" -ne 16 ]]; then
  echo "expected CI write rejection exit 16, got $status" >&2
  cat /tmp/code-brain-docs-ci-write.err >&2
  exit 1
fi

COPY="$TMP/code-brain"
mkdir -p "$COPY"
tar \
  --exclude './.git' \
  --exclude './.claude' \
  --exclude './.ai/cache' \
  --exclude './.ai/runtime/.venv' \
  --exclude './.ai/runtime/.pytest_cache' \
  --exclude './.ai/runtime/src/ai_core/__pycache__' \
  --exclude './.ai/runtime/src/ai_core/worker/__pycache__' \
  --exclude './.ai/runtime/tests/__pycache__' \
  --exclude './dist' \
  -C "$ROOT" -cf - . | tar -C "$COPY" -xf -

cd "$COPY"
unset CI GITHUB_ACTIONS GITLAB_CI AI_CI
uv run --project .ai/runtime ai render --json >/dev/null
./scripts/preflight.sh --check-only --json >/dev/null
uv run --project .ai/runtime ai queue recover-expired --json >/dev/null
uv run --project .ai/runtime ai queue archive-dead --older-than-days 30 --json >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --skip-usage --json >/dev/null
uv run --project .ai/runtime ai diagnostics prune --keep-days 30 --json >/dev/null

echo "docs check ok"
