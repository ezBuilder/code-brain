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
    # T46: derive KPI block from raw claude summary (pre-compact) so it stays
    # stable regardless of include_sessions. codex is left as a hook for now;
    # codex pricing/cache semantics differ and are not folded into these KPIs.
    claude_tokens = claude.get("tokens", {}) if isinstance(claude.get("tokens"), dict) else {}
    cache_metrics = _cache_hit_metrics(claude_tokens)
    claude_messages = int(claude.get("messages") or 0)
    claude_total_tokens = int(claude.get("total_observed_tokens") or 0)
    tokens_per_message = (
        round(claude_total_tokens / claude_messages, 3) if claude_messages > 0 else 0
    )
    kpi = {
        "claude_cache_hit_ratio": cache_metrics["cache_hit_ratio"],
        "tokens_per_message": tokens_per_message,
        "effective_input_tokens": cache_metrics["effective_input_tokens"],
    }
    if not include_sessions:
        claude = _usage_totals_only(claude)
        codex = _usage_totals_only(codex)
    return {
        "ok": True,
        "actual_token_usage": {
            "claude": claude,
            "codex": codex,
        },
        "kpi": kpi,
        "measured_code_brain_effect": events,
        "claims": {
            "token_usage": "actual only when sourced from agent session transcripts",
            "context_reduction": "measured in bytes only; no token-saving estimate is emitted",
        },
    }


def _cache_hit_metrics(tokens: dict[str, Any]) -> dict[str, Any]:
    """Compute Claude cache-hit KPI block from a transcript ``tokens`` dict.

    Returns a fixed shape regardless of input — zero denominators yield
    ``cache_hit_ratio == 0.0`` instead of raising. ``effective_input_tokens``
    approximates Anthropic's pricing: cache reads cost ~10% of an uncached
    input token and cache writes cost ~1.25x. Output tokens are not included
    here (this metric is input-side only).
    """
    input_tokens = int(tokens.get("input_tokens", 0) or 0)
    cache_read = int(tokens.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(tokens.get("cache_creation_input_tokens", 0) or 0)
    total_input_with_cache = input_tokens + cache_read + cache_creation
    if total_input_with_cache > 0:
        cache_hit_ratio = round(cache_read / total_input_with_cache, 4)
    else:
        cache_hit_ratio = 0.0
    effective_input_tokens = round(
        input_tokens + cache_read * 0.1 + cache_creation * 1.25, 3
    )
    return {
        "cache_hit_ratio": cache_hit_ratio,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "effective_input_tokens": effective_input_tokens,
        "total_input_with_cache": total_input_with_cache,
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
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
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
    tokens = payload.get("tokens", {}) if isinstance(payload.get("tokens"), dict) else {}
    compact = {
        "ok": payload.get("ok"),
        "source": payload.get("source"),
        "sessions_scanned": payload.get("sessions_scanned", 0),
        "sessions_matched": payload.get("sessions_matched", 0),
        "messages": payload.get("messages", 0),
        "tokens": tokens,
        "total_observed_tokens": payload.get("total_observed_tokens", 0),
        # T46: surface claude cache-hit KPI alongside totals. Codex token dicts
        # use different field names so its metrics will all be zero — kept for
        # shape stability; codex-side semantics will be wired separately.
        "cache_metrics": _cache_hit_metrics(tokens),
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
        "layers": _advanced_layers_summary(root),
    }
    return redact_value(payload)


def _advanced_layers_summary(root: Path) -> dict[str, Any]:
    """Health snapshot for T28-T31 next-gen modules. Each section fails-soft to None
    if its module is unavailable — never crashes the health endpoint."""
    out: dict[str, Any] = {
        "embedding": None,
        "codegraph": None,
        "memory_tier": None,
        "ast_verify": None,
    }
    # T28 — dense embedding
    try:
        from . import embedding as _emb
        out["embedding"] = _emb.status(root)
    except Exception:
        pass
    # T29 — codegraph
    try:
        from .search import connect, init_schema
        with connect(root) as conn:
            init_schema(conn)
            sym_count = int(conn.execute("select count(*) from code_symbols").fetchone()[0])
            call_count = int(conn.execute("select count(*) from code_calls").fetchone()[0])
            hotspots = conn.execute(
                "select callee, count(*) as n from code_calls "
                "group by callee order by n desc, callee asc limit 3"
            ).fetchall()
        out["codegraph"] = {
            "symbol_count": sym_count,
            "call_edge_count": call_count,
            "top_callees": [{"callee": r["callee"], "n": int(r["n"])} for r in hotspots],
        }
    except Exception:
        pass
    # T30 — memory tier
    try:
        from . import memory_tier as _mt
        cls = _mt.classify(root)
        pres = _mt.hot_pressure(root)
        out["memory_tier"] = {
            "tiers": cls["tiers"],
            "totals": cls["totals"],
            "page_out_recommended": pres["page_out_recommended"],
        }
    except Exception:
        pass
    # T31 — AST policy gate (module presence only; checker is on-demand)
    try:
        from . import ast_verify as _av  # noqa: F401
        out["ast_verify"] = {"present": True}
    except Exception:
        out["ast_verify"] = {"present": False}
    return out


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
    # T25 — per-source accept/reject counters (skill | agent | precall).
    per_source: dict[str, dict[str, int]] = {}
    # T25 — per-id earliest pending ts for action latency pairing (id -> earliest recommend_pending ts unpaired).
    pending_ts_by_id: dict[str, datetime] = {}
    latency_seconds: list[float] = []
    # T25 — lifetime recommend_pending count per id (for resurface count).
    pending_count_by_id: dict[str, int] = {}
    for audit_file in audit_files:
        try:
            fh = audit_file.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
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
                source = act.split(".", 1)[0]  # T25: "skill" | "agent" | "precall"
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
                    if isinstance(pid, str):
                        pending_count_by_id[pid] = pending_count_by_id.get(pid, 0) + 1
                    if parsed_ts is not None and isinstance(pid, str):
                        surfaced_records.append((parsed_ts, pid))
                        id_events.setdefault(pid, []).append((parsed_ts, "recommend_pending"))
                        # T25 latency: remember earliest unpaired pending ts for this id.
                        if pid not in pending_ts_by_id:
                            pending_ts_by_id[pid] = parsed_ts
                elif tail.startswith("accept"):
                    counts["accepted"] += 1
                    # T25 source bucket
                    bucket = per_source.setdefault(source, {"accepted": 0, "rejected": 0})
                    bucket["accepted"] += 1
                    if isinstance(pid, str):
                        acted_ids.add(pid)
                        # T25 latency: pair with earliest pending for this id, if any.
                        if parsed_ts is not None and pid in pending_ts_by_id:
                            delta = (parsed_ts - pending_ts_by_id.pop(pid)).total_seconds()
                            if delta >= 0:
                                latency_seconds.append(delta)
                    if parsed_ts is not None and (last_act_dt is None or parsed_ts > last_act_dt):
                        last_act_dt = parsed_ts
                elif tail == "reject":
                    counts["rejected"] += 1
                    # T25 source bucket
                    bucket = per_source.setdefault(source, {"accepted": 0, "rejected": 0})
                    bucket["rejected"] += 1
                    if isinstance(pid, str):
                        acted_ids.add(pid)
                        if parsed_ts is not None:
                            id_events.setdefault(pid, []).append((parsed_ts, "reject"))
                        # T25 latency: a reject also closes the pending pairing.
                        if parsed_ts is not None and pid in pending_ts_by_id:
                            delta = (parsed_ts - pending_ts_by_id.pop(pid)).total_seconds()
                            if delta >= 0:
                                latency_seconds.append(delta)
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
    # ---- T25 additive KPIs ------------------------------------------------
    # 1) per-source accept rate
    source_accept_rate: dict[str, float | None] = {}
    for src, bucket in per_source.items():
        denom = bucket["accepted"] + bucket["rejected"]
        source_accept_rate[src] = (
            round(bucket["accepted"] / denom, 3) if denom > 0 else None
        )
    # 2) action latency p75 (None if < 5 samples)
    if len(latency_seconds) >= 5:
        sorted_lat = sorted(latency_seconds)
        idx = int(0.75 * len(sorted_lat))
        if idx >= len(sorted_lat):
            idx = len(sorted_lat) - 1
        action_latency_p75_seconds: int | None = int(sorted_lat[idx])
    else:
        action_latency_p75_seconds = None
    # 3) top resurfaced ids (top 5 by recommend_pending count)
    top_resurfaced_ids = [
        {"id": pid, "count": c}
        for pid, c in sorted(pending_count_by_id.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]
    # 4) stale_surfaced_ratio
    surfaced_total = counts["surfaced"]
    stale_surfaced_ratio: float | None = (
        round(stale_count_7d / surfaced_total, 3) if surfaced_total > 0 else None
    )
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
        # T25 additive KPIs:
        "source_accept_rate": source_accept_rate,
        "action_latency_p75_seconds": action_latency_p75_seconds,
        "top_resurfaced_ids": top_resurfaced_ids,
        "stale_surfaced_ratio": stale_surfaced_ratio,
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


def mem_eval_summary(root: Path, *, window_days: int = 7) -> dict[str, Any]:
    """Memory and recommendation quality metrics over a time window.

    Returns time-series aggregates (daily buckets) for:
    - accept/reject recommend actions
    - hot-tier audit volume
    - search index freshness
    - lesson volume (if lessons.jsonl exists)

    All OSError/JSONDecodeError are silently caught. Missing files return
    zero counts. Malformed JSON lines are skipped.

    Returns:
        {
            "ok": True,
            "window_days": int,
            "accept_rate_by_day": {"2026-05-20": {"accept": N, "reject": M}, ...},
            "hot_audit_by_day": {"2026-05-20": N, ...},
            "search_index_age_seconds": int | None,
            "lessons_added_recent": int
        }
    """
    from datetime import timedelta
    import sqlite3

    now_dt = datetime.now(timezone.utc)
    window_start = now_dt - timedelta(days=window_days)

    # 1. Aggregate accept/reject rates by day from audit files
    accept_rate_by_day: dict[str, dict[str, int]] = {}
    hot_audit_by_day: dict[str, int] = {}
    audit_files = all_audit_files(root)

    for audit_file in audit_files:
        try:
            fh = audit_file.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = str(rec.get("ts") or "")
                parsed_ts: datetime | None = None
                if ts_raw:
                    try:
                        parsed_ts = (
                            datetime.fromisoformat(ts_raw[:-1]).replace(tzinfo=timezone.utc)
                            if ts_raw.endswith("Z")
                            else datetime.fromisoformat(ts_raw)
                        )
                    except ValueError:
                        parsed_ts = None
                if parsed_ts is None or parsed_ts < window_start:
                    continue
                date_key = parsed_ts.strftime("%Y-%m-%d")

                # Accept/reject tracking
                action = str(rec.get("action") or "")
                if action.startswith(("skill.", "agent.", "precall.")):
                    tail = action.split(".", 1)[1]
                    if "accept" in tail:
                        bucket = accept_rate_by_day.setdefault(date_key, {"accept": 0, "reject": 0})
                        bucket["accept"] += 1
                    elif tail == "reject":
                        bucket = accept_rate_by_day.setdefault(date_key, {"accept": 0, "reject": 0})
                        bucket["reject"] += 1

                # Hot-tier pressure: count recommend_pending + accept actions (surfaced candidates)
                if action in ("skill.recommend_pending", "agent.recommend_pending", "precall.recommend_pending"):
                    hot_audit_by_day[date_key] = hot_audit_by_day.get(date_key, 0) + 1

    # 2. Search index age: max(chunks.updated_at) from code.sqlite
    search_index_age_seconds: int | None = None
    db_path = root / ".ai" / "cache" / "code.sqlite"
    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "select max(updated_at) as last_update from chunks"
                ).fetchone()
                if row and row["last_update"]:
                    try:
                        last_ts = datetime.fromisoformat(
                            row["last_update"][:-1].replace("+00:00", "")
                        ).replace(tzinfo=timezone.utc)
                        search_index_age_seconds = int((now_dt - last_ts).total_seconds())
                    except (ValueError, AttributeError):
                        pass
        except Exception:
            pass

    # 3. Lesson volume: count lines added in window_days
    lessons_added_recent = 0
    lessons_path_obj = root / ".ai" / "memory" / "lessons.jsonl"
    if lessons_path_obj.exists():
        try:
            fh = lessons_path_obj.open(encoding="utf-8", errors="replace")
        except OSError:
            pass
        else:
            with fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_raw = str(rec.get("created_at") or "")
                    parsed_ts: datetime | None = None
                    if ts_raw:
                        try:
                            parsed_ts = (
                                datetime.fromisoformat(ts_raw[:-1]).replace(tzinfo=timezone.utc)
                                if ts_raw.endswith("Z")
                                else datetime.fromisoformat(ts_raw)
                            )
                        except ValueError:
                            parsed_ts = None
                    if parsed_ts is not None and parsed_ts >= window_start:
                        lessons_added_recent += 1

    # Normalize daily buckets: ensure all days in window are present
    for i in range(window_days):
        day_dt = now_dt - timedelta(days=i)
        date_key = day_dt.strftime("%Y-%m-%d")
        if date_key not in accept_rate_by_day:
            accept_rate_by_day[date_key] = {"accept": 0, "reject": 0}
        if date_key not in hot_audit_by_day:
            hot_audit_by_day[date_key] = 0

    return {
        "ok": True,
        "window_days": window_days,
        "accept_rate_by_day": {k: accept_rate_by_day[k] for k in sorted(accept_rate_by_day.keys())},
        "hot_audit_by_day": {k: hot_audit_by_day[k] for k in sorted(hot_audit_by_day.keys())},
        "search_index_age_seconds": search_index_age_seconds,
        "lessons_added_recent": lessons_added_recent,
    }
