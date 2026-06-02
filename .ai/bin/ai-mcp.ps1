$Root = Resolve-Path "$PSScriptRoot/../.."
$env:PYTHONIOENCODING = "utf-8"
$IsWin = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$Python = if ($IsWin) { "$Root/.ai/runtime/.venv/Scripts/python.exe" } else { "$Root/.ai/runtime/.venv/bin/python" }
if (Test-Path $Python) {
  & $Python -c "import ai_core.cli" *> $null
  if ($LASTEXITCODE -eq 0) {
    & $Python -m ai_core.cli mcp @args
    exit $LASTEXITCODE
  }
}
uv run --project "$Root/.ai/runtime" python -m ai_core.cli mcp @args
exit $LASTEXITCODE
