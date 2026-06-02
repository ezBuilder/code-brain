#!/usr/bin/env pwsh
# Code Brain Windows installer. Mirrors scripts/install-into.sh on Unix.
#
# Usage:
#   scripts\install-into.ps1 <target-git-repo>
#   scripts\install-into.ps1 install <target>
#   scripts\install-into.ps1 upgrade <target>
#   scripts\install-into.ps1 uninstall <target>
#
# Reuses Python merge logic via inline scripts. Requires `python` and `git`.

param(
    [Parameter(Position = 0)] [string] $Action = "",
    [Parameter(Position = 1)] [string] $TargetArg = ""
)

$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($Action -eq "-h" -or $Action -eq "--help") {
    Write-Host "usage:"
    Write-Host "  scripts/install-into.ps1 <target-git-repo>"
    Write-Host "  scripts/install-into.ps1 install <target>"
    Write-Host "  scripts/install-into.ps1 upgrade <target>"
    Write-Host "  scripts/install-into.ps1 uninstall <target>"
    exit 2
}

if ($Action -in @("install", "upgrade", "uninstall")) {
    if (-not $TargetArg) {
        Write-Error "install-into failed: target argument required"
        exit 2
    }
}
else {
    if ($Action) {
        $TargetArg = $Action
        $Action = "install"
    }
    else {
        Write-Error "install-into failed: target argument required"
        exit 2
    }
}

if (-not (Test-Path $TargetArg)) {
    Write-Error "install-into failed: target does not exist: $TargetArg"
    exit 2
}
$TargetRoot = (Resolve-Path $TargetArg).Path

# Verify git repo
$gitTop = ""
try {
    Push-Location $TargetRoot
    $gitTop = (git rev-parse --show-toplevel 2>$null).Trim()
    Pop-Location
}
catch {
    Pop-Location
    Write-Error "install-into failed: target is not inside a git repository: $TargetRoot"
    exit 2
}
if (-not $gitTop -or (Resolve-Path $gitTop).Path -ne $TargetRoot) {
    Write-Error "install-into failed: pass the git repository root"
    exit 2
}

function Get-ManagedFiles {
    Push-Location $SourceRoot
    try {
        $tracked = @()
        cmd /c "git rev-parse --show-toplevel >NUL 2>NUL"
        if ($LASTEXITCODE -eq 0) {
            $tracked = git ls-files --cached --others --exclude-standard `
                -- .ai .githooks .claude/commands .codex/prompts `
                scripts/env-check.sh scripts/preflight.sh 2>$null
        } else {
            $roots = @(".ai", ".githooks", ".claude/commands", ".codex/prompts")
            foreach ($root in $roots) {
                if (Test-Path $root) {
                    $tracked += Get-ChildItem -Recurse -File $root | ForEach-Object {
                        $_.FullName.Substring($SourceRoot.Length).TrimStart("\", "/").Replace("\", "/")
                    }
                }
            }
            foreach ($path in @("scripts/env-check.sh", "scripts/preflight.sh")) {
                if (Test-Path $path) { $tracked += $path }
            }
        }
        return $tracked | Where-Object { $_ -and -not $_.StartsWith(".ai/runtime/.venv") }
    }
    finally {
        Pop-Location
    }
}

function Copy-ManagedFile {
    param([string]$RelPath)
    $src = Join-Path $SourceRoot $RelPath
    $dst = Join-Path $TargetRoot $RelPath
    if (-not (Test-Path $src)) { return }
    $dstDir = Split-Path $dst -Parent
    if (-not (Test-Path $dstDir)) {
        New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
    }
    Copy-Item -Force -LiteralPath $src -Destination $dst
}

function Invoke-Python {
    param([string]$Script, [string]$Argument)
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        $py = Get-Command python3 -ErrorAction SilentlyContinue
    }
    $scriptFile = New-TemporaryFile
    $scriptPath = [System.IO.Path]::ChangeExtension($scriptFile.FullName, ".py")
    Move-Item -Force -LiteralPath $scriptFile.FullName -Destination $scriptPath
    Set-Content -LiteralPath $scriptPath -Value $Script -Encoding UTF8
    try {
        if ($py) {
            & $py.Path $scriptPath $Argument
        }
        else {
            $uv = Get-Command uv -ErrorAction SilentlyContinue
            if (-not $uv) {
                Write-Error "install-into failed: python not found in PATH and uv is unavailable"
                exit 2
            }
            & $uv.Path "run" "--project" (Join-Path $SourceRoot ".ai/runtime") "python" $scriptPath $Argument
        }
    }
    finally {
        Remove-Item -Force -LiteralPath $scriptPath -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "install-into failed: python merge script returned $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

function Merge-McpJson {
    $dst = Join-Path $TargetRoot ".mcp.json"
    $py = @'
import json, sys
from pathlib import Path
dst = Path(sys.argv[1])
managed = {"mcpServers": {"code-brain": {"command": "powershell", "args": ["-NoProfile", "-File", ".ai/bin/ai-mcp.ps1"]}}}
existing = {}
if dst.exists():
    try:
        existing = json.loads(dst.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
if not isinstance(existing, dict):
    existing = {}
servers = existing.setdefault("mcpServers", {})
servers["code-brain"] = managed["mcpServers"]["code-brain"]
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
'@
    Invoke-Python -Script $py -Argument $dst
}

function Merge-CodexConfig {
    $dst = Join-Path $TargetRoot ".codex/config.toml"
    $py = @'
import sys
from pathlib import Path
dst = Path(sys.argv[1])
block = ('[mcp_servers.code-brain]\n'
         'command = "powershell"\n'
         'args = ["-NoProfile", "-File", ".ai/bin/ai-mcp.ps1"]\n')
text = dst.read_text(encoding="utf-8") if dst.exists() else ""
def strip_section(t, header):
    lines = t.splitlines(); out = []; i = 0
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
        out.append(lines[i]); i += 1
    return "\n".join(out)
cleaned = strip_section(text, "[mcp_servers.code-brain]").rstrip()
cleaned = "\n".join(l for l in cleaned.splitlines() if l.strip() != "[]").rstrip()
new_text = cleaned + "\n\n" + block if cleaned else block
def ensure_features(t):
    lines = t.splitlines(); out = []; i = 0; found = False
    while i < len(lines):
        if lines[i].strip() == "[features]":
            found = True; out.append(lines[i]); i += 1
            section = []
            while i < len(lines):
                if lines[i].lstrip().startswith("[") and not lines[i].lstrip().startswith("[]"):
                    break
                section.append(lines[i]); i += 1
            section = [sl for sl in section if not (sl.strip().startswith("codex_hooks") and "=" in sl)]
            replaced = False
            for j, sl in enumerate(section):
                if sl.strip().startswith("hooks") and "=" in sl:
                    section[j] = "hooks = true"; replaced = True; break
            if not replaced:
                while section and section[-1].strip() == "": section.pop()
                section.append("hooks = true")
            out.extend(section); continue
        out.append(lines[i]); i += 1
    if not found:
        joined = "\n".join(out).rstrip()
        return (joined + "\n\n[features]\nhooks = true\n") if joined else "[features]\nhooks = true\n"
    return "\n".join(out).rstrip() + "\n"
new_text = ensure_features(new_text)
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(new_text, encoding="utf-8")
'@
    Invoke-Python -Script $py -Argument $dst
}

function Merge-ClaudeSettings {
    $dst = Join-Path $TargetRoot ".claude/settings.json"
    $py = @'
import json, sys
from pathlib import Path
dst = Path(sys.argv[1])
hooks_dir = "${CLAUDE_PROJECT_DIR:-.}/.ai/bin/ai-hook"
events = ["PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "Stop", "SubagentStop",
          "PreCompact", "SessionEnd", "Notification", "PostCompact", "CwdChanged", "ConfigChange",
          "PermissionDenied", "InstructionsLoaded"]
matchers = {"PreToolUse": "Bash", "PostToolUse": "Edit|Write|MultiEdit|NotebookEdit"}
managed = {}
for ev in events:
    entry = {"hooks": [{"type": "command", "command": f"{hooks_dir} {ev}"}]}
    if ev in matchers:
        entry["matcher"] = matchers[ev]
    managed[ev] = [entry]
payload = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else {}
if not isinstance(payload, dict):
    raise SystemExit("settings.json is not an object")
hooks = payload.setdefault("hooks", {})
def strip(entries):
    out = []
    for e in entries or []:
        if not isinstance(e, dict): out.append(e); continue
        new_h = [h for h in e.get("hooks", []) or [] if not (isinstance(h, dict) and ".ai/bin/ai-hook" in str(h.get("command", "")))]
        if new_h:
            ne = dict(e); ne["hooks"] = new_h; out.append(ne)
        elif "hooks" not in e:
            out.append(e)
    return out
for name, entries in managed.items():
    existing = hooks.get(name) if isinstance(hooks.get(name), list) else []
    hooks[name] = strip(existing) + entries
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
'@
    Invoke-Python -Script $py -Argument $dst
}

function Merge-CodexHooksJson {
    $dst = Join-Path $TargetRoot ".codex/hooks.json"
    $py = @'
import json, sys
from pathlib import Path
dst = Path(sys.argv[1])
def cb(ev, status):
    return {"type": "command", "command": f'ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; "$ROOT/.ai/bin/ai-hook" {ev}', "statusMessage": status}
managed = {
    "PreToolUse": [{"matcher": "Bash", "hooks": [cb("PreToolUse", "Checking Code Brain command routing")]}],
    "PostToolUse": [{"matcher": "Bash|apply_patch|Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep", "hooks": [cb("PostToolUse", "Recording Code Brain tool result")]}],
    "SessionStart": [{"matcher": "startup|resume|clear", "hooks": [cb("SessionStart", "Loading Code Brain session context")]}],
    "UserPromptSubmit": [{"hooks": [cb("UserPromptSubmit", "Loading Code Brain prompt context")]}],
    "Stop": [{"hooks": [cb("Stop", "Recording Code Brain stop event")]}],
    "SubagentStop": [{"hooks": [cb("SubagentStop", "Recording Code Brain subagent stop")]}],
    "PreCompact": [{"hooks": [cb("PreCompact", "Saving Code Brain compact snapshot")]}],
    "PostCompact": [{"hooks": [cb("PostCompact", "Recording Code Brain compact completion")]}],
    "PermissionRequest": [{"matcher": "Bash", "hooks": [cb("PermissionRequest", "Checking Code Brain approval policy")]}],
}
default_payload = {"_note": "Codex hook schema may differ across versions."}
payload = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else default_payload
if not isinstance(payload, dict):
    raise SystemExit("hooks.json is not an object")
hooks = payload.setdefault("hooks", {})
def has_cb(v):
    if not isinstance(v, dict):
        return False
    if ".ai/bin/ai-hook" in str(v.get("command", "")):
        return True
    return any(isinstance(h, dict) and ".ai/bin/ai-hook" in str(h.get("command", "")) for h in v.get("hooks", []) or [])
for name, entries in managed.items():
    existing = hooks.get(name)
    kept = [e for e in existing if not has_cb(e)] if isinstance(existing, list) else []
    hooks[name] = kept + entries
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
'@
    Invoke-Python -Script $py -Argument $dst
}

function Write-InstallManifest {
    $dst = Join-Path $TargetRoot ".ai/generated/install-manifest.json"
    $sourceSha = ""
    Push-Location $SourceRoot
    try {
        cmd /c "git rev-parse --is-inside-work-tree >NUL 2>NUL"
        if ($LASTEXITCODE -eq 0) {
            $sourceSha = (git rev-parse HEAD 2>$null).Trim()
        }
    } catch {}
    Pop-Location
    $files = Get-ManagedFiles
    $project = Split-Path $TargetRoot -Leaf
    $now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $manifest = @{
        files                = $files
        installed_at         = $now
        merged_config_files  = @(".mcp.json", ".codex/config.toml", ".claude/settings.json", ".codex/hooks.json")
        project              = $project
        schema_version       = 1
        source_git_sha       = $sourceSha
        tool                 = "code-brain"
    }
    $json = $manifest | ConvertTo-Json -Depth 6
    $dstDir = Split-Path $dst -Parent
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Set-Content -LiteralPath $dst -Value $json -Encoding UTF8
}

function Ensure-PersistentScaffold {
    $dirs = @(
        ".ai/generated",
        ".ai/memory/audit",
        ".ai/memory/queue/.tmp",
        ".ai/memory/queue/processing",
        ".ai/memory/queue/dead"
    )
    foreach ($dir in $dirs) {
        $path = Join-Path $TargetRoot $dir
        if (-not (Test-Path $path)) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
    }
    foreach ($file in @(
        ".ai/memory/audit-index.jsonl",
        ".ai/memory/queue/.tmp/.gitkeep",
        ".ai/memory/queue/processing/.gitkeep",
        ".ai/memory/queue/dead/.gitkeep"
    )) {
        $path = Join-Path $TargetRoot $file
        if (-not (Test-Path $path)) { New-Item -ItemType File -Force -Path $path | Out-Null }
    }
}

function Invoke-Bootstrap {
    if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
        return
    }
    Push-Location $TargetRoot
    try {
        $bootstrap = Join-Path $TargetRoot "bootstrap-code-brain.sh"
        if (-not (Test-Path $bootstrap)) {
            # Fallback: copy bootstrap.sh wrapper if absent
            $src = Join-Path $SourceRoot "bootstrap.sh"
            if (Test-Path $src) { Copy-Item -Force -LiteralPath $src -Destination $bootstrap }
        }
        # Bootstrap script invokes uv sync; re-run via bash if available, else skip silently.
        if (Get-Command bash -ErrorAction SilentlyContinue) {
            bash $bootstrap | Out-Null
        }
        else {
            Write-Warning "bash not found; skipping bootstrap-code-brain.sh. Run manually if needed."
        }
    }
    finally {
        Pop-Location
    }
}

function Install-OrUpgrade {
    foreach ($rel in Get-ManagedFiles) {
        Copy-ManagedFile -RelPath $rel
    }
    Merge-McpJson
    Merge-CodexConfig
    Merge-ClaudeSettings
    Merge-CodexHooksJson
    Ensure-PersistentScaffold
    Write-InstallManifest
    Invoke-Bootstrap
    Write-Host "code-brain $Action`: $TargetRoot"
}

function Uninstall {
    # Uninstall reverses managed file copy. We rely on manifest's `files` list.
    $manifest = Join-Path $TargetRoot ".ai/generated/install-manifest.json"
    if (-not (Test-Path $manifest)) {
        Write-Error "uninstall failed: no manifest at $manifest"
        exit 2
    }
    $data = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    foreach ($rel in $data.files) {
        $path = Join-Path $TargetRoot $rel
        if (Test-Path $path) { Remove-Item -Force -LiteralPath $path }
    }
    Write-Host "code-brain uninstalled: $TargetRoot"
}

switch ($Action) {
    "install"   { Install-OrUpgrade }
    "upgrade"   { Install-OrUpgrade }
    "uninstall" { Uninstall }
    default     { Install-OrUpgrade }
}
exit 0
