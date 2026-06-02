#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer uv-managed Python. On Windows, Git Bash can see Microsoft Store
# python/python3 aliases before a real interpreter; uv avoids that trap.
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_CMD=("$PYTHON")
elif [[ -x "$ROOT/.ai/runtime/.venv/Scripts/python.exe" ]]; then
  PYTHON_CMD=("$ROOT/.ai/runtime/.venv/Scripts/python.exe")
elif command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --project .ai/runtime python)
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=("$(command -v python3)")
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD=("$(command -v python)")
else
  echo "preflight failed: uv/python3/python interpreter not found on PATH" >&2
  exit 2
fi

CHECK_ONLY=false
JSON=false
for arg in "$@"; do
  case "$arg" in
    --check-only) CHECK_ONLY=true ;;
    --json) JSON=true ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

"${PYTHON_CMD[@]}" - "$ROOT" "$CHECK_ONLY" "$JSON" <<'PY'
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
check_only = sys.argv[2] == "true"
as_json = sys.argv[3] == "true"


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def command_version(command: str, *args: str, required: bool = True) -> dict[str, object]:
    path = shutil.which(command)
    result: dict[str, object] = {
        "command": command,
        "required": required,
        "path": path,
        "ok": bool(path) or not required,
        "version": None,
    }
    if not path:
        return result
    try:
        output = subprocess.check_output([path, *args], cwd=root, env=command_env(), text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        result.update({"ok": False, "error": str(exc)})
        return result
    result["version"] = output.splitlines()[0] if output else ""
    return result


def installed_venv_python() -> Path | None:
    candidates = [
        root / ".ai" / "runtime" / ".venv" / "Scripts" / "python.exe",
        root / ".ai" / "runtime" / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def uv_check() -> dict[str, object]:
    result = command_version("uv", "--version")
    if result.get("ok"):
        return result
    python_path = installed_venv_python()
    if python_path:
        return {
            "command": "uv",
            "required": True,
            "path": None,
            "ok": True,
            "version": None,
            "detail": f"uv not on PATH; installed runtime Python available at {python_path.relative_to(root).as_posix()}",
        }
    return result


def python_check() -> dict[str, object]:
    uv = shutil.which("uv")
    python_path = installed_venv_python()
    result: dict[str, object] = {
        "command": "uv python",
        "required": True,
        "path": "uv run --project .ai/runtime python" if uv else (str(python_path.relative_to(root).as_posix()) if python_path else None),
        "ok": False,
        "version": None,
        "minimum": "3.11",
    }
    command = [uv, "run", "--project", ".ai/runtime", "python", "-c", "import sys; print(sys.version.split()[0])"] if uv else None
    if not command and python_path:
        command = [str(python_path), "-c", "import sys; print(sys.version.split()[0])"]
        result["command"] = "installed runtime python"
    if not command:
        result["error"] = "uv or installed runtime Python is required before Python runtime can be resolved"
        return result
    try:
        output = subprocess.check_output(
            command,
            cwd=root,
            env=command_env(),
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except Exception as exc:
        result["error"] = str(exc)
        return result
    version = output.splitlines()[-1] if output else ""
    major, minor, *_ = (int(part) for part in version.split("."))
    result.update({"version": version, "ok": (major, minor) >= (3, 11)})
    if not result["ok"]:
        result["error"] = "Python 3.11 or newer is required"
    return result


def mode_check(path: Path, *, max_public_bits: int) -> dict[str, object]:
    if not path.exists():
        return {"path": path.relative_to(root).as_posix(), "exists": False, "ok": True}
    if os.name == "nt":
        return {
            "path": path.relative_to(root).as_posix(),
            "exists": True,
            "ok": True,
            "detail": "skipped on Windows",
        }
    mode = stat.S_IMODE(path.stat().st_mode)
    public_bits = mode & max_public_bits
    return {
        "path": path.relative_to(root).as_posix(),
        "exists": True,
        "mode": oct(mode),
        "ok": public_bits == 0,
        "detail": "ok" if public_bits == 0 else "group/other permissions are set",
    }


encrypted_secrets = sorted((root / ".ai" / "secrets").glob("*.enc.y*ml"))
gitattributes = "\n".join(
    path.read_text(encoding="utf-8", errors="ignore")
    for path in (root / ".gitattributes", root / ".ai" / ".gitattributes")
    if path.exists()
)
requires_lfs = "filter=lfs" in gitattributes

checks = {
    "repo_layout": {
        "ok": (
            (root / ".ai" / "runtime" / "pyproject.toml").exists()
            and (
                (root / "bootstrap.sh").exists()
                or (root / "bootstrap-code-brain.sh").exists()
                or (os.name == "nt" and (root / ".ai" / "bin" / "ai.ps1").exists())
            )
        ),
        "required": True,
        "detail": "ok",
    },
    "bash": command_version("bash", "--version"),
    "git": command_version("git", "--version"),
    "make": command_version("make", "--version", required=(os.name != "nt")),
    "uv": uv_check(),
    "python": python_check(),
    "sops": command_version("sops", "--version", required=bool(encrypted_secrets)),
    "age": command_version("age", "--version", required=bool(encrypted_secrets)),
    "git_lfs": command_version("git-lfs", "--version", required=requires_lfs),
    "cache_permissions": mode_check(root / ".ai" / "cache", max_public_bits=0o077),
}

warnings: list[str] = []
if os.environ.get("UV_OFFLINE") and not (root / ".ai" / "runtime" / ".venv").exists():
    warnings.append("UV_OFFLINE is set but .ai/runtime/.venv is absent")
if encrypted_secrets and (not checks["sops"]["ok"] or not checks["age"]["ok"]):
    warnings.append("encrypted secrets require both sops and age before secrets operations can work")
if not check_only:
    warnings.append("preflight currently performs checks only; bootstrap performs runtime sync/render")

ok = all(bool(check.get("ok")) for check in checks.values())
payload = {
    "ok": ok,
    "check_only": check_only,
    "warnings": warnings,
    "checks": checks,
}
if as_json:
    print(json.dumps(payload, indent=2, sort_keys=True))
else:
    print("preflight ok" if ok else "preflight failed")
    for name, check in checks.items():
        status = "ok" if check.get("ok") else "fail"
        required = "required" if check.get("required", True) else "optional"
        print(f"- {name}: {status} ({required})")
    for warning in warnings:
        print(f"warning: {warning}")
raise SystemExit(0 if ok else 1)
PY
