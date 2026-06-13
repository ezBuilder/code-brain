from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory import append_audit, append_decision
from .session_resume import write_handoff
from .worker.lock import queue_lock

LOOP_KIND = "loop.orchestrate"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_LEASE_SECONDS = 300
MAX_INSTRUCTION_BYTES = 128_000
MAX_RUBRIC_BYTES = 128_000
MAX_DISTILL_BYTES = 4096
MAX_ATTEMPTS = 5  # expired requests recovered this many times then dead-lettered
VALID_VERDICTS = {"pass", "fail", "blocked"}

# request ids are server-generated as loop-<ms>-<hex>; reject anything else so a
# caller-supplied --request-id cannot traverse outside processing/ (path safety).
_REQUEST_ID_RE = re.compile(r"^loop-[0-9]+-[0-9a-f]+$")


class LoopPhaseError(ValueError):
    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.metadata = metadata


def _validate_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError(f"invalid request_id: {request_id!r}")
    return request_id


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat().replace("+00:00", "Z")


def loop_root(root: Path) -> Path:
    return root / ".ai" / "memory" / "loop"


def ensure_loop_dirs(root: Path) -> None:
    for name in ("inbox", "processing", "done", "dead", ".tmp"):
        (loop_root(root) / name).mkdir(parents=True, exist_ok=True)


def submit(
    root: Path,
    *,
    instruction: str,
    goal: str,
    source_agent: str = "agent",
    target_agent: str = "agent",
    role: str = "worker",
    priority: str = "P1",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    reviewer_required: bool = True,
    rubric: str = "",
    checklist: list[str] | None = None,
) -> dict[str, Any]:
    if priority not in {"P0", "P1", "P2", "P3"}:
        raise ValueError(f"invalid priority: {priority}")
    instruction = instruction.strip()
    goal = goal.strip()
    if not instruction:
        raise ValueError("instruction is required")
    if not goal:
        goal = _first_line(instruction)
    if len(instruction.encode("utf-8")) > MAX_INSTRUCTION_BYTES:
        raise ValueError(f"instruction exceeds {MAX_INSTRUCTION_BYTES} bytes")
    rubric = rubric.strip()
    if len(rubric.encode("utf-8")) > MAX_RUBRIC_BYTES:
        raise ValueError(f"rubric exceeds {MAX_RUBRIC_BYTES} bytes")
    checklist_items = _clean_checklist(checklist or [])
    if interval_seconds < 60 or interval_seconds > 86_400:
        raise ValueError("interval_seconds must be between 60 and 86400")
    request_id = f"loop-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    payload = {
        "schema_version": 1,
        "id": request_id,
        "kind": LOOP_KIND,
        "status": "pending",
        "priority": priority,
        "role": _bounded(role, 40),
        "source_agent": _bounded(source_agent, 40),
        "target_agent": _bounded(target_agent, 40),
        "goal": goal[:2000],
        "instruction": instruction,
        "rubric": rubric,
        "checklist": checklist_items,
        "loop_interval_seconds": interval_seconds,
        "reviewer_required": bool(reviewer_required),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "attempts": 0,
    }
    with queue_lock(root):
        ensure_loop_dirs(root)
        tmp = loop_root(root) / ".tmp" / f"{request_id}.json.tmp"
        final = loop_root(root) / "inbox" / f"{request_id}.json"
        _write_json(tmp, payload)
        tmp.replace(final)
    append_audit(root, action="loop.submit", category="loop", payload={"request_id": request_id, "priority": priority})
    return {"ok": True, "request": _public_request(payload, final, root)}


def claim(
    root: Path,
    *,
    orchestrator_id: str,
    agent: str = "agent",
    priority: str | None = None,
    request_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    if lease_seconds < 60 or lease_seconds > 86_400:
        raise ValueError("lease_seconds must be between 60 and 86400")
    if request_id is not None and not _REQUEST_ID_RE.fullmatch(str(request_id)):
        raise ValueError("invalid request_id")
    with queue_lock(root):
        ensure_loop_dirs(root)
        recovered = _recover_expired_locked(root)
        if request_id is not None:
            # targeted claim: take exactly this request (loopd assigns; workers do not race).
            candidates = [loop_root(root) / "inbox" / f"{request_id}.json"]
        else:
            candidates = sorted((loop_root(root) / "inbox").glob("*.json"))
        for source in candidates:
            if not source.exists():
                continue
            request = _read_json(source)
            if priority and request.get("priority") != priority:
                continue
            target = loop_root(root) / "processing" / source.name
            try:
                source.rename(target)
            except FileNotFoundError:
                continue
            lease_id = secrets.token_hex(16)
            request.update(
                {
                    "status": "processing",
                    "lease_id": lease_id,
                    "orchestrator_id": _bounded(orchestrator_id, 80),
                    "claimed_by_agent": _bounded(agent, 40),
                    "claimed_at": now_iso(),
                    "lease_expires_at": (now() + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z"),
                    "attempts": int(request.get("attempts", 0) or 0) + 1,
                    "updated_at": now_iso(),
                }
            )
            _write_json(target, request)
            handoff = _write_goal_handoff(root, request, agent=agent)
            append_audit(
                root,
                action="loop.claim",
                category="loop",
                payload={"request_id": request["id"], "orchestrator_id": orchestrator_id},
            )
            return {
                "ok": True,
                "request": _public_request(request, target, root),
                "handoff": handoff,
                "contract": _contract(request),
                "recovered": recovered,
            }
    return {"ok": True, "request": None, "recovered": recovered}


def complete(root: Path, *, request_id: str, lease_id: str, summary: str, result: str = "") -> dict[str, Any]:
    return _finish(root, request_id=request_id, lease_id=lease_id, status="done", summary=summary, result=result)


def fail(root: Path, *, request_id: str, lease_id: str, reason: str) -> dict[str, Any]:
    return _finish(root, request_id=request_id, lease_id=lease_id, status="dead", summary=reason, result="")


def record_verdict(
    root: Path,
    *,
    request_id: str,
    lease_id: str,
    reviewer: str,
    verdict: str,
    summary: str,
    rubric_result: str = "",
) -> dict[str, Any]:
    _validate_request_id(request_id)
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict}")
    summary = summary.strip()
    if not summary:
        raise ValueError("summary is required")
    with queue_lock(root):
        ensure_loop_dirs(root)
        source = loop_root(root) / "processing" / f"{request_id}.json"
        if not source.exists():
            raise ValueError(f"loop request is not processing: {request_id}")
        request = _read_json(source)
        if request.get("lease_id") != lease_id:
            raise ValueError("lease_id mismatch")
        request["reviewer_verdict"] = {
            "id": f"verdict-{secrets.token_hex(4)}",
            "reviewer": _bounded(reviewer, 80),
            "verdict": verdict,
            "summary": summary[:4000],
            "rubric_result": rubric_result.strip()[:MAX_RUBRIC_BYTES],
            "recorded_at": now_iso(),
        }
        request["updated_at"] = now_iso()
        _write_json(source, request)
    append_audit(root, action="loop.verdict", category="loop", payload={"request_id": request_id, "verdict": verdict})
    return {"ok": True, "request": _public_request(request, source, root)}


def distill(
    root: Path, *, request_id: str, text: str, tags: list[str] | None = None, force: bool = False
) -> dict[str, Any]:
    _validate_request_id(request_id)
    text = text.strip()
    if not text:
        raise ValueError("distill text is required")
    with queue_lock(root):
        ensure_loop_dirs(root)
        done = loop_root(root) / "done" / f"{request_id}.json"
        dead = loop_root(root) / "dead" / f"{request_id}.json"
        if done.exists():
            source, outcome = done, "done"
        elif dead.exists():
            source, outcome = dead, "dead"
        else:
            raise ValueError(f"loop request is not finished (done/dead): {request_id}")
        request = _read_json(source)
        # Success distills require a passing review (the lesson is a verified win);
        # failure distills are post-mortems — no pass exists, so allow them so lessons
        # from failures are captured too (fail -> investigate -> verify -> distill).
        if outcome == "done" and bool(request.get("reviewer_required", True)) and not _verdict_passed(request):
            raise ValueError("reviewer verdict pass required before distill")
    # Contradiction gate: do not let a lesson silently overwrite/oppose an existing rule.
    # Surface same-topic decisions and require an explicit --force after the agent confirms
    # the new lesson does not contradict them (guards against self-reinforcing memory).
    if not force:
        conflicts = _conflicting_decisions(root, text)
        if conflicts:
            return {
                "ok": False,
                "reason": "potential_contradiction",
                "request_id": request_id,
                "conflicts": conflicts,
                "hint": "기존 decision과 토픽이 크게 겹친다. 모순/중복이 아닌지 확인하고 모순이 아니면 --force로 재실행하라.",
            }
    tag_list = ["loop", "distill", outcome, *[str(tag).strip() for tag in (tags or []) if str(tag).strip()]]
    decision = append_decision(root, text=text[:MAX_DISTILL_BYTES], tags=tag_list, source="loop.distill")
    append_audit(
        root,
        action="loop.distill",
        category="loop",
        payload={"request_id": request_id, "decision_id": decision.get("record", {}).get("id")},
    )
    return {"ok": True, "request_id": request_id, "decision": decision.get("record")}


def recover_expired(root: Path) -> dict[str, Any]:
    with queue_lock(root):
        ensure_loop_dirs(root)
        recovered = _recover_expired_locked(root)
    if recovered:
        append_audit(root, action="loop.recover_expired", category="loop", payload={"recovered": recovered})
    return {"ok": True, "recovered": recovered}


def status(root: Path) -> dict[str, Any]:
    ensure_loop_dirs(root)
    expired_processing = 0
    phase_issues: list[dict[str, Any]] = []
    expected_phases: dict[str, int] = {}
    for path in (loop_root(root) / "processing").glob("*.json"):
        try:
            request = _read_json(path)
            if _is_expired(request.get("lease_expires_at")):
                expired_processing += 1
        except Exception:
            continue
    for queue_name in ("inbox", "processing"):
        for path in sorted((loop_root(root) / queue_name).glob("*.json")):
            try:
                request = _read_json(path)
            except Exception:
                continue
            guard = _phase_guard(request)
            expected_phases[guard["expected_phase"]] = expected_phases.get(guard["expected_phase"], 0) + 1
            if guard["phase_issues"]:
                phase_issues.append(
                    {
                        "request_id": request.get("id"),
                        "status": request.get("status"),
                        "path": path.relative_to(root).as_posix(),
                        **guard,
                    }
                )
    return {
        "ok": True,
        "pending": len(list((loop_root(root) / "inbox").glob("*.json"))),
        "processing": len(list((loop_root(root) / "processing").glob("*.json"))),
        "expired_processing": expired_processing,
        "done": len(list((loop_root(root) / "done").glob("*.json"))),
        "dead": len(list((loop_root(root) / "dead").glob("*.json"))),
        "expected_phases": expected_phases,
        "out_of_plan": any(issue.get("out_of_plan") for issue in phase_issues),
        "phase_issue_count": sum(len(issue.get("phase_issues") or []) for issue in phase_issues),
        "phase_issues": phase_issues,
    }


def _finish(root: Path, *, request_id: str, lease_id: str, status: str, summary: str, result: str) -> dict[str, Any]:
    if status not in {"done", "dead"}:
        raise ValueError("invalid status")
    _validate_request_id(request_id)
    with queue_lock(root):
        ensure_loop_dirs(root)
        source = loop_root(root) / "processing" / f"{request_id}.json"
        if not source.exists():
            raise ValueError(f"loop request is not processing: {request_id}")
        request = _read_json(source)
        if request.get("lease_id") != lease_id:
            raise ValueError("lease_id mismatch")
        if status == "done" and bool(request.get("reviewer_required", True)) and not _verdict_passed(request):
            guard = _phase_guard(request, completion_attempt=True)
            raise LoopPhaseError("reviewer verdict pass required before complete", guard)
        request.update(
            {
                "status": status,
                "summary": summary.strip()[:4000],
                "result": result.strip()[:MAX_INSTRUCTION_BYTES],
                "finished_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        target = loop_root(root) / status / source.name
        _write_json(target, request)
        source.unlink()
    append_audit(root, action=f"loop.{status}", category="loop", payload={"request_id": request_id})
    return {"ok": True, "request": _public_request(request, target, root)}


def _write_goal_handoff(root: Path, request: dict[str, Any], *, agent: str) -> dict[str, Any]:
    plan = [
        "Act as orchestrator, not sole implementer.",
        "Delegate maker work and reviewer work to separate subagents where the host supports it.",
        "Use the request rubric/checklist as the completion contract.",
        "Record a passing reviewer verdict before loop complete when reviewer_required is true.",
        f"Run a bounded loop every {request.get('loop_interval_seconds', DEFAULT_INTERVAL_SECONDS)} seconds until tests pass or blockers are recorded.",
        "Write final result with ai loop complete; use ai loop fail only for real blockers.",
    ]
    return write_handoff(
        root,
        goal=str(request.get("goal", "")),
        next_step=str(request.get("instruction", ""))[:2000],
        plan=plan,
        open_questions=[],
        blockers=[],
        agent=agent,
    )


def _recover_expired_locked(root: Path) -> int:
    recovered = 0
    for source in sorted((loop_root(root) / "processing").glob("*.json")):
        try:
            request = _read_json(source)
        except Exception:
            continue
        if not _is_expired(request.get("lease_expires_at")):
            continue
        for key in ("lease_id", "orchestrator_id", "claimed_by_agent", "claimed_at", "lease_expires_at"):
            request.pop(key, None)
        if int(request.get("attempts", 0) or 0) >= MAX_ATTEMPTS:
            # Repeatedly abandoned (claimer kept dying): dead-letter instead of
            # re-queueing forever, so an expired request cannot loop indefinitely.
            request.update(
                {
                    "status": "dead",
                    "summary": f"dead-lettered after {MAX_ATTEMPTS} expired leases",
                    "finished_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            _write_json(loop_root(root) / "dead" / source.name, request)
            source.unlink()
            append_audit(root, action="loop.dead", category="loop", payload={"request_id": request.get("id")})
            continue
        request.update({"status": "pending", "updated_at": now_iso(), "recovered_at": now_iso()})
        target = loop_root(root) / "inbox" / source.name
        _write_json(target, request)
        source.unlink()
        recovered += 1
    return recovered


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires <= now()


def _contract(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "maker": "implementation subagent owns code/docs changes",
        "checker": "review subagent validates behavior, security, tests, and regressions",
        "rubric": str(request.get("rubric") or ""),
        "checklist": list(request.get("checklist") or []),
        "finish": "orchestrator integrates, records reviewer verdict, verifies, then records complete/fail",
        "reviewer_required": bool(request.get("reviewer_required", True)),
    }


def _verdict_passed(request: dict[str, Any]) -> bool:
    verdict = request.get("reviewer_verdict")
    return isinstance(verdict, dict) and verdict.get("verdict") == "pass"


def _phase_guard(request: dict[str, Any], *, completion_attempt: bool = False) -> dict[str, Any]:
    expected_phase = _expected_phase(request)
    issues = _phase_issues(request)
    out_of_plan_codes = {"missing_rubric", "missing_checklist", "reviewer_verdict_failed", "reviewer_verdict_blocked"}
    if completion_attempt:
        out_of_plan_codes.add("missing_reviewer_verdict")
    out_of_plan = any(issue.get("code") in out_of_plan_codes for issue in issues)
    return {
        "expected_phase": expected_phase,
        "recovery_hint": _recovery_hint(expected_phase, issues),
        "out_of_plan": out_of_plan,
        "phase_issues": issues,
    }


def _expected_phase(request: dict[str, Any]) -> str:
    request_status = str(request.get("status") or "pending")
    if request_status == "pending":
        return "claim"
    if request_status == "done":
        return "distill"
    if request_status == "dead":
        return "postmortem"
    if request_status != "processing":
        return "inspect"
    if not bool(request.get("reviewer_required", True)):
        return "complete"
    verdict = request.get("reviewer_verdict")
    verdict_value = verdict.get("verdict") if isinstance(verdict, dict) else None
    if verdict_value == "pass":
        return "complete"
    if verdict_value == "fail":
        return "fix"
    if verdict_value == "blocked":
        return "unblock"
    return "review"


def _phase_issues(request: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not bool(request.get("reviewer_required", True)):
        return issues
    if not str(request.get("rubric") or "").strip():
        issues.append(
            _phase_issue(
                "missing_rubric",
                "reviewer_required request has no rubric",
                "resubmit reviewer-gated loop work with a rubric or use --no-review for ungated work",
            )
        )
    checklist = request.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = [str(checklist)]
    if not _clean_checklist(checklist):
        issues.append(
            _phase_issue(
                "missing_checklist",
                "reviewer_required request has no checklist",
                "resubmit reviewer-gated loop work with at least one checklist item",
            )
        )
    if str(request.get("status") or "") != "processing":
        return issues
    verdict = request.get("reviewer_verdict")
    verdict_value = verdict.get("verdict") if isinstance(verdict, dict) else None
    if verdict_value == "pass":
        return issues
    if verdict_value == "fail":
        issues.append(
            _phase_issue(
                "reviewer_verdict_failed",
                "reviewer verdict is fail",
                "address reviewer findings, verify again, then record a pass verdict",
            )
        )
    elif verdict_value == "blocked":
        issues.append(
            _phase_issue(
                "reviewer_verdict_blocked",
                "reviewer verdict is blocked",
                "resolve or document the blocker before completing the loop",
            )
        )
    else:
        issues.append(
            _phase_issue(
                "missing_reviewer_verdict",
                "reviewer verdict is required before complete",
                "record a pass reviewer verdict before completing the loop",
            )
        )
    return issues


def _phase_issue(code: str, message: str, recovery_hint: str) -> dict[str, str]:
    return {"code": code, "message": message, "recovery_hint": recovery_hint}


def _recovery_hint(expected_phase: str, issues: list[dict[str, str]]) -> str:
    priority = [
        "reviewer_verdict_failed",
        "reviewer_verdict_blocked",
        "missing_reviewer_verdict",
        "missing_rubric",
        "missing_checklist",
    ]
    for code in priority:
        for issue in issues:
            if issue.get("code") == code:
                return issue["recovery_hint"]
    return {
        "claim": "claim the loop request before starting work",
        "review": "record a reviewer verdict before completing the loop",
        "fix": "address reviewer findings before requesting another verdict",
        "unblock": "resolve the blocker before completing the loop",
        "complete": "complete the loop after local verification",
        "distill": "distill verified lessons if the run produced durable knowledge",
        "postmortem": "distill a postmortem lesson if useful",
    }.get(expected_phase, "inspect the loop request state")


_STOPWORDS = frozenset(
    "the a an and or but if then else when while for with from into onto of in on at to by is are was "
    "were be been being do does did not no nor so than that this these those it its as use used using "
    "loop distill done dead lesson rule should must always never can will".split()
)
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{3,}|[가-힣]{2,}")
_CONFLICT_THRESHOLD = 0.45  # share of new-lesson tokens that must overlap an existing decision
_CONFLICT_SCAN = 800        # only the most recent decisions are compared (bounded cost)


def _significant_tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS}


def _conflicting_decisions(root: Path, text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Recent decisions whose token set strongly overlaps the new lesson.

    Deterministic candidate-finder only (token Jaccard-ish): it surfaces same-topic
    decisions the agent must confirm the lesson does not contradict before it becomes a
    durable rule (guards against self-reinforcing / over-generalised memory). The semantic
    contradiction judgement is the agent's — it overrides with --force after reviewing.
    """
    from .memory import decisions_path

    new_tokens = _significant_tokens(text)
    if len(new_tokens) < 3:
        return []
    path = decisions_path(root)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    # Fold by id first so a superseded/retired failure (reused-id reappend) does not get
    # double-counted, and its now-retired original is dropped from the conflict scan.
    folded: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in lines[-_CONFLICT_SCAN:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        rid = str(record.get("id") or f"_anon{len(order)}")
        if rid not in folded:
            order.append(rid)
        folded[rid] = record
    hits: list[dict[str, Any]] = []
    for rid in order:
        record = folded[rid]
        if record.get("kind") == "failure" and str(record.get("status", "observed")) in {"stale", "refuted"}:
            continue  # retired failures are not durable rules; never a contradiction source
        existing = str(record.get("decision", ""))
        existing_tokens = _significant_tokens(existing)
        if not existing_tokens:
            continue
        overlap = len(new_tokens & existing_tokens) / len(new_tokens)
        if overlap >= _CONFLICT_THRESHOLD:
            hits.append({"id": record.get("id"), "overlap": round(overlap, 2), "decision": existing[:200]})
    hits.sort(key=lambda h: h["overlap"], reverse=True)
    return hits[:limit]


def _clean_checklist(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        value = str(item).strip()
        if value:
            cleaned.append(value[:500])
        if len(cleaned) >= 100:
            break
    return cleaned


def _public_request(request: dict[str, Any], path: Path, root: Path) -> dict[str, Any]:
    public = {key: value for key, value in request.items() if key not in {"instruction", "result"}}
    public["instruction"] = str(request.get("instruction", ""))
    if request.get("result"):
        public["result"] = str(request.get("result"))
    public["path"] = path.relative_to(root).as_posix()
    public.update(_phase_guard(request))
    return public


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid loop request: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    # Atomic write: a crash mid-write must not leave a half-written queue file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.writing")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip(" #\t")
        if stripped:
            return stripped[:2000]
    return "Code Brain loop task"


def _bounded(value: str, limit: int) -> str:
    cleaned = "".join(ch for ch in str(value or "agent") if ch.isalnum() or ch in "._- ")[:limit].strip()
    return cleaned or "agent"
