from __future__ import annotations

import json
import platform
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .memory import all_audit_files
from .redact import redact_value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_path(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return root / ".ai" / "cache" / "logs" / f"{stamp}.jsonl"


def write_log(root: Path, level: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": now_iso(),
        "level": level,
        "event": event,
        "payload": redact_value(payload),
    }
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a", encoding="utf-8").write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "path": path.relative_to(root).as_posix(), "record": record}


def metrics(root: Path) -> dict[str, Any]:
    from .worker.scheduler import queue_age_stats, recovery_status
    from .search import observability as search_observability
    from .transcripts import claude_usage_summary, codex_usage_summary

    queue_root = root / ".ai" / "memory" / "queue"
    recovery = recovery_status(root)
    ages = queue_age_stats(root)
    search = search_observability(root)
    return {
        "ok": True,
        "runtime_version": __version__,
        "queue": {
            "pending": len(list(queue_root.glob("*.json"))),
            "processing": len(list((queue_root / "processing").glob("*.json"))),
            "dead": len(list((queue_root / "dead").glob("*.json"))),
            "expired_processing": recovery["expired_processing"],
            "recovery_lag_seconds": recovery["lag_seconds"],
            "last_recovered": recovery.get("last_recovered", 0),
            "last_dead_lettered": recovery.get("last_dead_lettered", 0),
            **ages,
        },
        "cache": {
            "code_sqlite_exists": (root / ".ai" / "cache" / "code.sqlite").exists(),
            "code_sqlite_bytes": search.get("sqlite_bytes", 0),
            "indexed_files": search.get("indexed_files", 0),
            "indexed_bytes": search.get("indexed_bytes", 0),
        },
        "search": search,
        "usage": {
            "claude": _usage_totals_only(claude_usage_summary(root)),
            "codex": codex_usage_summary(root),
        },
    }


def search_report(root: Path, *, query_text: str | None = None, limit: int = 5) -> dict[str, Any]:
    from .doctor import as_payload, run_checks
    from .search import observability as search_observability

    doctor = as_payload(run_checks(root))
    freshness = next((check for check in doctor["checks"] if check["name"] == "index_freshness"), None)
    payload = search_observability(root, query_text=query_text, limit=limit)
    payload["doctor"] = {
        "ok": doctor["ok"],
        "index_freshness": freshness,
    }
    query = payload.get("query")
    if isinstance(query, dict):
        stale = query.get("stale_results") or []
        if stale:
            query["remediation"] = {
                "command": "ai index rebuild --json",
                "alternative": "ai obs search --refresh-stale --query <text>",
                "stale_count": len(stale),
                "exit_code": 13,
            }
    return payload


def usage_report(root: Path, *, include_sessions: bool = False) -> dict[str, Any]:
    from .transcripts import claude_usage_summary, codex_usage_summary

    events = event_observability(root)
    claude = claude_usage_summary(root)
    codex = codex_usage_summary(root)
    if not include_sessions:
        claude = _usage_totals_only(claude)
        codex = _usage_totals_only(codex)
    return {
        "ok": True,
        "actual_token_usage": {
            "claude": claude,
            "codex": codex,
        },
        "measured_code_brain_effect": events,
        "claims": {
            "token_usage": "actual only when sourced from agent session transcripts",
            "context_reduction": "measured in bytes only; no token-saving estimate is emitted",
        },
    }


def event_observability(root: Path) -> dict[str, Any]:
    events_root = root / ".ai" / "memory" / "events"
    totals = {
        "events": 0,
        "hook_events": 0,
        "mcp_requests": 0,
        "additional_context_bytes": 0,
        "original_context_bytes": 0,
        "delta_skipped_count": 0,
        "delta_saved_bytes": 0,
        "mcp_request_bytes": 0,
        "mcp_response_bytes": 0,
        "hook_breakdown": {},
        "mcp_breakdown": {},
        "mcp_tool_breakdown": {},
        "sandbox_executions": 0,
        "pretooluse_blocks": 0,
        "pretooluse_allows": 0,
    }
    if not events_root.exists():
        return {"ok": True, **totals}
    HOOK_NAMES = {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "SubagentStop", "PreCompact", "PostCompact",
        "SessionEnd", "Notification", "PermissionRequest", "PermissionDenied",
        "CwdChanged", "ConfigChange", "InstructionsLoaded",
    }
    hook_breakdown: dict[str, dict[str, int]] = {}
    mcp_breakdown: dict[str, dict[str, int]] = {}
    mcp_tool_breakdown: dict[str, dict[str, int]] = {}
    for path in sorted(events_root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            kind = str(record.get("kind") or payload.get("hook") or payload.get("kind") or "")
            totals["events"] += 1
            ctx_bytes = _int(payload.get("additional_context_bytes"))
            orig_bytes = _int(payload.get("original_context_bytes")) or ctx_bytes
            delta_skipped = bool(payload.get("delta_skipped"))
            req_bytes = _int(payload.get("request_bytes"))
            resp_bytes = _int(payload.get("response_bytes"))
            totals["additional_context_bytes"] += ctx_bytes
            totals["original_context_bytes"] += orig_bytes
            if delta_skipped:
                totals["delta_skipped_count"] += 1
                totals["delta_saved_bytes"] += max(0, orig_bytes - ctx_bytes)
            totals["mcp_request_bytes"] += req_bytes
            totals["mcp_response_bytes"] += resp_bytes
            if kind == "mcp.request":
                totals["mcp_requests"] += 1
                method = str(payload.get("method") or "unknown")
                bucket = mcp_breakdown.setdefault(
                    method, {"count": 0, "request_bytes": 0, "response_bytes": 0}
                )
                bucket["count"] += 1
                bucket["request_bytes"] += req_bytes
                bucket["response_bytes"] += resp_bytes
                tool_name = payload.get("tool_name")
                if method == "tools/call" and isinstance(tool_name, str) and tool_name:
                    tool_bucket = mcp_tool_breakdown.setdefault(
                        tool_name, {"count": 0, "request_bytes": 0, "response_bytes": 0}
                    )
                    tool_bucket["count"] += 1
                    tool_bucket["request_bytes"] += req_bytes
                    tool_bucket["response_bytes"] += resp_bytes
            elif kind == "sandbox.execute":
                totals["sandbox_executions"] += 1
            elif kind in HOOK_NAMES:
                totals["hook_events"] += 1
                bucket = hook_breakdown.setdefault(
                    kind, {"count": 0, "bytes_total": 0, "blocked": 0, "allowed": 0}
                )
                bucket["count"] += 1
                bucket["bytes_total"] += ctx_bytes
                if kind == "PreToolUse":
                    precall = payload.get("precall") if isinstance(payload.get("precall"), dict) else None
                    action = (precall or {}).get("action")
                    decision = payload.get("decision") or (payload.get("response") or {}).get("decision")
                    if action == "block" or decision == "block":
                        bucket["blocked"] += 1
                        totals["pretooluse_blocks"] += 1
                    elif action == "allow":
                        bucket["allowed"] += 1
                        totals["pretooluse_allows"] += 1
                    elif action == "observe":
                        bucket.setdefault("observed", 0)
                        bucket["observed"] += 1
    totals["hook_breakdown"] = {k: hook_breakdown[k] for k in sorted(hook_breakdown)}
    totals["mcp_breakdown"] = {k: mcp_breakdown[k] for k in sorted(mcp_breakdown)}
    totals["mcp_tool_breakdown"] = {k: mcp_tool_breakdown[k] for k in sorted(mcp_tool_breakdown)}
    return {"ok": True, **totals}


def _usage_totals_only(payload: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "ok": payload.get("ok"),
        "source": payload.get("source"),
        "sessions_scanned": payload.get("sessions_scanned", 0),
        "sessions_matched": payload.get("sessions_matched", 0),
        "messages": payload.get("messages", 0),
        "tokens": payload.get("tokens", {}),
        "total_observed_tokens": payload.get("total_observed_tokens", 0),
    }
    for key in ("user_messages", "agent_messages", "turns"):
        if key in payload:
            compact[key] = payload.get(key, 0)
    return compact


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def health_summary(root: Path) -> dict[str, Any]:
    from .worker.lock import lock_status
    from .worker.scheduler import QUEUE_PENDING_AGE_STALE_SECONDS, QUEUE_PROCESSING_AGE_STALE_SECONDS, status as queue_status

    doctor = as_payload(run_checks(root))
    failed_checks = [
        {"name": check["name"], "detail": check.get("detail", "")}
        for check in doctor.get("checks", [])
        if not check.get("ok")
    ]
    worker = lock_status(root)
    queue = queue_status(root)
    pending_age = int(queue.get("oldest_pending_age_seconds", 0) or 0)
    processing_age = int(queue.get("oldest_processing_age_seconds", 0) or 0)
    payload = {
        "ok": bool(
            doctor.get("ok")
            and not worker.get("stale")
            and not worker.get("cross_host")
            and pending_age <= QUEUE_PENDING_AGE_STALE_SECONDS
            and processing_age <= QUEUE_PROCESSING_AGE_STALE_SECONDS
        ),
        "doctor": {"ok": bool(doctor.get("ok")), "failed": failed_checks},
        "worker": {
            "locked": bool(worker.get("locked")),
            "stale": bool(worker.get("stale")),
            "cross_host": bool(worker.get("cross_host")),
            "reason": worker.get("reason"),
            "pid": worker.get("pid"),
        },
        "queue": {
            "pending": int(queue.get("pending", 0) or 0),
            "processing": int(queue.get("processing", 0) or 0),
            "dead": int(queue.get("dead", 0) or 0),
            "expired_processing": int(queue.get("expired_processing", 0) or 0),
            "oldest_pending_age_seconds": pending_age,
            "oldest_processing_age_seconds": processing_age,
            "age_stats_skipped": int(queue.get("age_stats_skipped", 0) or 0),
        },
        "release_artifacts": release_artifact_summary(root),
        "index": index_summary(root),
        "surfacing": _surfacing_summary(root),
    }
    return redact_value(payload)


def _surfacing_summary(root: Path) -> dict[str, Any]:
    """Cumulative recommendation surfacing KPIs from audit log + cache freshness."""
    import json as _json
    import time
    from datetime import datetime, timedelta, timezone

    audit_files = all_audit_files(root)
    counts = {"surfaced": 0, "accepted": 0, "rejected": 0}
    now_dt = datetime.now(timezone.utc)
    stale_cutoff = now_dt - timedelta(days=7)
    resurface_window = timedelta(days=7)
    acted_ids: set[str] = set()
    surfaced_records: list[tuple[datetime, str]] = []
    # Per-id chronological event log for resurface-after-reject computation.
    # Stores (ts, kind) where kind in {"reject", "recommend_pending"}.
    id_events: dict[str, list[tuple[datetime, str]]] = {}
    last_act_dt: datetime | None = None
    for audit_file in audit_files:
        try:
            content = audit_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            act = str(rec.get("action") or "")
            if not act.startswith(("skill.", "agent.", "precall.")):
                continue
            tail = act.split(".", 1)[1]
            pid = (rec.get("payload") or {}).get("id") if isinstance(rec.get("payload"), dict) else None
            ts_raw = str(rec.get("ts") or "")
            parsed_ts: datetime | None = None
            if ts_raw:
                try:
                    parsed_ts = (
                        datetime.fromisoformat(ts_raw[:-1]).replace(tzinfo=timezone.utc)
                        if ts_raw.endswith("Z") else datetime.fromisoformat(ts_raw)
                    )
                except ValueError:
                    parsed_ts = None
            if tail == "recommend_pending":
                counts["surfaced"] += 1
                if parsed_ts is not None and isinstance(pid, str):
                    surfaced_records.append((parsed_ts, pid))
                    id_events.setdefault(pid, []).append((parsed_ts, "recommend_pending"))
            elif tail.startswith("accept"):
                counts["accepted"] += 1
                if isinstance(pid, str):
                    acted_ids.add(pid)
                if parsed_ts is not None and (last_act_dt is None or parsed_ts > last_act_dt):
                    last_act_dt = parsed_ts
            elif tail == "reject":
                counts["rejected"] += 1
                if isinstance(pid, str):
                    acted_ids.add(pid)
                    if parsed_ts is not None:
                        id_events.setdefault(pid, []).append((parsed_ts, "reject"))
                if parsed_ts is not None and (last_act_dt is None or parsed_ts > last_act_dt):
                    last_act_dt = parsed_ts
    # resurface_after_reject: for each reject (with parsed ts), check if any
    # subsequent recommend_pending for the same id occurs within 7 days.
    total_rejected_with_ts = 0
    resurfaced_count = 0
    for pid, events in id_events.items():
        events_sorted = sorted(events, key=lambda x: x[0])
        for idx, (ts_evt, kind) in enumerate(events_sorted):
            if kind != "reject":
                continue
            total_rejected_with_ts += 1
            for ts_next, kind_next in events_sorted[idx + 1:]:
                if kind_next == "recommend_pending" and (ts_next - ts_evt) < resurface_window:
                    resurfaced_count += 1
                    break
    if total_rejected_with_ts:
        resurface_after_reject_rate: float | None = round(
            resurfaced_count / total_rejected_with_ts, 3
        )
    else:
        resurface_after_reject_rate = None
    total_acted = counts["accepted"] + counts["rejected"]
    accept_ratio = round(counts["accepted"] / total_acted, 3) if total_acted else None
    caches = {}
    for name in ("skill_hot", "agent_hot", "precall_hot", "bash_heads"):
        p = root / ".ai" / "cache" / f"{name}.json"
        if p.exists():
            try:
                caches[name] = int(time.time() - p.stat().st_mtime)
            except OSError:
                caches[name] = None
        else:
            caches[name] = None
    # adaptive_bump: current min_signal raise above base 3
    try:
        from .hooks import _adaptive_min_signal_from_satisfaction
        adaptive_bump = _adaptive_min_signal_from_satisfaction(root, 3) - 3
    except Exception:
        adaptive_bump = 0
    # last_act_age_seconds: seconds since most recent accept|reject; None if none
    if last_act_dt is None:
        last_act_age_seconds: int | None = None
    else:
        last_act_age_seconds = max(0, int((now_dt - last_act_dt).total_seconds()))
    # stale_count_7d: surfaced candidates never acted on AND surfaced >7d ago
    stale_count_7d = 0
    for ts, pid in surfaced_records:
        if pid not in acted_ids and ts < stale_cutoff:
            stale_count_7d += 1
    return {
        "surfaced_lifetime": counts["surfaced"],
        "accepted": counts["accepted"],
        "rejected": counts["rejected"],
        "accept_ratio": accept_ratio,
        "cache_age_seconds": caches,
        "adaptive_bump": adaptive_bump,
        "last_act_age_seconds": last_act_age_seconds,
        "stale_count_7d": stale_count_7d,
        "resurface_after_reject_count": resurfaced_count,
        "resurface_after_reject_rate": resurface_after_reject_rate,
    }


def index_summary(root: Path) -> dict[str, Any]:
    db = root / ".ai" / "cache" / "code.sqlite"
    if not db.exists():
        return {"present": False, "indexed_files": 0, "indexed_bytes": 0, "db_bytes": 0}
    indexed_files = 0
    indexed_bytes = 0
    try:
        import sqlite3
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "select count(*) as n, coalesce(sum(m.bytes), 0) as b "
                "from chunks c left join chunk_meta m on m.chunk_id = c.id"
            ).fetchone()
            indexed_files = int(row[0] or 0)
            indexed_bytes = int(row[1] or 0)
    except Exception:
        pass
    return {
        "present": True,
        "indexed_files": indexed_files,
        "indexed_bytes": indexed_bytes,
        "db_bytes": db.stat().st_size,
    }


def release_artifact_summary(root: Path) -> dict[str, Any]:
    summary_path = root / "dist" / "release-gate.summary.json"
    if not summary_path.exists():
        return {
            "summary_path": None,
            "release_ready": None,
            "all_present": None,
            "all_valid": None,
            "all_current": None,
        }
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    artifacts = summary.get("release_artifacts", {}) if isinstance(summary, dict) else {}
    return {
        "summary_path": summary_path.relative_to(root).as_posix(),
        "release_ready": summary.get("release_ready") if isinstance(summary, dict) else None,
        "all_present": artifacts.get("all_present") if isinstance(artifacts, dict) else None,
        "all_valid": artifacts.get("all_valid") if isinstance(artifacts, dict) else None,
        "all_current": artifacts.get("all_current") if isinstance(artifacts, dict) else None,
    }


def slo_bench(root: Path, iterations: int = 10) -> dict[str, Any]:
    from .hooks import handle_hook

    elapsed: list[int] = []
    for _ in range(iterations):
        result = handle_hook(root, "SLOBaseline", {"agent": "bench", "dry": True})
        elapsed.append(int(result["elapsed_ms"]))
    p95 = sorted(elapsed)[max(0, int(len(elapsed) * 0.95) - 1)] if elapsed else 0
    return {"ok": p95 <= 200, "iterations": iterations, "p95_ms": p95, "target_ms": 200, "samples_ms": elapsed}


def diagnostics(root: Path, *, dry_run: bool = False, include_doctor: bool = True) -> dict[str, Any]:
    checks = as_payload(run_checks(root)) if include_doctor else {"ok": True, "checks": []}
    bundle = {
        "created_at": now_iso(),
        "runtime_version": __version__,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "doctor": redact_value(checks),
        "metrics": redact_value(metrics(root)),
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "bundle": bundle}
    diag_root = root / ".ai" / "cache" / "diagnostics"
    diag_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = diag_root / f"diagnostics-{stamp}.json"
    zip_path = diag_root / f"diagnostics-{stamp}.zip"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(json_path, json_path.name)
    return {"ok": True, "dry_run": False, "path": zip_path.relative_to(root).as_posix(), "retention_days": 30}


def prune_diagnostics(root: Path, *, keep_days: int = 30) -> dict[str, Any]:
    cutoff = time.time() - keep_days * 86400
    removed = 0
    diag_root = root / ".ai" / "cache" / "diagnostics"
    if not diag_root.exists():
        return {"ok": True, "removed": 0}
    for path in diag_root.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    for path in diag_root.iterdir():
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path)
            removed += 1
    return {"ok": True, "removed": removed}
