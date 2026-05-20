#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit/evolution"
EVENT_FILE="$STATE_DIR/events.jsonl"

event_type="candidate"
source="manual"
candidate=""
signal=""
note=""
tokens=""
confidence=""
risk=""
tags=()

usage() {
  cat <<'USAGE'
Usage: ./scripts/evolve-capture.sh [options]

Capture one local evolution event as sanitized JSONL.

Options:
  --event name          Event type, default: candidate
  --source name         Source of the event, default: manual
  --candidate text      Candidate name or short description
  --signal text         Signal such as pass, fail, repeated, verified, risky
  --note text           Short supporting note; secrets are redacted
  --tokens number       Optional observed or estimated token impact
  --confidence 0..1     Optional confidence hint
  --risk 0..1           Optional risk hint
  --tag text            Repeatable tag
  --state-dir path      Override state directory for tests
  --self-test           Run local capture redaction self-test
  -h, --help            Show this help
USAGE
}

while (($#)); do
  case "$1" in
    --event)
      shift; [[ $# -gt 0 ]] || { echo "--event requires a value" >&2; exit 2; }
      event_type="$1"
      ;;
    --source)
      shift; [[ $# -gt 0 ]] || { echo "--source requires a value" >&2; exit 2; }
      source="$1"
      ;;
    --candidate)
      shift; [[ $# -gt 0 ]] || { echo "--candidate requires a value" >&2; exit 2; }
      candidate="$1"
      ;;
    --signal)
      shift; [[ $# -gt 0 ]] || { echo "--signal requires a value" >&2; exit 2; }
      signal="$1"
      ;;
    --note)
      shift; [[ $# -gt 0 ]] || { echo "--note requires a value" >&2; exit 2; }
      note="$1"
      ;;
    --tokens)
      shift; [[ $# -gt 0 ]] || { echo "--tokens requires a value" >&2; exit 2; }
      tokens="$1"
      ;;
    --confidence)
      shift; [[ $# -gt 0 ]] || { echo "--confidence requires a value" >&2; exit 2; }
      confidence="$1"
      ;;
    --risk)
      shift; [[ $# -gt 0 ]] || { echo "--risk requires a value" >&2; exit 2; }
      risk="$1"
      ;;
    --tag)
      shift; [[ $# -gt 0 ]] || { echo "--tag requires a value" >&2; exit 2; }
      tags+=("$1")
      ;;
    --state-dir)
      shift; [[ $# -gt 0 ]] || { echo "--state-dir requires a value" >&2; exit 2; }
      STATE_DIR="$1"
      EVENT_FILE="$STATE_DIR/events.jsonl"
      ;;
    --self-test)
      tmp_state="$(mktemp -d)"
      trap 'rm -rf "$tmp_state"' EXIT
      "$0" --state-dir "$tmp_state" \
        --candidate "secret redaction" \
        --signal "verified" \
        --note "password=abc123 token: sk-test-value Authorization: Bearer testvalue" \
        --tokens 42 \
        --confidence 0.9 \
        --risk 0.1 >/dev/null
      python3 - "$tmp_state/events.jsonl" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
line = path.read_text(encoding="utf-8").strip()
event = json.loads(line)
text = json.dumps(event, sort_keys=True)
for raw in ("abc123", "sk-test-value", "testvalue"):
    if raw in text:
        raise SystemExit(f"secret was not redacted: {raw}")
if "[REDACTED]" not in text:
    raise SystemExit("redaction marker missing")
PY
      echo "evolve-capture self-test ok"
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -z "$candidate$signal$note" ]]; then
  echo "at least one of --candidate, --signal, or --note is required" >&2
  exit 2
fi

umask 077
mkdir -p "$STATE_DIR"

if ((${#tags[@]})); then
  tags_json="$(printf '%s\n' "${tags[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')"
else
  tags_json="[]"
fi

EVENT_TYPE="$event_type" \
SOURCE="$source" \
CANDIDATE="$candidate" \
SIGNAL="$signal" \
NOTE="$note" \
TOKENS="$tokens" \
CONFIDENCE="$confidence" \
RISK="$risk" \
TAGS_JSON="$tags_json" \
EVENT_FILE="$EVENT_FILE" \
python3 - <<'PY'
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)\b(password|passwd|token|api[_-]?key|secret|credential)\b\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{6,})\b"),
]

def redact(value):
    if value is None:
        return ""
    text = str(value)
    for pattern in SECRET_PATTERNS:
        def repl(match):
            if match.lastindex and match.lastindex >= 1:
                return match.group(1) + "[REDACTED]"
            return "[REDACTED]"
        text = pattern.sub(repl, text)
    return text

def number_or_none(value, as_int=False):
    if value == "":
        return None
    try:
        numeric = int(value) if as_int else float(value)
    except ValueError as exc:
        raise SystemExit(f"invalid numeric value: {value}") from exc
    if not as_int and not 0 <= numeric <= 1:
        raise SystemExit(f"expected 0..1 numeric value: {value}")
    return numeric

event = {
    "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "event": redact(os.environ["EVENT_TYPE"]) or "candidate",
    "source": redact(os.environ["SOURCE"]) or "manual",
    "candidate": redact(os.environ["CANDIDATE"]),
    "signal": redact(os.environ["SIGNAL"]),
    "note": redact(os.environ["NOTE"]),
    "token_estimate": number_or_none(os.environ["TOKENS"], as_int=True),
    "confidence_hint": number_or_none(os.environ["CONFIDENCE"]),
    "risk_hint": number_or_none(os.environ["RISK"]),
    "tags": [redact(tag) for tag in json.loads(os.environ["TAGS_JSON"])],
}

path = Path(os.environ["EVENT_FILE"])
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
print(path)
PY
