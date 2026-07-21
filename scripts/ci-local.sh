#!/usr/bin/env bash
# Local mirror of GitHub CI. Run BEFORE pushing so a failure shows up in seconds
# here instead of after a multi-minute CI round-trip on a slow runner.
#
# By default it mirrors the two jobs that actually catch most failures:
#   1. doctor          -> `make quick`            (same command the doctor job runs)
#   2. windows-runtime -> the windows-portability pytest subset (same -k filter;
#                         the windows job strips CI markers, so this matches by
#                         running in plain local mode — powershell/ps1 cases skip
#                         off-Windows)
# Pass --full to also run the release-gate job (ubuntu/macos). That stage needs a
# CLEAN working tree (it checks `git status --short`), so commit first.
#
# Keep the -k filter below in sync with .github/workflows/windows.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

WIN_K="preflight or mcp_methods_registered or secret_scan_uses_git_baseline_not_local_noise or os_aware or powershell or ps1 or windows"

fail() { echo "❌ ci-local: $1 FAILED — fix before pushing"; exit 1; }

# Refresh the search index first. A fresh CI checkout has no index (doctor reports
# "not indexed", which is OK); locally a stale index would fail index_freshness, so
# rebuild to reflect the current tree before the doctor check runs.
echo "── [0] refresh index ──"
uv run --project .ai/runtime ai index rebuild >/dev/null 2>&1 || true

echo "── [1/2] doctor job  (make quick) ──"
make quick || fail "make quick (doctor job)"

echo "── [2/2] windows-runtime job  (portability pytest subset) ──"
./scripts/run-observed-command.sh ci-local-windows-runtime -- \
  uv run --project .ai/runtime python -m pytest \
    .ai/runtime/tests/test_cli.py \
    .ai/runtime/tests/test_mcp_config_and_antigravity.py \
    .ai/runtime/tests/test_recommend.py \
    -k "$WIN_K" --tb=short -q || fail "windows-portability pytest subset"

if [[ "${1:-}" == "--full" ]]; then
  echo "── [3/3] release-gate job  (needs a clean working tree) ──"
  ./scripts/release-gate.sh || fail "release-gate"
fi

echo "✅ ci-local: all mirrored checks passed — safe to push"
