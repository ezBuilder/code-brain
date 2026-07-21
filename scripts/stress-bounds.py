#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

# Set deliberately tight, production-supported policies before importing modules
# whose bounded constants are initialized from the environment.
os.environ.setdefault("AI_SANDBOX_META_MAX_BYTES", "16000")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_FILES", "32")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_CANDIDATES", "64")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_BYTES", "262144")
os.environ.setdefault("AI_SANDBOX_DIAGNOSTICS_MAX_SECONDS", "2")
os.environ.setdefault("AI_TRANSCRIPT_MAX_FILE_BYTES", "1000000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_LINE_BYTES", "64000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SCAN_BYTES", "4000000")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SESSIONS", "32")
os.environ.setdefault("AI_TRANSCRIPT_MAX_CANDIDATES", "64")
os.environ.setdefault("AI_TRANSCRIPT_MAX_SCAN_SECONDS", "3")
os.environ.setdefault("AI_TRANSCRIPT_MAX_DEDUPE_KEYS", "100")

from ai_core import sandbox  # noqa: E402
from ai_core.transcripts import claude_usage_summary, codex_usage_summary  # noqa: E402


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _sandbox_fixture(root: Path, files: int) -> None:
    target = root / ".ai" / "cache" / "sandbox"
    target.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        exec_id = f"{index:016x}"
        killed = index == files - 1
        _write_json(
            target / f"{exec_id}.meta.json",
            {
                "exec_id": exec_id,
                "created_at": f"2026-07-21T00:{index % 60:02d}:00Z",
                "command_ok": not killed,
                "exit_code": -9 if killed else 0,
                "peak_rss_kib": 2048 + index,
                "source_total_bytes": 128,
                "termination": {
                    "classification": "external_sigkill_or_execution_limit" if killed else "exit_code",
                    "signal": "SIGKILL" if killed else None,
                },
            },
        )


def _transcript_fixtures(root: Path, claude_home: Path, codex_home: Path, files: int) -> None:
    claude_project = claude_home / "projects" / "stress-project"
    codex_sessions = codex_home / "sessions" / "2026" / "07" / "21"
    claude_project.mkdir(parents=True, exist_ok=True)
    codex_sessions.mkdir(parents=True, exist_ok=True)
    root_text = str(root)
    for index in range(files):
        stamp = f"2026-07-21T00:{index % 60:02d}:00Z"
        claude = {
            "sessionId": f"claude-{index}",
            "requestId": f"request-{index}",
            "cwd": root_text,
            "timestamp": stamp,
            "message": {
                "id": f"message-{index}",
                "model": "stress",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        (claude_project / f"session-{index:08d}.jsonl").write_text(
            json.dumps(claude, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        codex_records = [
            {
                "timestamp": stamp,
                "type": "session_meta",
                "payload": {"id": f"codex-{index}", "cwd": root_text, "model_provider": "stress"},
            },
            {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "cached_input_tokens": 0,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 2,
                        }
                    },
                },
            },
        ]
        (codex_sessions / f"rollout-{index:08d}.jsonl").write_text(
            "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in codex_records),
            encoding="utf-8",
        )


def _assert_scan_bounds(name: str, payload: dict[str, Any], files: int) -> dict[str, Any]:
    scan = payload.get("scan")
    if not isinstance(scan, dict):
        raise AssertionError(f"{name}: missing scan payload")
    policy = scan.get("policy")
    if not isinstance(policy, dict):
        raise AssertionError(f"{name}: missing scan policy")
    max_sessions = int(policy["max_sessions"])
    max_candidates = int(policy["max_candidates"])
    max_bytes = int(policy["max_scan_bytes"])
    max_seconds = float(policy["max_scan_seconds"])
    discovered = int(scan.get("sessions_discovered") or 0)
    scanned = int(scan.get("sessions_scanned") or 0)
    bytes_scanned = int(scan.get("bytes_scanned") or 0)
    elapsed_ms = int(scan.get("elapsed_ms") or 0)
    if discovered != files:
        raise AssertionError(f"{name}: discovered={discovered}, expected={files}")
    if scanned > max_sessions:
        raise AssertionError(f"{name}: scanned={scanned} exceeds max_sessions={max_sessions}")
    if bytes_scanned > max_bytes:
        raise AssertionError(f"{name}: bytes_scanned={bytes_scanned} exceeds max_scan_bytes={max_bytes}")
    if elapsed_ms > int((max_seconds + 2.0) * 1000):
        raise AssertionError(f"{name}: elapsed_ms={elapsed_ms} exceeds bounded allowance")
    if files > max_candidates and int(scan.get("skip_counts", {}).get("candidate_limit", 0)) == 0:
        raise AssertionError(f"{name}: candidate limit was not reported")
    return {
        "complete": scan.get("complete"),
        "discovered": discovered,
        "scanned": scanned,
        "bytes_scanned": bytes_scanned,
        "elapsed_ms": elapsed_ms,
        "candidate_limit_skips": int(scan.get("skip_counts", {}).get("candidate_limit", 0)),
        "policy": policy,
    }


def run(files: int, iterations: int, max_peak_mib: int) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="code-brain-stress-bounds-") as temp:
        base = Path(temp)
        project = base / "project"
        claude_home = base / "claude"
        codex_home = base / "codex"
        project.mkdir()
        _sandbox_fixture(project, files)
        _transcript_fixtures(project, claude_home, codex_home, files)

        tracemalloc.start()
        sandbox_payload: dict[str, Any] = {}
        claude_payload: dict[str, Any] = {}
        codex_payload: dict[str, Any] = {}
        for _ in range(iterations):
            sandbox_payload = sandbox.execution_diagnostics(project)
            claude_payload = claude_usage_summary(project, home=claude_home)
            codex_payload = codex_usage_summary(project, home=codex_home)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        sandbox_policy = sandbox_payload.get("policy", {})
        if sandbox_payload.get("bounded") is not True:
            raise AssertionError("sandbox diagnostics did not report bounded=true")
        if int(sandbox_payload.get("metas_discovered") or 0) != files:
            raise AssertionError("sandbox metadata discovery count mismatch")
        if int(sandbox_payload.get("metas_scanned") or 0) > int(sandbox_policy["max_files"]):
            raise AssertionError("sandbox metadata file limit exceeded")
        if int(sandbox_payload.get("bytes_scanned") or 0) > int(sandbox_policy["max_scan_bytes"]):
            raise AssertionError("sandbox metadata byte limit exceeded")
        if files > int(sandbox_policy["max_candidates"]) and int(
            sandbox_payload.get("skip_counts", {}).get("candidate_limit", 0)
        ) == 0:
            raise AssertionError("sandbox candidate limit was not reported")
        if int(sandbox_payload.get("killed_9", {}).get("sigkill_total", 0)) != 1:
            raise AssertionError("sandbox SIGKILL evidence was not preserved")

        peak_mib = round(peak / (1024 * 1024), 3)
        if peak_mib > max_peak_mib:
            raise AssertionError(f"traced peak {peak_mib} MiB exceeds {max_peak_mib} MiB")

        return {
            "ok": True,
            "files_per_store": files,
            "iterations": iterations,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "tracemalloc_peak_mib": peak_mib,
            "sandbox": {
                "complete": sandbox_payload.get("complete"),
                "metas_discovered": sandbox_payload.get("metas_discovered"),
                "metas_scanned": sandbox_payload.get("metas_scanned"),
                "bytes_scanned": sandbox_payload.get("bytes_scanned"),
                "candidate_limit_skips": sandbox_payload.get("skip_counts", {}).get("candidate_limit", 0),
                "killed_9": sandbox_payload.get("killed_9"),
                "policy": sandbox_policy,
            },
            "claude": _assert_scan_bounds("claude", claude_payload, files),
            "codex": _assert_scan_bounds("codex", codex_payload, files),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stress bounded sandbox and transcript diagnostics without touching the repository."
    )
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--max-peak-mib", type=int, default=64)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.files < 65:
        parser.error("--files must be at least 65 so candidate truncation is exercised")
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.max_peak_mib < 1:
        parser.error("--max-peak-mib must be positive")
    try:
        payload = run(args.files, args.iterations, args.max_peak_mib)
    except (AssertionError, OSError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"stress-bounds failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "stress-bounds ok "
            f"files={payload['files_per_store']} iterations={payload['iterations']} "
            f"peak_mib={payload['tracemalloc_peak_mib']} elapsed_ms={payload['elapsed_ms']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
