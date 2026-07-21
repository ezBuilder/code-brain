#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
py() {
  if [[ -x "$ROOT/.ai/runtime/.venv/bin/python" ]]; then
    "$ROOT/.ai/runtime/.venv/bin/python" "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run --project "$ROOT/.ai/runtime" python "$@"
  else
    local _py
    _py="$(command -v python3 || command -v python || true)"
    if [[ -z "$_py" ]]; then
      echo "dep-advisory failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}
mkdir -p dist

OUT="dist/dep-advisory.json"
RAW_OUTPUT="$(mktemp)"
trap 'rm -f "$RAW_OUTPUT"' EXIT

if [[ -n "${CODE_BRAIN_DEP_ADVISORY_RAW:-}" ]]; then
  printf '%s\n' "$CODE_BRAIN_DEP_ADVISORY_RAW" >"$RAW_OUTPUT"
elif [[ "${CODE_BRAIN_DEP_ADVISORY_OFFLINE:-}" == "1" ]]; then
  py - "$OUT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "ok": True,
    "skipped": "offline",
    "findings": [],
    "finding_count": 0,
    "tool": "pip-audit",
    "mode": "advisory",
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("dep-advisory ok (offline-skipped)")
PY
  exit 0
else
  if ! command -v uv >/dev/null 2>&1; then
    py - "$OUT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "ok": True,
    "skipped": "tool-unavailable",
    "findings": [],
    "finding_count": 0,
    "tool": "pip-audit",
    "mode": "advisory",
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("dep-advisory ok (tool-unavailable)")
PY
    exit 0
  fi
  set +e
  uv run --with pip-audit pip-audit .ai/runtime -f json --progress-spinner off --desc off --aliases off >"$RAW_OUTPUT" 2>/dev/null
  AUDIT_STATUS="$?"
  set -e
  if [[ "$AUDIT_STATUS" -gt 1 ]]; then
    py - "$OUT" "$AUDIT_STATUS" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "ok": True,
    "skipped": "offline",
    "findings": [],
    "finding_count": 0,
    "tool": "pip-audit",
    "mode": "advisory",
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "tool_exit_code": int(sys.argv[2]),
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("dep-advisory ok (offline-skipped)")
PY
    exit 0
  fi
fi

py - "$OUT" "$RAW_OUTPUT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def truncate(value: object, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def load_json_fragment(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("pip-audit JSON payload not found")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("pip-audit JSON payload is not an object")
    return payload


def normalize(raw: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for dep in raw.get("dependencies", []):
        if not isinstance(dep, dict):
            continue
        package = dep.get("name")
        version = dep.get("version")
        for vuln in dep.get("vulns", []):
            if not isinstance(vuln, dict):
                continue
            finding_id = vuln.get("id") or vuln.get("name")
            findings.append(
                {
                    "package": package,
                    "version": version,
                    "id": finding_id,
                    "fix_versions": vuln.get("fix_versions") or vuln.get("fixes") or [],
                    "description": truncate(vuln.get("description")),
                }
            )
    return findings


out_path = Path(sys.argv[1])
raw_text = Path(sys.argv[2]).read_text(encoding="utf-8")
try:
    raw = load_json_fragment(raw_text)
    findings = normalize(raw)
    payload = {
        "ok": True,
        "skipped": None,
        "findings": findings,
        "finding_count": len(findings),
        "tool": "pip-audit",
        "mode": "advisory",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
except Exception as exc:
    payload = {
        "ok": True,
        "skipped": "parse-error",
        "findings": [],
        "finding_count": 0,
        "tool": "pip-audit",
        "mode": "advisory",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "error": truncate(exc),
    }

out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"dep-advisory ok ({payload['finding_count']} findings)")
PY
