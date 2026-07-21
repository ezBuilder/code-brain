"""Stage 2 development-research metric ratchet loop (agent-driven; runtime = deterministic).

The CREATIVE step — editing the edit_surface with experiment ideas — is the calling agent's
job. The runtime owns only the deterministic mechanics: running the metric command in the
HARDENED sandbox (network + env isolation — sandbox.execute isolate_network/isolate_env,
§12.2.1 / Phase 1), extracting the metric, the ratchet KEEP/DISCARD decision, results.tsv, and
budget/stop enforcement. The runtime does NOT run git (worktree/commit/reset) and NEVER
auto-merges — the agent does git per AGENTS.md and a human reviews before merge (PRD §5.3).

Disabled by default (config autoresearch.loop.enable=false). metric_cmd must be a USER-trusted
command (do not synthesise it from untrusted content). No LLM, no network. stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .. import sandbox

_SID_RE = re.compile(r"^lp_[0-9a-f]{12}$")
DIRECTIONS = ("minimize", "maximize")

_MAX_ITERS_CAP = 500
_MAX_TIMEOUT_CAP = 3600
_MAX_GREP_LEN = 500
_MAX_CMD_LEN = 4096


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _loop_dir(project_root: Path) -> Path:
    d = storage.data_root(project_root) / storage.STATE / "loop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(project_root: Path, session_id: str) -> Path | None:
    # session_id format is strictly validated → no path traversal via crafted ids
    if not _SID_RE.match(session_id or ""):
        return None
    return _loop_dir(project_root) / f"{session_id}.json"


def _results_path(project_root: Path, session_id: str) -> Path | None:
    if not _SID_RE.match(session_id or ""):
        return None
    return _loop_dir(project_root) / f"{session_id}.results.tsv"


def _gen_id(workspace: str, metric_cmd: str) -> str:
    return "lp_" + hashlib.sha256(f"{workspace}\x00{metric_cmd}".encode("utf-8")).hexdigest()[:12]


def _enabled(project_root: Path) -> bool:
    """Stage 2 is OFF unless config autoresearch.loop.enable is true (auto-exec gate)."""
    try:
        from ..config import load_config
        cfg = load_config(project_root)
        section = (cfg.get("autoresearch") or {}).get("loop") or {}
        return bool(section.get("enable", False))
    except Exception:
        return False


def _read(project_root: Path, session_id: str) -> dict | None:
    p = _state_path(project_root, session_id)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(project_root: Path, session_id: str, state: dict) -> None:
    _state_path(project_root, session_id).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def _safe_workspace(project_root: Path, workspace: str) -> Path | None:
    """Resolve workspace; must be an existing dir at/inside the project (no traversal escape)."""
    try:
        proj = project_root.resolve()
        raw = Path(workspace)
        ws = raw.resolve() if raw.is_absolute() else (proj / raw).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if ws != proj and proj not in ws.parents:
        return None
    if not ws.is_dir():
        return None
    return ws


def _coerce_cmd(metric_cmd):
    """Return (cmd, repr_str) or (None, reason)."""
    if isinstance(metric_cmd, list):
        cmd = [str(p) for p in metric_cmd]
        rep = " ".join(cmd)
        if not cmd or not rep.strip():
            return None, "empty_metric_cmd"
        return cmd, rep
    if isinstance(metric_cmd, str):
        if not metric_cmd.strip():
            return None, "empty_metric_cmd"
        return metric_cmd, metric_cmd
    return None, "invalid_metric_cmd"


def start(project_root, *, workspace, metric_cmd, metric_grep, direction,
          edit_surface=None, max_iters=50, max_cost_usd=0.0, per_run_timeout_s=600) -> dict:
    proj = Path(project_root)
    if not _enabled(proj):
        return {"error": "loop_disabled"}
    if direction not in DIRECTIONS:
        return {"error": "invalid_direction"}
    cmd, rep = _coerce_cmd(metric_cmd)
    if cmd is None:
        return {"error": rep}
    if len(rep) > _MAX_CMD_LEN:
        return {"error": "metric_cmd_too_long"}
    if not isinstance(metric_grep, str) or not metric_grep or len(metric_grep) > _MAX_GREP_LEN:
        return {"error": "invalid_metric_grep"}
    try:
        re.compile(metric_grep)
    except re.error:
        return {"error": "invalid_metric_grep_regex"}
    ws = _safe_workspace(proj, str(workspace))
    if ws is None:
        return {"error": "invalid_workspace"}
    try:
        max_iters = max(1, min(int(max_iters), _MAX_ITERS_CAP))
        per_run_timeout_s = max(1, min(int(per_run_timeout_s), _MAX_TIMEOUT_CAP))
        max_cost_usd = max(0.0, float(max_cost_usd))
    except (TypeError, ValueError):
        return {"error": "invalid_budget"}

    sid = _gen_id(str(ws), rep)
    existing = _read(proj, sid)
    if existing is not None and existing.get("status") == "running":
        # deterministic id → refuse to clobber an in-flight loop; stop it first to restart
        return {"error": "session_already_running", "session_id": sid}
    state = {
        "session_id": sid,
        "workspace": str(ws),
        "metric_cmd": cmd,
        "metric_grep": metric_grep,
        "direction": direction,
        "edit_surface": [str(p)[:512] for p in (edit_surface or [])][:100],
        "budget": {
            "max_iters": max_iters,
            "max_cost_usd": max_cost_usd,
            "per_run_timeout_s": per_run_timeout_s,
        },
        "status": "running",
        "iters_used": 0,
        "cost_used": 0.0,
        "best": None,
    }
    _write(proj, sid, state)
    rp = _results_path(proj, sid)
    if rp is not None and not rp.exists():
        rp.write_text("iter\tmetric\tdecision\tcost_used\ttimestamp\n", encoding="utf-8")
    return state


def _extract_metric(text: str, pattern: str):
    m = re.search(pattern, text or "", re.MULTILINE)
    if not m:
        return None
    raw = m.group(1) if m.groups() else m.group(0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_better(metric: float, best: dict | None, direction: str) -> bool:
    if best is None:
        return True
    if direction == "minimize":
        return metric < best["metric"]
    return metric > best["metric"]


def record(project_root, session_id, *, cost_spent=0.0) -> dict:
    """Run one ratchet evaluation (after the agent edited + committed the edit_surface).

    Returns {iter, metric, decision(keep|discard|crash), reason, best, iters_used,
    cost_used, should_continue, exec_id}. The agent acts on `decision`: keep the commit, or
    git-reset on discard/crash. The runtime does no git.
    """
    proj = Path(project_root)
    state = _read(proj, session_id)
    if state is None:
        return {"error": "session_not_found"}
    if state.get("status") != "running":
        return {"error": "loop_not_running", "status": state.get("status")}

    budget = state["budget"]
    if state["iters_used"] >= budget["max_iters"]:
        state["status"] = "stopped"
        _write(proj, session_id, state)
        return {"should_continue": False, "reason": "max_iters_reached",
                "best": state["best"], "iters_used": state["iters_used"]}
    if budget["max_cost_usd"] > 0 and state["cost_used"] >= budget["max_cost_usd"]:
        state["status"] = "stopped"
        _write(proj, session_id, state)
        return {"should_continue": False, "reason": "max_cost_reached",
                "best": state["best"], "iters_used": state["iters_used"]}

    ws = _safe_workspace(proj, state["workspace"])
    if ws is None:
        return {"error": "invalid_workspace"}

    # Auto-execution happens ONLY here, always under the hardened sandbox (network + env).
    res = sandbox.execute(
        proj,
        command=state["metric_cmd"],
        cwd=str(ws),
        timeout=int(budget["per_run_timeout_s"]),
        isolate_network=True,
        isolate_env=True,
    )

    iter_no = state["iters_used"] + 1
    metric = None
    command_ok = res.get("command_ok")
    if command_ok is None:
        command_ok = res.get("exit_code") == 0
    if not res.get("ok") or not command_ok:
        termination = res.get("termination") if isinstance(res.get("termination"), dict) else {}
        decision = "crash"
        reason = str(termination.get("classification") or res.get("reason") or "run_failed")
    else:
        exec_id = res.get("exec_id")
        text = sandbox.read_output(proj, exec_id) if exec_id else None
        if text is None:
            text = res.get("output") or "\n".join(
                res.get("first_lines", []) + res.get("last_lines", [])
            )
        metric = _extract_metric(text, state["metric_grep"])
        if metric is None:
            decision, reason = "crash", "metric_not_found"
        elif _is_better(metric, state["best"], state["direction"]):
            decision, reason = "keep", "improved"
            state["best"] = {"metric": metric, "iter": iter_no}
        else:
            decision, reason = "discard", "no_improvement"

    try:
        cost_spent = max(0.0, float(cost_spent))
    except (TypeError, ValueError):
        cost_spent = 0.0
    state["iters_used"] = iter_no
    state["cost_used"] = round(state["cost_used"] + cost_spent, 6)

    should_continue = (
        state["iters_used"] < budget["max_iters"]
        and (budget["max_cost_usd"] <= 0 or state["cost_used"] < budget["max_cost_usd"])
    )
    if not should_continue:
        state["status"] = "stopped"
    _write(proj, session_id, state)

    rp = _results_path(proj, session_id)
    if rp is not None:
        metric_str = "" if metric is None else repr(metric)
        try:
            with rp.open("a", encoding="utf-8") as fh:
                fh.write(f"{iter_no}\t{metric_str}\t{decision}\t{state['cost_used']}\t{_now_iso()}\n")
        except OSError:
            pass

    return {
        "iter": iter_no,
        "metric": metric,
        "decision": decision,
        "reason": reason,
        "best": state["best"],
        "iters_used": state["iters_used"],
        "cost_used": state["cost_used"],
        "should_continue": should_continue,
        "exec_id": res.get("exec_id"),
        "termination": res.get("termination"),
        "peak_rss_kib": res.get("peak_rss_kib"),
        "output_truncated": bool(res.get("output_truncated", False)),
    }


def status(project_root, session_id) -> dict:
    state = _read(Path(project_root), session_id)
    return state if state is not None else {"error": "session_not_found"}


def stop(project_root, session_id) -> dict:
    proj = Path(project_root)
    state = _read(proj, session_id)
    if state is None:
        return {"error": "session_not_found"}
    state["status"] = "stopped"
    _write(proj, session_id, state)
    return state
