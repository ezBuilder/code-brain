#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer python3; some systems (e.g. macOS) ship only `python3`, not `python`.
PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [[ -z "$PYTHON" ]]; then
  echo "env-check failed: no python3/python interpreter found on PATH" >&2
  exit 2
fi

"$PYTHON" - "$ROOT" <<'PY'
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])


def command_version(command: str, *args: str) -> dict:
    path = shutil.which(command)
    result = {"command": command, "path": path, "ok": bool(path), "version": None}
    if not path:
        return result
    try:
        output = subprocess.check_output([path, *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        return result
    result["version"] = output.splitlines()[0] if output else ""
    return result


checks = {
    "bash": command_version("bash", "--version"),
    "git": command_version("git", "--version"),
    "make": command_version("make", "--version"),
    "uv": command_version("uv", "--version"),
}

python_check = {"command": "python", "path": None, "ok": False, "version": None}
uv_path = checks["uv"].get("path")
if uv_path:
    try:
        output = subprocess.check_output(
            [uv_path, "run", "--project", ".ai/runtime", "python", "-c", "import sys; print(sys.version.split()[0])"],
            cwd=root,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        python_check.update({"path": "uv run --project .ai/runtime python", "ok": True, "version": output})
    except Exception as exc:
        python_check["error"] = str(exc)
checks["python"] = python_check

powershell = command_version("pwsh", "--version")
if not powershell["ok"]:
    powershell = command_version("powershell", "-Version")
powershell["required"] = False
checks["powershell"] = powershell

required = ("bash", "git", "make", "uv", "python")
ok = all(checks[name]["ok"] for name in required)
payload = {
    "ok": ok,
    "required": list(required),
    "optional": ["powershell"],
    "checks": checks,
}
print(json.dumps(payload, indent=2, sort_keys=True))
if not ok:
    raise SystemExit(1)
PY
