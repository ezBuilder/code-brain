"""Deterministic acceptance re-runner — the machine-verification half of a verified-completion gate.

OmO's Oracle is a read-only LLM that emits a `VERIFIED` token; that is only as honest as the model.
The durable upgrade (G1) is to corroborate a reviewer's `pass` by actually RE-RUNNING the acceptance
commands (the rubric/checklist that can be expressed as shell checks) and requiring exit 0. The LLM
verdict stays the soft signal; this is the hard, reproducible one.

Runs each command through the existing sandbox (offline-capable, approval-gated, redacted output) and
records the batch to eval_loop as a correctness signal. stdlib + sandbox + eval_loop; no network of
its own, no LLM. Used by loop_engineering.record_acceptance to stamp a request before complete.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .redact import redact_value
from .sandbox import execute as sandbox_execute

MAX_COMMANDS = 20
_CMD_LABEL_MAX = 200


def run_acceptance(
    root: Path,
    *,
    commands: list[str],
    timeout: int = 60,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Re-run acceptance commands; pass iff every command exits 0. Deterministic, fail-soft.

    Returns {ok, all_passed, count, results:[{command, exit_code, passed, exec_id}]}. An empty
    command list is `all_passed=False` (nothing was actually verified — never a silent pass).
    """
    cmds = [c for c in (commands or []) if isinstance(c, str) and c.strip()][:MAX_COMMANDS]
    results: list[dict[str, Any]] = []
    for cmd in cmds:
        out = sandbox_execute(root, command=cmd, cwd=cwd, timeout=int(timeout))
        exit_code = out.get("exit_code")
        passed = bool(out.get("ok")) and exit_code == 0
        results.append({
            "command": str(redact_value(cmd))[:_CMD_LABEL_MAX],
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "passed": passed,
            "exec_id": out.get("exec_id"),
            "reason": out.get("reason") if not passed else None,
        })
    all_passed = bool(results) and all(r["passed"] for r in results)
    # Record to the correctness eval ledger (best-effort; never breaks acceptance).
    try:
        from . import eval_loop
        eval_loop.record_case(
            root, kind="acceptance",
            command=f"{len(results)} acceptance commands",
            outcome="pass" if all_passed else "fail",
            duration_ms=0,
        )
    except Exception:
        pass
    return {"ok": True, "all_passed": all_passed, "count": len(results), "results": results}
