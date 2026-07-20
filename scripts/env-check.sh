#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer an already-installed project runtime. This avoids making a health
# check trigger dependency resolution or installation through `uv run`.
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON="$PYTHON"
elif [[ -x "$ROOT/.ai/runtime/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.ai/runtime/.venv/bin/python"
elif [[ -x "$ROOT/.ai/runtime/.venv/Scripts/python.exe" ]]; then
  PYTHON="$ROOT/.ai/runtime/.venv/Scripts/python.exe"
else
  PYTHON="$(command -v python3 || command -v python || true)"
fi
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
installed_python = next(
    (
        path
        for path in (
            root / ".ai" / "runtime" / ".venv" / "bin" / "python",
            root / ".ai" / "runtime" / ".venv" / "Scripts" / "python.exe",
        )
        if path.exists()
    ),
    None,
)
command = None
path_label = None
if installed_python is not None:
    command = [str(installed_python), "-c", "import sys; print(sys.version.split()[0])"]
    path_label = installed_python.relative_to(root).as_posix()
elif uv_path:
    command = [uv_path, "run", "--project", ".ai/runtime", "python", "-c", "import sys; print(sys.version.split()[0])"]
    path_label = "uv run --project .ai/runtime python"
if command:
    try:
        output = subprocess.check_output(
            command,
            cwd=root,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        python_check.update({"path": path_label, "ok": True, "version": output})
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
