#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bash -n bootstrap.sh
for script in scripts/*.sh; do
  bash -n "$script"
done

./scripts/env-check.sh >/dev/null
uv run --project .ai/runtime python -m compileall -q .ai/runtime/src .ai/runtime/tests

make -n env-check >/dev/null
make -n lockfile-check >/dev/null
make -n lock-check >/dev/null
make -n quick >/dev/null
make -n package >/dev/null
make -n verify-artifacts >/dev/null
make -n install-check >/dev/null
make -n tamper-check >/dev/null
make -n release-gate >/dev/null
make -n clean-cache >/dev/null
make -n clean-artifacts >/dev/null
make -n clean-all >/dev/null

if command -v pwsh >/dev/null 2>&1; then
  pwsh -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw 'bootstrap.ps1')) | Out-Null"
  pwsh -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw '.ai/bin/ai.ps1')) | Out-Null"
  pwsh -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw '.ai/bin/ai-hook.ps1')) | Out-Null"
elif command -v powershell >/dev/null 2>&1; then
  powershell -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw 'bootstrap.ps1')) | Out-Null"
  powershell -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw '.ai/bin/ai.ps1')) | Out-Null"
  powershell -NoProfile -NonInteractive -Command "[scriptblock]::Create((Get-Content -Raw '.ai/bin/ai-hook.ps1')) | Out-Null"
fi

echo "lint ok"
