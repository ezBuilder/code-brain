$Root = Resolve-Path "$PSScriptRoot/../.."
$Python = if ($IsWindows) { "$Root/.ai/runtime/.venv/Scripts/python.exe" } else { "$Root/.ai/runtime/.venv/bin/python" }
if (Test-Path $Python) {
  & $Python -m ai_core.cli hook @args
} else {
  uv run --project "$Root/.ai/runtime" ai hook @args
}
