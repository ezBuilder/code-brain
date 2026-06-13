"""code-brain-loopd control plane (PRD §5, §7, §8) — token-free dispatch & recovery.

loopd is deterministic Python: it watches the file queue, scores warm workers, injects a
task into the chosen tmux pane, and records lifecycle — all WITHOUT calling an LLM. An empty
queue costs zero tokens (``llm_idle_polls`` is always 0). High-risk/approval-gated requests
are never auto-dispatched; they are parked as ``blocked: approval_required`` for a human.

This module owns the policy and lifecycle; the actual queue primitives stay in
loop_engineering, the worker inventory in worker_registry, and pane I/O in tmux_adapter.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import loop_engineering as le
from . import worker_registry as wr
from .memory import append_audit, now_iso
from .tmux_adapter import TmuxAdapterBase, get_adapter, output_hash

# PRD §7.2 / §12.2 — work that must never auto-dispatch without explicit approval.
_GATED = re.compile(
    r"(deploy|prod(uction)?|배포|release\s+to\s+prod|ship\s+(the\s+)?build|"
    r"billing|결제|구독|subscrib|invoice|"
    r"secret|시크릿|토큰|token|auth\b|oauth|인증|권한|authoriz|"
    r"password|passwd|credential|api[\s._-]?key|private[\s._-]?key|secret[\s._-]?key|access[\s._-]?key|"
    r"kubectl|terraform\s+apply|ansible-playbook|helm\s+(upgrade|install)|docker\s+push|registry|"
    r"destructive|drop\s+(table|database|schema|index)|truncate|삭제|데이터\s*삭제|"
    r"delete\s+from|remove\s+all\s+rows|wipe\s+the|clear\s+the\s+(database|table)|"
    r"\bgit\s+(push|merge|rebase|reset\s+--hard)|git-push|gh\s+(pr\s+merge|repo\s+delete)|"
    r"rm\s+-r|rimraf|find\s+.*-delete|rmtree)",
    re.IGNORECASE,
)
HEARTBEAT_TTL_SECONDS = 120
BLOCKED_PARTS = (".ai", "memory", "loop", "blocked")
_PANE_RE = re.compile(r"^%\d+$")  # tmux pane ids only; refuse session:window targets (cross-session inject)
# Benign mid-task interrupts a 3rd-party CLI can pop up that stall an autonomous worker; map a
# capture-pane substring → the safe key(s) to clear it. Best-effort self-healing in recovery_tick.
_BENIGN_INTERRUPTS = (
    ("How's the CLI experience", ["0", "Enter"]),   # agy feedback survey → Skip
    ("Help us improve", ["0", "Enter"]),
)


def nudge_workers(root: Path, *, adapter: TmuxAdapterBase | None = None) -> list[str]:
    """Clear known benign interrupts on busy workers so they don't stall. Fail-soft, no LLM."""
    adapter = adapter or get_adapter()
    if not hasattr(adapter, "capture") or not hasattr(adapter, "send_key"):
        return []
    nudged: list[str] = []
    for w in wr.list_workers(root):
        if w.get("state") not in ("assigned", "working", "reviewing"):
            continue
        pane = str((w.get("tmux") or {}).get("pane_id") or "")
        if not _PANE_RE.fullmatch(pane):
            continue
        try:
            screen = adapter.capture(pane) or ""
        except Exception:
            continue
        for pattern, keys in _BENIGN_INTERRUPTS:
            if pattern in screen:
                for k in keys:
                    adapter.send_key(pane, k)  # type: ignore[attr-defined]
                nudged.append(w["worker_id"])
                break
    return nudged


def _blocked_dir(root: Path) -> Path:
    return root.joinpath(*BLOCKED_PARTS)


def infer_risk(request: dict[str, Any]) -> str:
    # scan the WHOLE request (incl. dispatch/cwd/custom fields), not just goal/instruction —
    # a gated keyword hidden in any field must still trip the approval gate.
    try:
        text = json.dumps(request, ensure_ascii=False, default=str)
    except Exception:
        text = " ".join(str(v) for v in request.values())
    dispatch = request.get("dispatch") if isinstance(request.get("dispatch"), dict) else {}
    declared = str(dispatch.get("risk_tier", "")).lower()
    if declared in ("low", "medium", "high"):
        if declared == "high" or _GATED.search(text):
            return "high"
        return declared
    return "high" if _GATED.search(text) else "medium"


def select_worker(root: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    """PRD §7.1 worker selection: hard constraints then soft scoring. Returns a worker or None."""
    risk = infer_risk(request)
    dispatch = request.get("dispatch") if isinstance(request.get("dispatch"), dict) else {}
    required = set(dispatch.get("required_capabilities") or [])
    preferred = [str(a) for a in (dispatch.get("preferred_agents") or [])]
    req_cwd = str(request.get("cwd") or "")

    candidates: list[dict[str, Any]] = []
    for w in wr.list_workers(root):
        if w.get("state") not in wr.ASSIGNABLE:
            continue
        if risk not in (w.get("risk_tier_allowed") or []):
            continue
        if required and not required.issubset(set(w.get("capabilities") or [])):
            continue
        if req_cwd and str(w.get("cwd") or "") and str(w.get("cwd")) != req_cwd:
            continue
        candidates.append(w)
    if not candidates:
        return None

    def score(w: dict[str, Any]) -> tuple:
        prefer = 1 if w.get("agent") in preferred else 0
        quota_ok = 1 if str((w.get("usage") or {}).get("quota_state")) != "quota_exhausted" else 0
        # Codex/Claude favored for high-risk code/review; AGY pool favored for low-risk/research.
        tie = 1 if (risk == "high") == (w.get("agent") in ("codex", "claude")) else 0
        return (prefer, quota_ok, tie, -int((w.get("usage") or {}).get("requests_today", 0) or 0))

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _task_injection(request: dict[str, Any], worker: dict[str, Any], lease_id: str) -> str:
    # loopd already claimed the request for this worker — the worker must NOT call a generic
    # claim (that races and steals other workers' tasks). It processes this id with this lease.
    rid = request.get("id", "")
    wid = worker.get("worker_id", "")
    goal = str(request.get("goal", ""))[:200]
    return (
        f"Code Brain assigned you request {rid} (already claimed for you; do NOT run loop claim). "
        f"Goal: {goal}. Do the work, then finish with "
        f".ai/bin/ai loop complete --request-id {rid} --lease-id {lease_id} --summary \"<short>\" --json "
        f"(or .ai/bin/ai loop fail --request-id {rid} --lease-id {lease_id} --reason \"<why>\" for a real blocker). "
        f"Respect approval gates for secrets/auth/billing/prod/destructive actions."
    )


def _park_blocked(root: Path, request: dict[str, Any], reason: str) -> None:
    rid = str(request.get("id") or "")
    if not rid:
        return
    d = _blocked_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / f"{rid}.json"
    if marker.exists():
        return  # idempotent: already parked
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"request_id": rid, "reason": reason, "goal": str(request.get("goal", ""))[:200],
                               "at": now_iso()}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(marker)
    append_audit(root, action="loopd.blocked", category="loopd", payload={"request_id": rid, "reason": reason})


def _is_parked(root: Path, rid: str) -> bool:
    return (_blocked_dir(root) / f"{rid}.json").exists()


def dispatch_once(root: Path, *, adapter: TmuxAdapterBase | None = None) -> dict[str, Any]:
    """Scan the inbox once and dispatch each pending request to a worker. No LLM, ever."""
    le.ensure_loop_dirs(root)
    adapter = adapter or get_adapter()
    inbox = sorted((le.loop_root(root) / "inbox").glob("*.json"))
    dispatched: list[dict[str, str]] = []
    blocked: list[str] = []
    skipped = 0
    for path in inbox:
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = str(request.get("id") or "")
        if not rid or not le._REQUEST_ID_RE.fullmatch(rid) or _is_parked(root, rid):
            continue  # only well-formed, server-minted request ids are dispatchable
        if infer_risk(request) == "high":
            _park_blocked(root, request, "approval_required")
            blocked.append(rid)
            continue
        worker = select_worker(root, request)
        if worker is None:
            skipped += 1
            continue
        pane = str((worker.get("tmux") or {}).get("pane_id") or "")
        if not _PANE_RE.fullmatch(pane):
            skipped += 1
            continue
        # claim this exact request for the worker BEFORE injecting, so two workers never collide.
        claimed = le.claim(root, orchestrator_id="loopd", agent=str(worker.get("agent", "agent")),
                           request_id=rid)
        lease = (claimed.get("request") or {}).get("lease_id") if isinstance(claimed, dict) else None
        if not lease or not re.fullmatch(r"[0-9a-f]{8,64}", str(lease)):
            skipped += 1   # only a well-formed hex lease is injected (no control chars into tmux)
            continue
        if not adapter.inject(pane, _task_injection(request, worker, str(lease))):
            skipped += 1
            continue
        wr.set_state(root, worker_id=worker["worker_id"], state="assigned", request_id=rid)
        wr.write_heartbeat(root, worker_id=worker["worker_id"], state="assigned", request_id=rid, pane_id=pane)
        append_audit(root, action="loopd.dispatch", category="loopd",
                     payload={"request_id": rid, "worker_id": worker["worker_id"]})
        dispatched.append({"request_id": rid, "worker_id": worker["worker_id"]})
    return {"ok": True, "dispatched": dispatched, "blocked": blocked, "skipped": skipped, "llm_idle_polls": 0}


def recovery_tick(root: Path, *, now_seconds: float | None = None,
                  adapter: TmuxAdapterBase | None = None) -> dict[str, Any]:
    """PRD §8.6 — recover expired leases, free completed/stale workers, nudge benign interrupts."""
    from datetime import datetime, timezone

    nudged = nudge_workers(root, adapter=adapter)

    moment = datetime.now(timezone.utc).timestamp() if now_seconds is None else now_seconds
    qroot = le.loop_root(root)
    freed: list[str] = []
    stale: list[str] = []
    for w in wr.list_workers(root):
        if w.get("state") not in ("assigned", "working", "reviewing"):
            continue
        # completion → idle: if the worker's request left processing (done/dead), free the worker.
        rid = str(w.get("current_request_id") or "")
        if rid:
            in_processing = (qroot / "processing" / f"{rid}.json").exists()
            settled = (qroot / "done" / f"{rid}.json").exists() or (qroot / "dead" / f"{rid}.json").exists()
            if settled and not in_processing:
                wr.set_state(root, worker_id=w["worker_id"], state="idle", request_id=None)
                freed.append(w["worker_id"])
                continue
        beat = wr.read_heartbeat(root, w["worker_id"]) or {}
        seen = str(beat.get("last_seen_at") or w.get("heartbeat_at") or "")
        try:
            seen_ts = datetime.fromisoformat(seen.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if moment - seen_ts > HEARTBEAT_TTL_SECONDS:
            wr.set_state(root, worker_id=w["worker_id"], state="stale")
            stale.append(w["worker_id"])
    recovered = 0
    try:
        rec = le.recover_expired(root)
        recovered = int(rec.get("recovered", 0)) if isinstance(rec, dict) else 0
    except Exception:
        pass
    return {"ok": True, "freed_workers": freed, "stale_workers": stale, "nudged_workers": nudged,
            "recovered_requests": recovered, "llm_idle_polls": 0}


def status(root: Path) -> dict[str, Any]:
    le.ensure_loop_dirs(root)
    qroot = le.loop_root(root)

    def _count(name: str) -> int:
        return len(list((qroot / name).glob("*.json")))

    workers = wr.list_workers(root)
    by_state: dict[str, int] = {}
    for w in workers:
        by_state[str(w.get("state"))] = by_state.get(str(w.get("state")), 0) + 1
    return {
        "ok": True,
        "daemon": "embedded",
        "project": str(root),
        "queue": {"pending": _count("inbox"), "processing": _count("processing"),
                  "done": _count("done"), "dead": _count("dead"), "blocked": _count("blocked")},
        "workers": {"total": len(workers), **by_state},
        "llm_idle_polls": 0,
    }
