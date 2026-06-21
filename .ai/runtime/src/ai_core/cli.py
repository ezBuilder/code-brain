from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .doctor import as_payload, run_checks
from .hooks import codex_wire_output, handle_hook, read_payload
from .inbox import decide, list_approvals, request_approval
from .memory import append_audit, append_event, rebuild_audit_index
from .obs import diagnostics, health_summary, metrics, prune_diagnostics, search_report, slo_bench, usage_report, write_log
from .paths import find_repo_root
from .policy import CONFIG_INVALID, GENERIC_ERROR, MANIFEST_DRIFT, OK, PERMISSION_DENIED, PolicyDenied, WORKER_UNAVAILABLE, reject_ci_write
from .render import render
from .search import context_pack, query, rebuild
from .secrets_store import status as secrets_status
from .trust import init_machine, list_machines, revoke_machine

RUNTIME_PROTOCOL_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai",
        description="Code Brain repo-local AI agent infrastructure CLI.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--ci", action="store_true", help="force CI read-only policy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show")
    render_parser = sub.add_parser("render")
    render_parser.add_argument("--json", action="store_true", dest="command_json")
    render_parser.add_argument("--dry-run", action="store_true")
    render_parser.add_argument("--no-overwrite", action="store_true")
    render_parser.add_argument("--manifest-only", action="store_true")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true", dest="command_json")
    doctor_parser.add_argument("--strict", action="store_true")
    worker = sub.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_health = worker_sub.add_parser("health")
    worker_health.add_argument("--json", action="store_true", dest="command_json")
    worker_health.add_argument("--envelope-json")
    worker_status = worker_sub.add_parser("status")
    worker_status.add_argument("--json", action="store_true", dest="command_json")
    worker_stop = worker_sub.add_parser("stop")
    worker_stop.add_argument("--force", action="store_true")
    worker_stop.add_argument("--reason", default="operator")
    worker_stop.add_argument("--json", action="store_true", dest="command_json")
    queue = sub.add_parser("queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_enqueue = queue_sub.add_parser("enqueue")
    queue_enqueue.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], required=True)
    queue_enqueue.add_argument("--kind", required=True)
    queue_enqueue.add_argument("--max-attempts", type=int)
    queue_enqueue.add_argument("--json", action="store_true", dest="command_json")
    queue_lease = queue_sub.add_parser("lease")
    queue_lease.add_argument("--worker-id", required=True)
    queue_lease.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    queue_lease.add_argument("--json", action="store_true", dest="command_json")
    queue_complete = queue_sub.add_parser("complete")
    queue_complete.add_argument("--job-id", required=True)
    queue_complete.add_argument("--lease-id", required=True)
    queue_complete.add_argument("--json", action="store_true", dest="command_json")
    queue_fail = queue_sub.add_parser("fail")
    queue_fail.add_argument("--job-id", required=True)
    queue_fail.add_argument("--lease-id", required=True)
    queue_fail.add_argument("--reason", required=True)
    queue_fail.add_argument("--json", action="store_true", dest="command_json")
    queue_recover = queue_sub.add_parser("recover-expired")
    queue_recover.add_argument("--json", action="store_true", dest="command_json")
    queue_archive = queue_sub.add_parser("archive-dead")
    queue_archive.add_argument("--older-than-days", type=int, default=30)
    queue_archive.add_argument("--json", action="store_true", dest="command_json")
    queue_dead = queue_sub.add_parser("dead")
    queue_dead.add_argument("--limit", type=int, default=50)
    queue_dead.add_argument("--since")
    queue_dead.add_argument("--json", action="store_true", dest="command_json")
    queue_status_parser = queue_sub.add_parser("status")
    queue_status_parser.add_argument("--json", action="store_true", dest="command_json")
    loop = sub.add_parser("loop", help="file-based loop-engineering handoff between agents")
    loop_sub = loop.add_subparsers(dest="loop_command", required=True)
    loop_submit = loop_sub.add_parser("submit")
    loop_submit.add_argument("--goal", default="")
    loop_submit.add_argument("--file")
    loop_submit.add_argument("--text")
    loop_submit.add_argument("--rubric")
    loop_submit.add_argument("--rubric-file")
    loop_submit.add_argument("--checklist", action="append", default=[])
    loop_submit.add_argument("--source-agent", default="agent")
    loop_submit.add_argument("--target-agent", default="agent")
    loop_submit.add_argument("--role", default="worker")
    loop_submit.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P1")
    loop_submit.add_argument("--interval-seconds", type=int, default=300)
    loop_submit.add_argument("--no-review", action="store_true")
    loop_submit.add_argument("--require-acceptance", action="store_true", dest="require_acceptance",
                             help="gate complete on a passing deterministic acceptance re-run (G1)")
    loop_submit.add_argument("--json", action="store_true", dest="command_json")
    loop_claim = loop_sub.add_parser("claim")
    loop_claim.add_argument("--orchestrator-id", required=True)
    loop_claim.add_argument("--agent", default="agent")
    loop_claim.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    loop_claim.add_argument("--request-id", dest="request_id", default=None)
    loop_claim.add_argument("--lease-seconds", type=int, default=300)
    loop_claim.add_argument("--json", action="store_true", dest="command_json")
    loop_complete = loop_sub.add_parser("complete")
    loop_complete.add_argument("--request-id", required=True)
    loop_complete.add_argument("--lease-id", required=True)
    loop_complete.add_argument("--summary", required=True)
    loop_complete.add_argument("--result")
    loop_complete.add_argument("--result-file")
    loop_complete.add_argument("--json", action="store_true", dest="command_json")
    loop_fail = loop_sub.add_parser("fail")
    loop_fail.add_argument("--request-id", required=True)
    loop_fail.add_argument("--lease-id", required=True)
    loop_fail.add_argument("--reason", required=True)
    loop_fail.add_argument("--json", action="store_true", dest="command_json")
    loop_verdict = loop_sub.add_parser("verdict")
    loop_verdict.add_argument("--request-id", required=True)
    loop_verdict.add_argument("--lease-id", required=True)
    loop_verdict.add_argument("--reviewer", default="reviewer")
    loop_verdict.add_argument("--verdict", choices=["pass", "fail", "blocked"], required=True)
    loop_verdict.add_argument("--summary", required=True)
    loop_verdict.add_argument("--rubric-result")
    loop_verdict.add_argument("--rubric-result-file")
    loop_verdict.add_argument("--evidence-json", dest="evidence_json",
                              help="JSON array of {command,observed,artifact_path} typed evidence (G1)")
    loop_verdict.add_argument("--json", action="store_true", dest="command_json")
    loop_acceptance = loop_sub.add_parser("acceptance", help="deterministically re-run acceptance commands (G1)")
    loop_acceptance.add_argument("--request-id", required=True)
    loop_acceptance.add_argument("--lease-id", required=True)
    loop_acceptance.add_argument("--command", action="append", default=[], dest="acceptance_commands",
                                 help="acceptance command; repeatable (must exit 0)")
    loop_acceptance.add_argument("--timeout", type=int, default=60)
    loop_acceptance.add_argument("--json", action="store_true", dest="command_json")
    loop_distill = loop_sub.add_parser("distill")
    loop_distill.add_argument("--request-id", required=True)
    loop_distill.add_argument("--text")
    loop_distill.add_argument("--file")
    loop_distill.add_argument("--tag", action="append", default=[])
    loop_distill.add_argument("--force", action="store_true", help="override the contradiction gate after review")
    loop_distill.add_argument("--json", action="store_true", dest="command_json")
    loop_recover = loop_sub.add_parser("recover-expired")
    loop_recover.add_argument("--json", action="store_true", dest="command_json")
    loop_status = loop_sub.add_parser("status")
    loop_status.add_argument("--json", action="store_true", dest="command_json")
    plan = sub.add_parser("plan", help="durable per-plan step progress (checkbox = state)")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    plan_init = plan_sub.add_parser("init")
    plan_init.add_argument("--id", required=True, dest="plan_id")
    plan_init.add_argument("--title", default="")
    plan_init.add_argument("--step", action="append", default=[], dest="plan_steps", help="step label; repeatable")
    plan_init.add_argument("--force", action="store_true")
    plan_init.add_argument("--json", action="store_true", dest="command_json")
    plan_show = plan_sub.add_parser("show")
    plan_show.add_argument("--id", required=True, dest="plan_id")
    plan_show.add_argument("--json", action="store_true", dest="command_json")
    plan_check = plan_sub.add_parser("check")
    plan_check.add_argument("--id", required=True, dest="plan_id")
    plan_check.add_argument("--match", help="label substring")
    plan_check.add_argument("--index", type=int)
    plan_check.add_argument("--undo", action="store_true", help="mark not done")
    plan_check.add_argument("--json", action="store_true", dest="command_json")
    plan_list = plan_sub.add_parser("list")
    plan_list.add_argument("--json", action="store_true", dest="command_json")
    trust = sub.add_parser("trust")
    trust_sub = trust.add_subparsers(dest="trust_command", required=True)
    trust_init = trust_sub.add_parser("init")
    trust_init.add_argument("--name", required=True)
    trust_init.add_argument("--json", action="store_true", dest="command_json")
    trust_list = trust_sub.add_parser("list")
    trust_list.add_argument("--json", action="store_true", dest="command_json")
    trust_revoke = trust_sub.add_parser("revoke")
    trust_revoke.add_argument("machine_id_hash")
    trust_revoke.add_argument("--json", action="store_true", dest="command_json")
    secrets_parser = sub.add_parser("secrets")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    secrets_status_parser = secrets_sub.add_parser("status")
    secrets_status_parser.add_argument("--json", action="store_true", dest="command_json")
    inbox = sub.add_parser("inbox")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_request = inbox_sub.add_parser("request")
    inbox_request.add_argument("--gate", required=True)
    inbox_request.add_argument("--summary", required=True)
    inbox_request.add_argument("--ttl-hours", type=int, default=24)
    inbox_request.add_argument("--json", action="store_true", dest="command_json")
    inbox_list = inbox_sub.add_parser("list")
    inbox_list.add_argument("--json", action="store_true", dest="command_json")
    inbox_approve = inbox_sub.add_parser("approve")
    inbox_approve.add_argument("approval_id")
    inbox_approve.add_argument("--json", action="store_true", dest="command_json")
    inbox_reject = inbox_sub.add_parser("reject")
    inbox_reject.add_argument("approval_id")
    inbox_reject.add_argument("--json", action="store_true", dest="command_json")
    notify = sub.add_parser("notify")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    notify_enqueue = notify_sub.add_parser("enqueue")
    notify_enqueue.add_argument("--channel", required=True)
    notify_enqueue.add_argument("--json", action="store_true", dest="command_json")
    pgrowth = sub.add_parser("prompt-growth")
    pgrowth_sub = pgrowth.add_subparsers(dest="prompt_growth_command", required=True)
    pgrowth_status = pgrowth_sub.add_parser("status")
    pgrowth_status.add_argument("--json", action="store_true", dest="command_json")

    si = sub.add_parser("selfimprove")
    si_sub = si.add_subparsers(dest="selfimprove_command", required=True)
    si_run = si_sub.add_parser("run")
    si_run.add_argument("--tier", choices=["cheap", "balanced", "best"], default="cheap")
    si_run.add_argument("--json", action="store_true", dest="command_json")
    si_propose = si_sub.add_parser("propose")
    si_propose.add_argument("--text", required=True)
    si_propose.add_argument("--rationale", default="")
    si_propose.add_argument("--json", action="store_true", dest="command_json")
    si_status = si_sub.add_parser("status")
    si_status.add_argument("--json", action="store_true", dest="command_json")

    loopd_p = sub.add_parser("loopd")
    loopd_sub = loopd_p.add_subparsers(dest="loopd_command", required=True)
    for _name in ("status", "dispatch-once", "recover", "agents"):
        _sp = loopd_sub.add_parser(_name)
        _sp.add_argument("--json", action="store_true", dest="command_json")
    ld_launch = loopd_sub.add_parser("launch")
    ld_launch.add_argument("--worker-id", required=True, dest="worker_id")
    ld_launch.add_argument("--agent", required=True)
    ld_launch.add_argument("--profile", default="")
    ld_launch.add_argument("--inherit-auth", action="store_true", dest="inherit_auth")
    ld_launch.add_argument("--autonomous", action="store_true", dest="autonomous")
    ld_launch.add_argument("--tier", choices=["cheap", "balanced", "best"], default=None)
    ld_launch.add_argument("--dry-run", action="store_true", dest="dry_run")
    ld_launch.add_argument("--json", action="store_true", dest="command_json")
    ld_up = loopd_sub.add_parser("up")
    ld_up.add_argument("--autonomous", action="store_true", dest="autonomous")
    ld_up.add_argument("--tier", choices=["cheap", "balanced", "best"], default=None)
    ld_up.add_argument("--dry-run", action="store_true", dest="dry_run")
    ld_up.add_argument("--json", action="store_true", dest="command_json")
    acct_sub = loopd_sub.add_parser("account").add_subparsers(dest="account_command", required=True)
    a_add = acct_sub.add_parser("add")
    a_add.add_argument("--agent", required=True, choices=["codex", "claude", "agy"])
    a_add.add_argument("--account", required=True)
    a_add.add_argument("--json", action="store_true", dest="command_json")
    a_login = acct_sub.add_parser("login")
    a_login.add_argument("--agent", required=True, choices=["codex", "claude", "agy"])
    a_login.add_argument("--account", required=True)
    a_login.add_argument("--json", action="store_true", dest="command_json")
    a_list = acct_sub.add_parser("list")
    a_list.add_argument("--agent", default=None, choices=["codex", "claude", "agy"])
    a_list.add_argument("--json", action="store_true", dest="command_json")
    models_sub = loopd_sub.add_parser("models").add_subparsers(dest="models_command", required=True)
    m_list = models_sub.add_parser("list")
    m_list.add_argument("--json", action="store_true", dest="command_json")
    m_set = models_sub.add_parser("set")
    m_set.add_argument("--agent", required=True, choices=["codex", "claude", "agy"])
    m_set.add_argument("--model", required=True)
    m_set.add_argument("--reasoning", default="high")
    m_set.add_argument("--flag", action="append", default=[], dest="model_flags")
    m_set.add_argument("--json", action="store_true", dest="command_json")

    worker_sub = loopd_sub.add_parser("worker").add_subparsers(dest="worker_command", required=True)
    w_reg = worker_sub.add_parser("register")
    w_reg.add_argument("--worker-id", required=True, dest="worker_id")
    w_reg.add_argument("--agent", required=True)
    w_reg.add_argument("--profile", default="")
    w_reg.add_argument("--pane-id", default="", dest="pane_id")
    w_reg.add_argument("--cwd", default="")
    w_reg.add_argument("--state", default="booting")
    w_reg.add_argument("--json", action="store_true", dest="command_json")
    w_list = worker_sub.add_parser("list")
    w_list.add_argument("--state", default=None)
    w_list.add_argument("--json", action="store_true", dest="command_json")
    w_hb = worker_sub.add_parser("heartbeat")
    w_hb.add_argument("--worker-id", required=True, dest="worker_id")
    w_hb.add_argument("--state", required=True)
    w_hb.add_argument("--request-id", default=None, dest="request_id")
    w_hb.add_argument("--json", action="store_true", dest="command_json")
    w_prof = worker_sub.add_parser("profile")
    w_prof_sub = w_prof.add_subparsers(dest="worker_profile_command", required=True)
    w_prof_reg = w_prof_sub.add_parser("register")
    w_prof_reg.add_argument("--profile", required=True)
    w_prof_reg.add_argument("--agent", required=True)
    w_prof_reg.add_argument("--worker-id", default="", dest="worker_id")
    w_prof_reg.add_argument("--json", action="store_true", dest="command_json")
    w_prof_list = w_prof_sub.add_parser("list")
    w_prof_list.add_argument("--json", action="store_true", dest="command_json")

    obs = sub.add_parser("obs")
    obs_sub = obs.add_subparsers(dest="obs_command", required=True)
    obs_log = obs_sub.add_parser("log")
    obs_log.add_argument("--level", default="info")
    obs_log.add_argument("--event", required=True)
    obs_log.add_argument("--json", action="store_true", dest="command_json")
    obs_metrics = obs_sub.add_parser("metrics")
    obs_metrics.add_argument("--json", action="store_true", dest="command_json")
    obs_search = obs_sub.add_parser("search")
    obs_search.add_argument("--query")
    obs_search.add_argument("--limit", type=int, default=5)
    obs_search.add_argument("--refresh-stale", action="store_true", dest="refresh_stale",
                            help="rebuild index before query if any result would be stale (write op)")
    obs_search.add_argument("--json", action="store_true", dest="command_json")
    obs_usage = obs_sub.add_parser("usage")
    obs_usage.add_argument("--include-sessions", action="store_true", dest="include_sessions")
    obs_usage.add_argument("--json", action="store_true", dest="command_json")
    obs_slo = obs_sub.add_parser("slo")
    obs_slo.add_argument("--iterations", type=int, default=10)
    obs_slo.add_argument("--json", action="store_true", dest="command_json")
    obs_health = obs_sub.add_parser("health-summary")
    obs_health.add_argument("--json", action="store_true", dest="command_json")
    obs_traj = obs_sub.add_parser("trajectory", help="TRAJEVAL-style trajectory diagnosis")
    obs_traj.add_argument("--session-id", default=None)
    obs_traj.add_argument("--limit", type=int, default=10)
    obs_traj.add_argument("--json", action="store_true", dest="command_json")
    obs_spec = obs_sub.add_parser("speculative", help="PASTE-style pattern mining + hit rate")
    obs_spec.add_argument("--min-support", type=int, default=3)
    obs_spec.add_argument("--min-confidence", type=float, default=0.5)
    obs_spec.add_argument("--hit-rate", action="store_true", help="Show hit rate only")
    obs_spec.add_argument("--json", action="store_true", dest="command_json")
    diagnostics_parser = sub.add_parser("diagnostics")
    diagnostics_sub = diagnostics_parser.add_subparsers(dest="diagnostics_command", required=True)
    diagnostics_bundle = diagnostics_sub.add_parser("bundle")
    diagnostics_bundle.add_argument("--dry-run", action="store_true")
    diagnostics_bundle.add_argument("--json", action="store_true", dest="command_json")
    diagnostics_prune = diagnostics_sub.add_parser("prune")
    diagnostics_prune.add_argument("--keep-days", type=int, default=30)
    diagnostics_prune.add_argument("--json", action="store_true", dest="command_json")
    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.add_argument("--json", action="store_true", dest="command_json")
    upgrade = sub.add_parser("upgrade")
    upgrade_sub = upgrade.add_subparsers(dest="upgrade_command", required=True)
    upgrade_plan_parser = upgrade_sub.add_parser("plan")
    upgrade_plan_parser.add_argument("--target-version", required=True)
    upgrade_plan_parser.add_argument("--json", action="store_true", dest="command_json")
    upgrade_apply_parser = upgrade_sub.add_parser("apply")
    upgrade_apply_parser.add_argument("--target-version", required=True)
    upgrade_apply_parser.add_argument("--dry-run", action="store_true")
    upgrade_apply_parser.add_argument("--json", action="store_true", dest="command_json")
    upgrade_rollback = upgrade_sub.add_parser("rollback")
    upgrade_rollback.add_argument("--backup-path", required=True)
    upgrade_rollback.add_argument("--json", action="store_true", dest="command_json")
    upgrade_clean = upgrade_sub.add_parser("clean-cache")
    upgrade_clean.add_argument("--json", action="store_true", dest="command_json")
    upgrade_latest_parser = upgrade_sub.add_parser("latest", help="upgrade this install from the public Code Brain repo")
    upgrade_latest_parser.add_argument("--repo-url", default=None)
    upgrade_latest_parser.add_argument("--ref", default=None)
    upgrade_latest_parser.add_argument("--dry-run", action="store_true")
    upgrade_latest_parser.add_argument("--keep-clone", action="store_true")
    upgrade_latest_parser.add_argument("--json", action="store_true", dest="command_json")
    hook_parser = sub.add_parser("hook")
    hook_parser.add_argument("hook_name", nargs="?")
    hook_parser.add_argument("--json", action="store_true", dest="command_json")
    memory = sub.add_parser("memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_append_event = memory_sub.add_parser("append-event")
    memory_append_event.add_argument("--json", action="store_true", dest="command_json")
    memory_evidence = memory_sub.add_parser("evidence", help="repo-local evidence ledger for search/context results")
    memory_evidence_sub = memory_evidence.add_subparsers(dest="memory_evidence_command", required=True)
    memory_evidence_list = memory_evidence_sub.add_parser("list")
    memory_evidence_list.add_argument("--status", choices=["candidate", "curated", "verified", "rejected"])
    memory_evidence_list.add_argument("--limit", type=int, default=20)
    memory_evidence_list.add_argument("--json", action="store_true", dest="command_json")
    memory_evidence_status = memory_evidence_sub.add_parser("set-status")
    memory_evidence_status.add_argument("--id", required=True)
    memory_evidence_status.add_argument("--status", required=True, choices=["candidate", "curated", "verified", "rejected"])
    memory_evidence_status.add_argument("--note", default="")
    memory_evidence_status.add_argument("--source", default="operator")
    memory_evidence_status.add_argument("--json", action="store_true", dest="command_json")
    memory_decision = memory_sub.add_parser("decision")
    memory_decision_sub = memory_decision.add_subparsers(dest="memory_decision_command", required=True)
    memory_decision_add = memory_decision_sub.add_parser("add")
    memory_decision_add.add_argument("--text", required=True)
    memory_decision_add.add_argument("--tag", action="append", default=[])
    memory_decision_add.add_argument("--source", default="operator")
    memory_decision_add.add_argument("--kind", choices=["decision", "failure"])
    memory_decision_add.add_argument("--observed-at", dest="observed_at")
    memory_decision_add.add_argument("--observed-version", action="append", default=[], dest="observed_version",
                                     help="key=value; repeatable (failure only)")
    memory_decision_add.add_argument("--environment")
    memory_decision_add.add_argument("--retest-after", dest="retest_after")
    memory_decision_add.add_argument("--status", choices=["observed", "confirmed", "stale", "refuted"])
    memory_decision_add.add_argument("--supersedes-id", dest="supersedes_id")
    memory_decision_add.add_argument("--contradicts", dest="contradicts",
                                     help="id of a decision this one contradicts (dec-...)")
    memory_decision_add.add_argument("--derives-from", dest="derives_from",
                                     help="id of a decision this one derives from (dec-...)")
    memory_decision_add.add_argument("--expires-at", dest="expires_at",
                                     help="ISO date/time after which this decision is retired")
    memory_decision_add.add_argument("--json", action="store_true", dest="command_json")
    memory_decision_list = memory_decision_sub.add_parser("list", help="filtered on-demand read of decisions/failures")
    memory_decision_list.add_argument("--kind", choices=["decision", "failure"])
    memory_decision_list.add_argument("--status", choices=["observed", "confirmed", "stale", "refuted"])
    memory_decision_list.add_argument("--tag", help="match any tag (substring)")
    memory_decision_list.add_argument("--source", help="substring match on source")
    memory_decision_list.add_argument("--text", help="substring match on decision text")
    memory_decision_list.add_argument("--limit", type=int, default=20)
    memory_decision_list.add_argument("--include-retired", action="store_true", dest="include_retired")
    memory_decision_list.add_argument("--json", action="store_true", dest="command_json")
    memory_recall = memory_sub.add_parser("recall", help="unified recall across decisions/failures/lessons/procedures")
    memory_recall.add_argument("--query", required=True)
    memory_recall.add_argument("--limit", type=int, default=8)
    memory_recall.add_argument("--type", action="append", default=None, dest="recall_types",
                               choices=["decision", "failure", "lesson", "procedure"], help="repeatable; default all")
    memory_recall.add_argument("--json", action="store_true", dest="command_json")
    memory_conflicts = memory_sub.add_parser("conflicts", help="advisory: scan/list contradicting decision pairs")
    memory_conflicts.add_argument("--scan", action="store_true", help="run a fresh scan (writes conflicts.jsonl)")
    memory_conflicts.add_argument("--dry-run", action="store_true", dest="dry_run", help="scan but do not write")
    memory_conflicts.add_argument("--limit", type=int, default=20)
    memory_conflicts.add_argument("--json", action="store_true", dest="command_json")
    memory_todo = memory_sub.add_parser("todo")
    memory_todo_sub = memory_todo.add_subparsers(dest="memory_todo_command", required=True)
    memory_todo_add = memory_todo_sub.add_parser("add")
    memory_todo_add.add_argument("--title", required=True)
    memory_todo_add.add_argument("--owner", default="")
    memory_todo_add.add_argument("--tag", action="append", default=[])
    memory_todo_add.add_argument("--source", default="operator")
    memory_todo_add.add_argument("--json", action="store_true", dest="command_json")
    memory_todo_close = memory_todo_sub.add_parser("close")
    memory_todo_close.add_argument("--match", required=True, help="todo id or title substring")
    memory_todo_close.add_argument("--status", default="done", choices=["done", "closed", "cancelled", "canceled"])
    memory_todo_close.add_argument("--reason", default="")
    memory_todo_close.add_argument("--json", action="store_true", dest="command_json")
    memory_session = memory_sub.add_parser("session")
    memory_session_sub = memory_session.add_subparsers(dest="memory_session_command", required=True)
    memory_session_append = memory_session_sub.add_parser("append")
    memory_session_append.add_argument("--text", required=True)
    memory_session_append.add_argument("--json", action="store_true", dest="command_json")
    memory_handoff = memory_sub.add_parser(
        "handoff", help="set the resume handoff (goal/plan/next-step) that travels Mac↔VPS"
    )
    memory_handoff.add_argument("--goal")
    memory_handoff.add_argument("--next-step", dest="next_step")
    memory_handoff.add_argument("--plan", action="append", default=None, help="repeatable")
    memory_handoff.add_argument("--open-question", action="append", default=None, dest="open_questions", help="repeatable")
    memory_handoff.add_argument("--blocker", action="append", default=None, dest="blockers", help="repeatable")
    memory_handoff.add_argument("--agent", default="operator")
    memory_handoff.add_argument("--clear", action="store_true", help="wipe the current handoff")
    memory_handoff.add_argument("--json", action="store_true", dest="command_json")
    memory_sync = memory_sub.add_parser(
        "sync",
        help="opt-in: commit .ai/memory only + pull --rebase (clean tree) + push — bounce work Mac↔VPS. NEVER on the hot path.",
    )
    memory_sync.add_argument("--agent", default="operator")
    memory_sync.add_argument("--no-push", action="store_true", help="commit/pull only; do not push")
    memory_sync.add_argument("--loop", type=int, default=0, help="daemon mode: sync every N seconds (>=30); run under systemd/launchd")
    memory_sync.add_argument("--json", action="store_true", dest="command_json")
    memory_tier = memory_sub.add_parser("tier", help="MemGPT-style hot/warm/cold classification (T30)")
    memory_tier.add_argument("--json", action="store_true", dest="command_json")
    memory_pressure = memory_sub.add_parser("pressure", help="hot-tier pressure (page-out signal)")
    memory_pressure.add_argument("--json", action="store_true", dest="command_json")
    memory_pageout = memory_sub.add_parser("page-out", help="rotate session + archive old sessions (T30 step B)")
    memory_pageout.add_argument("--dry-run", action="store_true")
    memory_pageout.add_argument("--json", action="store_true", dest="command_json")
    memory_pagein = memory_sub.add_parser("page-in", help="consolidate ranked HOT cache for SessionStart (T30 step C)")
    memory_pagein.add_argument("--dry-run", action="store_true")
    memory_pagein.add_argument("--limit", type=int, default=None)
    memory_pagein.add_argument("--json", action="store_true", dest="command_json")
    memory_retention = memory_sub.add_parser("retention", help="retention scoring (decay+reinforcement) of durable memory")
    memory_retention.add_argument("--evict-limit", type=int, default=50)
    memory_retention.add_argument("--json", action="store_true", dest="command_json")
    lessons = sub.add_parser("lessons")
    lessons_sub = lessons.add_subparsers(dest="lessons_command", required=True)
    lessons_add = lessons_sub.add_parser("add")
    lessons_add.add_argument("--source", default="operator")
    lessons_add.add_argument("--failure", required=True)
    lessons_add.add_argument("--cause", required=True)
    lessons_add.add_argument("--fix", required=True)
    lessons_add.add_argument("--tag", action="append", default=[])
    lessons_add.add_argument("--json", action="store_true", dest="command_json")
    lessons_list = lessons_sub.add_parser("list")
    lessons_list.add_argument("--limit", type=int, default=20)
    lessons_list.add_argument("--json", action="store_true", dest="command_json")
    lessons_summary = lessons_sub.add_parser("summary")
    lessons_summary.add_argument("--json", action="store_true", dest="command_json")
    lessons_score = lessons_sub.add_parser("score", help="confidence/decay scoring of lessons")
    lessons_score.add_argument("--include-stale", action="store_true")
    lessons_score.add_argument("--json", action="store_true", dest="command_json")
    lessons_recall = lessons_sub.add_parser("recall", help="rank lessons for a query (confidence*relevance*recency)")
    lessons_recall.add_argument("--query", required=True)
    lessons_recall.add_argument("--limit", type=int, default=10)
    lessons_recall.add_argument("--include-stale", action="store_true")
    lessons_recall.add_argument("--json", action="store_true", dest="command_json")
    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_append = audit_sub.add_parser("append")
    audit_append.add_argument("--action", required=True)
    audit_append.add_argument("--category", default="manual")
    audit_append.add_argument("--json", action="store_true", dest="command_json")
    audit_rebuild = audit_sub.add_parser("rebuild-index")
    audit_rebuild.add_argument("--json", action="store_true", dest="command_json")
    audit_repair = audit_sub.add_parser("repair-chain", help="re-compute prev_sha for mis-chained audit records (after stash/merge artifact)")
    audit_repair.add_argument("--year", type=int, default=None, help="repair a specific year file only")
    audit_repair.add_argument("--json", action="store_true", dest="command_json")
    exec_parser = sub.add_parser("exec", help="run a shell command in Code Brain sandbox (truncated summary, fetchable by id)")
    exec_sub = exec_parser.add_subparsers(dest="exec_command", required=True)
    exec_run = exec_sub.add_parser("run")
    exec_run.add_argument("--cwd")
    exec_run.add_argument("--timeout", type=int, default=30)
    exec_run.add_argument("--json", action="store_true", dest="command_json")
    exec_run.add_argument("argv", nargs=argparse.REMAINDER, help="command and arguments after --")
    exec_fetch = exec_sub.add_parser("fetch")
    exec_fetch.add_argument("--exec-id", required=True)
    exec_fetch.add_argument("--line-start", type=int, default=1)
    exec_fetch.add_argument("--line-end", type=int)
    exec_fetch.add_argument("--grep")
    exec_fetch.add_argument("--json", action="store_true", dest="command_json")
    exec_list = exec_sub.add_parser("list")
    exec_list.add_argument("--limit", type=int, default=20)
    exec_list.add_argument("--json", action="store_true", dest="command_json")
    exec_prune = exec_sub.add_parser("prune")
    exec_prune.add_argument("--older-than-seconds", type=int, default=86400)
    exec_prune.add_argument("--json", action="store_true", dest="command_json")
    index = sub.add_parser("index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_rebuild = index_sub.add_parser("rebuild")
    index_rebuild.add_argument("--json", action="store_true", dest="command_json")
    index_rebuild.add_argument(
        "--single-flight",
        action="store_true",
        dest="single_flight",
        help="non-blocking flock on .ai/cache/.rebuild.lock; skip if another rebuild is in progress",
    )
    index_rebuild.add_argument(
        "--incremental",
        action="store_true",
        help="reindex only files whose redacted-content sha256 changed (T33)",
    )
    recommend_parser = sub.add_parser("recommend")
    recommend_sub = recommend_parser.add_subparsers(dest="recommend_command", required=True)
    recommend_skills = recommend_sub.add_parser("skills")
    recommend_skills_sub = recommend_skills.add_subparsers(dest="recommend_skills_command", required=False)
    recommend_skills.add_argument("--limit", type=int, default=5)
    recommend_skills.add_argument("--no-global", action="store_true", dest="no_global")
    recommend_skills.add_argument("--min-signal", type=int, default=3, dest="min_signal")
    recommend_skills.add_argument("--json", action="store_true", dest="command_json")
    recommend_skills.add_argument("--compact", action="store_true", dest="compact")
    rec_accept = recommend_skills_sub.add_parser("accept")
    rec_accept.add_argument("candidate_id")
    rec_accept.add_argument("--json", action="store_true", dest="command_json")
    rec_reject = recommend_skills_sub.add_parser("reject")
    rec_reject.add_argument("candidate_id")
    rec_reject.add_argument("--json", action="store_true", dest="command_json")

    skills_parser = sub.add_parser("skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list")
    skills_list.add_argument("--json", action="store_true", dest="command_json")
    skills_uninstall = skills_sub.add_parser("uninstall")
    skills_uninstall.add_argument("slug")
    skills_uninstall.add_argument("--force", action="store_true")
    skills_uninstall.add_argument("--json", action="store_true", dest="command_json")

    precall_parser = sub.add_parser("precall")
    precall_sub = precall_parser.add_subparsers(dest="precall_command", required=True)
    pc_list = precall_sub.add_parser("list")
    pc_list.add_argument("--json", action="store_true", dest="command_json")
    pc_recommend = precall_sub.add_parser("recommend")
    pc_recommend.add_argument("--limit", type=int, default=5)
    pc_recommend.add_argument("--min-signal", type=int, default=5, dest="min_signal")
    pc_recommend.add_argument("--include-transcripts", action="store_true", dest="include_transcripts")
    pc_recommend.add_argument("--json", action="store_true", dest="command_json")
    pc_accept = precall_sub.add_parser("accept")
    pc_accept.add_argument("candidate_id")
    pc_accept.add_argument("--json", action="store_true", dest="command_json")
    pc_activate = precall_sub.add_parser("activate")
    pc_activate.add_argument("candidate_id")
    pc_activate.add_argument("--force", action="store_true")
    pc_activate.add_argument("--json", action="store_true", dest="command_json")
    pc_reject = precall_sub.add_parser("reject")
    pc_reject.add_argument("candidate_id")
    pc_reject.add_argument("--json", action="store_true", dest="command_json")
    pc_disable = precall_sub.add_parser("disable")
    pc_disable.add_argument("candidate_id")
    pc_disable.add_argument("--json", action="store_true", dest="command_json")

    federated_parser = sub.add_parser("federated")
    federated_sub = federated_parser.add_subparsers(dest="federated_command", required=True)
    fed_summary = federated_sub.add_parser("summary")
    fed_summary.add_argument("--json", action="store_true", dest="command_json")

    eval_parser = sub.add_parser("eval")
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_record = eval_sub.add_parser("record")
    eval_record.add_argument("--id", dest="case_id")
    eval_record.add_argument("--kind", required=True)
    eval_record.add_argument("--command", required=True, dest="eval_case_command")
    eval_record.add_argument("--outcome", required=True)
    eval_record.add_argument("--duration-ms", required=True, type=int, dest="duration_ms")
    eval_record.add_argument("--json", action="store_true", dest="command_json")
    eval_summary = eval_sub.add_parser("summary")
    eval_summary.add_argument("--latest", type=int, default=5)
    eval_summary.add_argument("--json", action="store_true", dest="command_json")

    embedding_parser = sub.add_parser("embedding", help="dense semantic-search model management (opt-in)")
    embedding_sub = embedding_parser.add_subparsers(dest="embedding_command", required=True)
    emb_status = embedding_sub.add_parser("status", help="show dense embedding readiness")
    emb_status.add_argument("--json", action="store_true", dest="command_json")
    emb_install = embedding_sub.add_parser("install", help="download ONNX MiniLM model (~25MB, one-shot)")
    emb_install.add_argument("--verify", action="store_true", help="only check presence")
    emb_install.add_argument("--json", action="store_true", dest="command_json")
    emb_uninstall = embedding_sub.add_parser("uninstall", help="remove cached model files")
    emb_uninstall.add_argument("--json", action="store_true", dest="command_json")

    agents_parser = sub.add_parser("agents")
    agents_sub = agents_parser.add_subparsers(dest="agents_command", required=True)
    ag_recommend = agents_sub.add_parser("recommend")
    ag_recommend.add_argument("--limit", type=int, default=5)
    ag_recommend.add_argument("--min-signal", type=int, default=3, dest="min_signal")
    ag_recommend.add_argument("--json", action="store_true", dest="command_json")
    ag_accept = agents_sub.add_parser("accept")
    ag_accept.add_argument("candidate_id")
    ag_accept.add_argument("--json", action="store_true", dest="command_json")
    ag_reject = agents_sub.add_parser("reject")
    ag_reject.add_argument("candidate_id")
    ag_reject.add_argument("--json", action="store_true", dest="command_json")
    ag_list = agents_sub.add_parser("list")
    ag_list.add_argument("--json", action="store_true", dest="command_json")
    ag_uninstall = agents_sub.add_parser("uninstall")
    ag_uninstall.add_argument("slug")
    ag_uninstall.add_argument("--force", action="store_true")
    ag_uninstall.add_argument("--json", action="store_true", dest="command_json")

    # remote-memory CLI removed (T37) — .ai/ git sync covers cross-device.
    code = sub.add_parser("code")
    code_sub = code.add_subparsers(dest="code_command", required=True)
    code_query = code_sub.add_parser("query")
    code_query.add_argument("query")
    code_query.add_argument("--limit", type=int, default=5)
    code_query.add_argument("--json", action="store_true", dest="command_json")
    code_graph = code_sub.add_parser("graph", help="function-call graph queries (T29 step C)")
    code_graph_sub = code_graph.add_subparsers(dest="graph_command", required=True)
    cg_callers = code_graph_sub.add_parser("callers", help="who calls this function?")
    cg_callers.add_argument("qualname")
    cg_callers.add_argument("--limit", type=int, default=20)
    cg_callers.add_argument("--json", action="store_true", dest="command_json")
    cg_callees = code_graph_sub.add_parser("callees", help="what does this function call?")
    cg_callees.add_argument("qualname")
    cg_callees.add_argument("--limit", type=int, default=20)
    cg_callees.add_argument("--json", action="store_true", dest="command_json")
    cg_symbol = code_graph_sub.add_parser("symbol", help="locate symbol(s) by name fragment")
    cg_symbol.add_argument("name")
    cg_symbol.add_argument("--limit", type=int, default=20)
    cg_symbol.add_argument("--json", action="store_true", dest="command_json")
    cg_hotspots = code_graph_sub.add_parser("hotspots", help="most-called callees in the index")
    cg_hotspots.add_argument("--limit", type=int, default=20)
    cg_hotspots.add_argument("--json", action="store_true", dest="command_json")
    code_verify = code_sub.add_parser("verify", help="AST-based policy gate (T31)")
    code_verify.add_argument("source", nargs="?", help="file path; omit to read from stdin")
    code_verify.add_argument("--stdin", action="store_true")
    code_verify.add_argument("--json", action="store_true", dest="command_json")
    code_hashline = code_sub.add_parser("read-hashline", help="read file with line+hash anchors")
    code_hashline.add_argument("path")
    code_hashline.add_argument("--start", type=int)
    code_hashline.add_argument("--end", type=int)
    code_hashline.add_argument("--json", action="store_true", dest="command_json")
    code_verify_hashline = code_sub.add_parser("verify-hashline", help="verify line+hash anchors from JSON stdin")
    code_verify_hashline.add_argument("path")
    code_verify_hashline.add_argument("--json", action="store_true", dest="command_json")
    code_map = code_sub.add_parser("map", help="live top-level codebase map with local commands")
    code_map.add_argument("--limit", type=int, default=40)
    code_map.add_argument("--json", action="store_true", dest="command_json")
    guard = sub.add_parser("guard")
    guard_sub = guard.add_subparsers(dest="guard_command", required=True)
    guard_scan = guard_sub.add_parser("scan")
    guard_scan.add_argument("--text")
    guard_scan.add_argument("--scope", choices=["tool", "prompt", "output"], default="tool")
    guard_scan.add_argument("--stdin", action="store_true")
    guard_scan.add_argument("--json", action="store_true", dest="command_json")
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_pack_parser = context_sub.add_parser("pack")
    context_pack_parser.add_argument("query")
    context_pack_parser.add_argument("--limit", type=int, default=5)
    context_pack_parser.add_argument("--mode", choices=["high_fidelity", "balanced", "aggressive"], default="balanced")
    context_pack_parser.add_argument("--json", action="store_true", dest="command_json")
    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_record = evidence_sub.add_parser("record")
    evidence_record.add_argument("--query", required=True)
    evidence_record.add_argument("--path", required=True)
    evidence_record.add_argument("--status", choices=["candidate", "curated", "verified", "rejected"], default="candidate")
    evidence_record.add_argument("--snippet", default="")
    evidence_record.add_argument("--source", default="agent")
    evidence_record.add_argument("--note", default="")
    evidence_record.add_argument("--json", action="store_true", dest="command_json")
    evidence_update = evidence_sub.add_parser("update")
    evidence_update.add_argument("--id", required=True)
    evidence_update.add_argument("--status", choices=["candidate", "curated", "verified", "rejected"], required=True)
    evidence_update.add_argument("--note", default="")
    evidence_update.add_argument("--source", default="agent")
    evidence_update.add_argument("--json", action="store_true", dest="command_json")
    evidence_list = evidence_sub.add_parser("list")
    evidence_list.add_argument("--status", choices=["candidate", "curated", "verified", "rejected"])
    evidence_list.add_argument("--query")
    evidence_list.add_argument("--limit", type=int, default=50)
    evidence_list.add_argument("--json", action="store_true", dest="command_json")
    security = sub.add_parser("security")
    security_sub = security.add_subparsers(dest="security_command", required=True)
    security_finding = security_sub.add_parser("finding")
    security_finding_sub = security_finding.add_subparsers(dest="security_finding_command", required=True)
    security_finding_record = security_finding_sub.add_parser("record")
    security_finding_record.add_argument("--affected-path", required=True)
    security_finding_record.add_argument("--type", required=True, dest="finding_type")
    security_finding_record.add_argument("--detail-summary", required=True)
    security_finding_record.add_argument("--evidence-hash", default="")
    security_finding_record.add_argument("--repro-command", required=True)
    security_finding_record.add_argument("--verification-command", required=True)
    security_finding_record.add_argument("--status", choices=["open", "verified_fixed", "accepted_risk", "false_positive"], default="open")
    security_finding_record.add_argument("--source", default="agent")
    security_finding_record.add_argument("--json", action="store_true", dest="command_json")
    security_finding_update = security_finding_sub.add_parser("update")
    security_finding_update.add_argument("--id", required=True)
    security_finding_update.add_argument("--status", choices=["open", "verified_fixed", "accepted_risk", "false_positive"], required=True)
    security_finding_update.add_argument("--verification-command", required=True)
    security_finding_update.add_argument("--source", default="agent")
    security_finding_update.add_argument("--json", action="store_true", dest="command_json")
    security_finding_list = security_finding_sub.add_parser("list")
    security_finding_list.add_argument("--status", choices=["open", "verified_fixed", "accepted_risk", "false_positive"])
    security_finding_list.add_argument("--limit", type=int, default=50)
    security_finding_list.add_argument("--json", action="store_true", dest="command_json")
    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_start = session_sub.add_parser("start")
    session_start.add_argument("--agent", default="operator")
    session_start.add_argument("--rebuild", choices=["auto", "always", "never"], default="auto")
    session_start.add_argument("--dry-run", action="store_true")
    session_start.add_argument("--strict", action="store_true")
    session_start.add_argument("--query")
    session_start.add_argument("--limit", type=int, default=5)
    session_start.add_argument("--mode", choices=["high_fidelity", "balanced", "aggressive"], default="balanced")
    session_start.add_argument("--json", action="store_true", dest="command_json")
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--once-json")
    release_gate = sub.add_parser("release-gate")
    release_gate_sub = release_gate.add_subparsers(dest="release_gate_command", required=True)
    release_gate_summary = release_gate_sub.add_parser("summary")
    release_gate_summary.add_argument("--json", action="store_true", dest="command_json")
    kit = sub.add_parser("kit")
    kit_sub = kit.add_subparsers(dest="kit_command", required=True)
    kit_status = kit_sub.add_parser("status")
    kit_status.add_argument("--json", action="store_true", dest="command_json")
    kit_validate = kit_sub.add_parser("validate")
    kit_validate.add_argument("--json", action="store_true", dest="command_json")
    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_insights = runtime_sub.add_parser("insights")
    runtime_insights.add_argument("--json", action="store_true", dest="command_json")
    runtime_policy = runtime_sub.add_parser("context-policy")
    runtime_policy.add_argument("--json", action="store_true", dest="command_json")
    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_status = report_sub.add_parser("status")
    report_status.add_argument("--json", action="store_true", dest="command_json")
    report_sub.add_parser("release-notes")
    report_summary = report_sub.add_parser("release-gate-summary")
    report_summary.add_argument("--git-sha")
    report_summary.add_argument("--json", action="store_true", dest="command_json")
    return parser


def emit(payload: object, *, as_json: bool) -> None:
    def _write(text: str) -> None:
        try:
            print(text)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="backslashreplace"))
    if as_json:
        _write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            _write(f"{key}: {value}")
    else:
        _write(str(payload))


def _read_loop_text(root: Path, *, text: str | None, file_path: str | None, stdin_fallback: bool = False) -> str:
    if text is not None:
        return text
    if file_path:
        raw_path = Path(file_path)
        path = raw_path if raw_path.is_absolute() else root / raw_path
        resolved = path.resolve()
        root_resolved = root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise ValueError("loop file must be inside the repository root")
        if not resolved.is_file():
            raise ValueError(f"loop file not found: {file_path}")
        return resolved.read_text(encoding="utf-8")
    if stdin_fallback and not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("provide --text or --file")


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(args.json or getattr(args, "command_json", False))
    try:
        if args.ci:
            os.environ["AI_CI"] = "1"
        root = find_repo_root()
        if args.command == "version":
            emit({"version": __version__, "protocol_version": RUNTIME_PROTOCOL_VERSION}, as_json=as_json)
            return OK
        if args.command == "config" and args.config_command == "show":
            emit(load_config(root), as_json=as_json)
            return OK
        if args.command == "render":
            reject_ci_write("render", dry_run=args.dry_run)
            result = render(root, dry_run=args.dry_run, no_overwrite=args.no_overwrite, manifest_only=args.manifest_only)
            emit(result, as_json=as_json)
            return OK
        if args.command == "doctor":
            checks = run_checks(root)
            payload = as_payload(checks)
            emit(payload, as_json=as_json)
            return OK if payload["ok"] or not args.strict else CONFIG_INVALID
        if args.command == "worker" and args.worker_command == "health":
            from .worker.ipc import health, parse_envelope

            payload = health(root, parse_envelope(args.envelope_json))
            emit(payload, as_json=as_json)
            return OK
        if args.command == "worker" and args.worker_command == "status":
            from .worker.lock import lock_status

            payload = {"ok": True, "lock": lock_status(root)}
            emit(payload, as_json=as_json)
            return OK
        if args.command == "worker" and args.worker_command == "stop":
            reject_ci_write("worker_stop")
            from .worker.lock import clear_worker_lock

            payload = clear_worker_lock(root, force=args.force, reason=args.reason)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else WORKER_UNAVAILABLE
        if args.command == "queue" and args.queue_command == "enqueue":
            reject_ci_write("queue")
            from .worker.scheduler import enqueue

            payload = enqueue(root, args.priority, args.kind, read_payload(), max_attempts=args.max_attempts)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "lease":
            reject_ci_write("queue")
            from .worker.scheduler import lease_next

            payload = lease_next(root, args.worker_id, priority=args.priority)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "complete":
            reject_ci_write("queue")
            from .worker.scheduler import complete

            payload = complete(root, args.job_id, args.lease_id)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "fail":
            reject_ci_write("queue")
            from .worker.scheduler import fail

            payload = fail(root, args.job_id, args.lease_id, args.reason)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "recover-expired":
            reject_ci_write("queue")
            from .worker.scheduler import recover_expired

            payload = recover_expired(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "archive-dead":
            reject_ci_write("queue")
            from .worker.scheduler import archive_dead

            payload = archive_dead(root, older_than_days=args.older_than_days)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "dead":
            from .worker.scheduler import list_dead

            payload = list_dead(root, limit=args.limit, since_iso=args.since)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "queue" and args.queue_command == "status":
            from .worker.scheduler import status as queue_status

            payload = queue_status(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "loop":
            from . import loop_engineering as loop_eng

            if args.loop_command == "submit":
                reject_ci_write("loop")
                instruction = _read_loop_text(root, text=args.text, file_path=args.file, stdin_fallback=True)
                rubric = (
                    _read_loop_text(root, text=args.rubric, file_path=args.rubric_file)
                    if args.rubric is not None or args.rubric_file
                    else ""
                )
                payload = loop_eng.submit(
                    root,
                    instruction=instruction,
                    goal=args.goal,
                    source_agent=args.source_agent,
                    target_agent=args.target_agent,
                    role=args.role,
                    priority=args.priority,
                    interval_seconds=args.interval_seconds,
                    reviewer_required=not args.no_review,
                    rubric=rubric,
                    checklist=args.checklist,
                    acceptance_required=bool(getattr(args, "require_acceptance", False)),
                )
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "claim":
                reject_ci_write("loop")
                payload = loop_eng.claim(
                    root,
                    orchestrator_id=args.orchestrator_id,
                    agent=args.agent,
                    priority=args.priority,
                    request_id=args.request_id,
                    lease_seconds=args.lease_seconds,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "complete":
                reject_ci_write("loop")
                result = (
                    _read_loop_text(root, text=args.result, file_path=args.result_file)
                    if args.result_file or args.result is not None
                    else ""
                )
                payload = loop_eng.complete(
                    root,
                    request_id=args.request_id,
                    lease_id=args.lease_id,
                    summary=args.summary,
                    result=result,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "fail":
                reject_ci_write("loop")
                payload = loop_eng.fail(root, request_id=args.request_id, lease_id=args.lease_id, reason=args.reason)
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "verdict":
                reject_ci_write("loop")
                rubric_result = (
                    _read_loop_text(root, text=args.rubric_result, file_path=args.rubric_result_file)
                    if args.rubric_result is not None or args.rubric_result_file
                    else ""
                )
                evidence = None
                if getattr(args, "evidence_json", None):
                    try:
                        parsed = json.loads(args.evidence_json)
                        if isinstance(parsed, list):
                            evidence = [e for e in parsed if isinstance(e, dict)]
                    except (ValueError, TypeError):
                        emit({"ok": False, "reason": "invalid_evidence_json"}, as_json=as_json)
                        return GENERIC_ERROR
                payload = loop_eng.record_verdict(
                    root,
                    request_id=args.request_id,
                    lease_id=args.lease_id,
                    reviewer=args.reviewer,
                    verdict=args.verdict,
                    summary=args.summary,
                    rubric_result=rubric_result,
                    evidence=evidence,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "acceptance":
                reject_ci_write("loop")
                payload = loop_eng.record_acceptance(
                    root,
                    request_id=args.request_id,
                    lease_id=args.lease_id,
                    commands=args.acceptance_commands,
                    timeout=args.timeout,
                )
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.loop_command == "distill":
                reject_ci_write("loop")
                text = _read_loop_text(root, text=args.text, file_path=args.file)
                payload = loop_eng.distill(root, request_id=args.request_id, text=text, tags=args.tag, force=args.force)
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "recover-expired":
                reject_ci_write("loop")
                payload = loop_eng.recover_expired(root)
                emit(payload, as_json=as_json)
                return OK
            if args.loop_command == "status":
                payload = loop_eng.status(root)
                emit(payload, as_json=as_json)
                return OK
        if args.command == "plan":
            from . import plan_state as _ps
            if args.plan_command == "init":
                reject_ci_write("plan")
                payload = _ps.init_plan(root, plan_id=args.plan_id, steps=args.plan_steps,
                                        title=args.title, force=bool(args.force))
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.plan_command == "show":
                payload = _ps.read_plan(root, args.plan_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.plan_command == "check":
                reject_ci_write("plan")
                payload = _ps.mark_step(root, plan_id=args.plan_id, match=args.match,
                                        index=args.index, done=not args.undo)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.plan_command == "list":
                payload = _ps.list_plans(root)
                emit(payload, as_json=as_json)
                return OK
        if args.command == "prompt-growth" and args.prompt_growth_command == "status":
            from . import prompt_growth as _pg

            emit(_pg.status(root), as_json=as_json)
            return OK
        if args.command == "selfimprove":
            from . import self_improve as _si

            if args.selfimprove_command == "run":
                reject_ci_write("selfimprove")
                emit(_si.enqueue_review(root, tier=args.tier), as_json=as_json)
                return OK
            if args.selfimprove_command == "propose":
                reject_ci_write("selfimprove")
                payload = _si.propose_rule(root, text=args.text, rationale=args.rationale)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.selfimprove_command == "status":
                emit(_si.status(root), as_json=as_json)
                return OK
        if args.command == "loopd":
            from . import loopd as _ld

            if args.loopd_command == "status":
                emit(_ld.status(root), as_json=as_json)
                return OK
            if args.loopd_command == "agents":
                from . import worker_launch as _wl
                emit(_wl.capabilities(), as_json=as_json)
                return OK
            if args.loopd_command == "dispatch-once":
                reject_ci_write("loopd")
                emit(_ld.dispatch_once(root), as_json=as_json)
                return OK
            if args.loopd_command == "recover":
                reject_ci_write("loopd")
                emit(_ld.recovery_tick(root), as_json=as_json)
                return OK
            if args.loopd_command == "launch":
                reject_ci_write("loopd")
                from . import worker_launch as _wl
                payload = _wl.launch_worker(root, worker_id=args.worker_id, agent=args.agent,
                                            profile=args.profile or args.worker_id,
                                            inherit_auth=args.inherit_auth, autonomous=args.autonomous,
                                            tier=args.tier, dry_run=args.dry_run)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.loopd_command == "up":
                reject_ci_write("loopd")
                from . import worker_launch as _wl
                emit(_wl.launch_pool(root, dry_run=args.dry_run, autonomous=args.autonomous,
                                     tier=args.tier), as_json=as_json)
                return OK
            if args.loopd_command == "account":
                reject_ci_write("loopd")
                from . import worker_profiles as _wp
                if args.account_command == "add":
                    emit(_wp.add_account(root, agent=args.agent, account=args.account), as_json=as_json)
                    return OK
                if args.account_command == "list":
                    emit({"ok": True, "accounts": _wp.list_accounts(root, agent=args.agent)}, as_json=as_json)
                    return OK
                if args.account_command == "login":
                    from . import worker_launch as _wl
                    emit(_wl.account_login(root, agent=args.agent, account=args.account), as_json=as_json)
                    return OK
            if args.loopd_command == "models":
                from . import worker_models as _wm
                if args.models_command == "list":
                    emit(_wm.list_models(root), as_json=as_json)
                    return OK
                if args.models_command == "set":
                    reject_ci_write("loopd")
                    emit(_wm.set_model(root, agent=args.agent, model=args.model,
                                       reasoning=args.reasoning, flags=args.model_flags), as_json=as_json)
                    return OK
        if args.command == "loopd" and args.loopd_command == "worker":
            from . import worker_profiles as _wp
            from . import worker_registry as _wr

            if args.worker_command == "register":
                reject_ci_write("worker")
                emit(_wr.register_worker(root, worker_id=args.worker_id, agent=args.agent,
                                         profile=args.profile, cwd=args.cwd, pane_id=args.pane_id,
                                         state=args.state), as_json=as_json)
                return OK
            if args.worker_command == "list":
                emit({"ok": True, "workers": _wr.list_workers(root, state=args.state)}, as_json=as_json)
                return OK
            if args.worker_command == "heartbeat":
                reject_ci_write("worker")
                emit(_wr.write_heartbeat(root, worker_id=args.worker_id, state=args.state,
                                         request_id=args.request_id), as_json=as_json)
                return OK
            if args.worker_command == "profile" and args.worker_profile_command == "register":
                reject_ci_write("worker")
                emit(_wp.register_profile(root, profile=args.profile, agent=args.agent,
                                          worker_id=args.worker_id), as_json=as_json)
                return OK
            if args.worker_command == "profile" and args.worker_profile_command == "list":
                emit({"ok": True, "profiles": _wp.list_profiles(root)}, as_json=as_json)
                return OK
        if args.command == "trust" and args.trust_command == "init":
            reject_ci_write("trust")
            payload = init_machine(root, name=args.name)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "trust" and args.trust_command == "list":
            payload = list_machines(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "trust" and args.trust_command == "revoke":
            reject_ci_write("trust")
            payload = revoke_machine(root, args.machine_id_hash)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "secrets" and args.secrets_command == "status":
            payload = secrets_status(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "inbox" and args.inbox_command == "request":
            reject_ci_write("inbox")
            payload = request_approval(root, args.gate, args.summary, read_payload(), ttl_hours=args.ttl_hours)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "inbox" and args.inbox_command == "list":
            payload = list_approvals(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "inbox" and args.inbox_command == "approve":
            reject_ci_write("inbox")
            payload = decide(root, args.approval_id, "approved")
            emit(payload, as_json=as_json)
            return OK
        if args.command == "inbox" and args.inbox_command == "reject":
            reject_ci_write("inbox")
            payload = decide(root, args.approval_id, "rejected")
            emit(payload, as_json=as_json)
            return OK
        if args.command == "notify" and args.notify_command == "enqueue":
            reject_ci_write("notify")
            from .notify import enqueue_notification

            payload = enqueue_notification(root, args.channel, read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "log":
            reject_ci_write("obs_write")
            payload = write_log(root, args.level, args.event, read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "metrics":
            payload = metrics(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "search":
            if getattr(args, "refresh_stale", False):
                reject_ci_write("index", dry_run=False)
                rebuild(root)
            payload = search_report(root, query_text=args.query, limit=args.limit)
            emit(payload, as_json=as_json)
            stale = (payload.get("query") or {}).get("stale_results") or []
            if stale and not getattr(args, "refresh_stale", False):
                return MANIFEST_DRIFT
            return OK
        if args.command == "obs" and args.obs_command == "usage":
            payload = usage_report(root, include_sessions=bool(getattr(args, "include_sessions", False)))
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "slo":
            payload = slo_bench(root, iterations=args.iterations)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "health-summary":
            payload = health_summary(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "trajectory":
            from .trajectory import summarize, extract_trajectories
            if args.session_id:
                payload = extract_trajectories(root, session_id=args.session_id, limit=args.limit)
            else:
                payload = summarize(root, limit=args.limit)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "obs" and args.obs_command == "speculative":
            from .speculative import mine_patterns, hit_rate
            if args.hit_rate:
                payload = hit_rate(root)
            else:
                payload = mine_patterns(
                    root,
                    min_support=args.min_support,
                    min_confidence=args.min_confidence,
                )
            emit(payload, as_json=as_json)
            return OK
        if args.command == "diagnostics" and args.diagnostics_command == "bundle":
            reject_ci_write("diagnostics_write", dry_run=args.dry_run)
            payload = diagnostics(root, dry_run=args.dry_run)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "diagnostics" and args.diagnostics_command == "prune":
            reject_ci_write("diagnostics_write")
            payload = prune_diagnostics(root, keep_days=args.keep_days)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "migrate":
            reject_ci_write("migrate", dry_run=args.dry_run)
            from .upgrade import migrate

            payload = migrate(root, dry_run=args.dry_run)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "upgrade" and args.upgrade_command == "plan":
            from .upgrade import upgrade_plan

            payload = upgrade_plan(root, target_version=args.target_version)
            emit(payload, as_json=as_json)
            return OK if payload["ok"] else GENERIC_ERROR
        if args.command == "upgrade" and args.upgrade_command == "apply":
            reject_ci_write("upgrade", dry_run=args.dry_run)
            from .upgrade import upgrade_apply

            payload = upgrade_apply(root, target_version=args.target_version, dry_run=args.dry_run)
            emit(payload, as_json=as_json)
            return OK if payload["ok"] else GENERIC_ERROR
        if args.command == "upgrade" and args.upgrade_command == "rollback":
            reject_ci_write("upgrade")
            from .upgrade import rollback

            payload = rollback(root, backup_path=args.backup_path)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "upgrade" and args.upgrade_command == "clean-cache":
            reject_ci_write("upgrade")
            from .upgrade import clean_upgrade_cache

            payload = clean_upgrade_cache(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "upgrade" and args.upgrade_command == "latest":
            reject_ci_write("upgrade", dry_run=args.dry_run)
            from .upgrade import upgrade_latest

            payload = upgrade_latest(root, repo_url=args.repo_url, ref=args.ref, dry_run=args.dry_run, keep_clone=args.keep_clone)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "hook":
            payload = handle_hook(root, args.hook_name, read_payload())
            emit(payload if args.command_json else codex_wire_output(payload), as_json=True)
            return OK
        if args.command == "memory" and args.memory_command == "append-event":
            reject_ci_write("memory")
            payload = append_event(root, read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "memory" and args.memory_command == "evidence":
            from .evidence import list_evidence, set_evidence_status
            if args.memory_evidence_command == "list":
                payload = list_evidence(root, status=args.status, limit=args.limit)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.memory_evidence_command == "set-status":
                reject_ci_write("memory")
                payload = set_evidence_status(
                    root,
                    evidence_id_value=args.id,
                    status=args.status,
                    note=args.note,
                    source=args.source,
                )
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "decision" and args.memory_decision_command == "add":
            reject_ci_write("memory")
            from .memory import append_decision
            obs_versions: dict[str, str] = {}
            for pair in (getattr(args, "observed_version", None) or []):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k.strip():
                        obs_versions[k.strip()] = v.strip()
            payload = append_decision(
                root, text=args.text, tags=args.tag, source=args.source,
                kind=getattr(args, "kind", None),
                observed_at=getattr(args, "observed_at", None),
                observed_versions=obs_versions or None,
                environment=getattr(args, "environment", None),
                retest_after=getattr(args, "retest_after", None),
                status=getattr(args, "status", None),
                supersedes_id=getattr(args, "supersedes_id", None),
                contradicts=getattr(args, "contradicts", None),
                derives_from=getattr(args, "derives_from", None),
                expires_at=getattr(args, "expires_at", None),
            )
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "decision" and args.memory_decision_command == "list":
            from .memory import read_decisions_filtered
            payload = read_decisions_filtered(
                root, kind=args.kind, status=args.status, tag=args.tag,
                source=args.source, text=args.text, limit=args.limit,
                include_retired=bool(args.include_retired),
            )
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "recall":
            from .memory_recall import recall_memory
            payload = recall_memory(root, query=args.query, limit=args.limit, types=args.recall_types)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "conflicts":
            from .memory_conflicts import list_conflicts, scan_conflicts
            if args.scan or args.dry_run:
                reject_ci_write("memory")
                payload = scan_conflicts(root, dry_run=bool(args.dry_run))
            else:
                payload = list_conflicts(root, limit=args.limit)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "todo" and args.memory_todo_command == "add":
            reject_ci_write("memory")
            from .memory import append_todo
            payload = append_todo(root, title=args.title, owner=args.owner, tags=args.tag, source=args.source)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "todo" and args.memory_todo_command == "close":
            reject_ci_write("memory")
            from .memory import close_todo
            payload = close_todo(root, match=args.match, status=args.status, reason=args.reason)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "session" and args.memory_session_command == "append":
            reject_ci_write("memory")
            from .memory import append_session_note
            payload = append_session_note(root, text=args.text)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "handoff":
            reject_ci_write("memory")
            from .session_resume import write_handoff
            payload = write_handoff(
                root,
                goal=args.goal,
                next_step=args.next_step,
                plan=args.plan,
                open_questions=args.open_questions,
                blockers=args.blockers,
                agent=args.agent,
                clear=args.clear,
            )
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "sync":
            reject_ci_write("memory")
            from .memory_sync import sync_loop, sync_once
            if args.loop and args.loop > 0:
                sync_loop(root, agent=args.agent, interval=args.loop)  # blocks until killed
                return OK
            payload = sync_once(root, agent=args.agent, push=not args.no_push)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "tier":
            from . import memory_tier as _mt
            emit(_mt.classify(root), as_json=as_json)
            return OK
        if args.command == "memory" and args.memory_command == "pressure":
            from . import memory_tier as _mt
            emit(_mt.hot_pressure(root), as_json=as_json)
            return OK
        if args.command == "memory" and args.memory_command == "page-out":
            from . import memory_tier as _mt
            emit(_mt.page_out(root, dry_run=bool(args.dry_run)), as_json=as_json)
            return OK
        if args.command == "memory" and args.memory_command == "page-in":
            from . import memory_tier as _mt
            payload = _mt.page_in(root, dry_run=bool(args.dry_run), limit=args.limit)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "memory" and args.memory_command == "retention":
            from . import memory_tier as _mt
            emit(_mt.retention_report(root, evict_limit=int(args.evict_limit)), as_json=as_json)
            return OK
        if args.command == "lessons":
            from .lessons import add_lesson, lesson_summary, list_lessons

            if args.lessons_command == "add":
                reject_ci_write("lessons")
                payload = add_lesson(root, source=args.source, failure=args.failure, cause=args.cause, fix=args.fix, tags=args.tag)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.lessons_command == "list":
                payload = list_lessons(root, limit=args.limit)
                emit(payload, as_json=as_json)
                return OK
            if args.lessons_command == "summary":
                payload = lesson_summary(root)
                emit(payload, as_json=as_json)
                return OK
            if args.lessons_command == "score":
                from .lessons import score_lessons
                payload = score_lessons(root, include_stale=bool(args.include_stale))
                emit(payload, as_json=as_json)
                return OK
            if args.lessons_command == "recall":
                from .lessons import recall_lessons
                payload = recall_lessons(root, query=args.query, limit=int(args.limit), include_stale=bool(args.include_stale))
                emit(payload, as_json=as_json)
                return OK
        if args.command == "audit" and args.audit_command == "append":
            reject_ci_write("audit")
            payload = append_audit(root, action=args.action, category=args.category, payload=read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "audit" and args.audit_command == "rebuild-index":
            reject_ci_write("audit")
            payload = rebuild_audit_index(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "audit" and args.audit_command == "repair-chain":
            reject_ci_write("audit")
            from .audit_repair import repair_audit_chain
            payload = repair_audit_chain(root, year=args.year)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "index" and args.index_command == "rebuild":
            reject_ci_write("index")
            payload = rebuild(
                root,
                single_flight=getattr(args, "single_flight", False),
                incremental=getattr(args, "incremental", False),
            )
            emit(payload, as_json=as_json)
            return OK
        if args.command == "recommend" and args.recommend_command == "skills":
            from .recommend import accept as rec_accept_fn, recommend as rec_run, reject as rec_reject_fn
            sub_cmd = getattr(args, "recommend_skills_command", None)
            if sub_cmd == "accept":
                reject_ci_write("skills")
                payload = rec_accept_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if sub_cmd == "reject":
                reject_ci_write("skills")
                payload = rec_reject_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if getattr(args, "compact", False):
                reject_ci_write("skills")
                from .recommend import compact_skill_catalog
                payload = compact_skill_catalog(root)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            payload = rec_run(
                root,
                limit=args.limit,
                include_global=not getattr(args, "no_global", False),
                min_signal=getattr(args, "min_signal", 3),
            )
            emit(payload, as_json=as_json)
            return OK
        if args.command == "skills" and args.skills_command == "list":
            from .recommend import list_visible
            payload = {"ok": True, "skills": list_visible(root)}
            emit(payload, as_json=as_json)
            return OK
        if args.command == "skills" and args.skills_command == "uninstall":
            reject_ci_write("skills")
            from .recommend import uninstall as skills_uninstall_fn
            payload = skills_uninstall_fn(root, args.slug, force=args.force)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "precall":
            from .precall_recommend import (
                accept as pc_accept_fn,
                activate as pc_activate_fn,
                disable as pc_disable_fn,
                list_visible as pc_list_visible,
                recommend as pc_recommend_fn,
                reject as pc_reject_fn,
            )
            cmd = args.precall_command
            if cmd == "list":
                payload = {"ok": True, "rules": pc_list_visible(root)}
                emit(payload, as_json=as_json)
                return OK
            if cmd == "recommend":
                payload = pc_recommend_fn(
                    root,
                    limit=args.limit,
                    min_signal=getattr(args, "min_signal", 5),
                    include_transcripts=getattr(args, "include_transcripts", False),
                )
                emit(payload, as_json=as_json)
                return OK
            if cmd == "accept":
                reject_ci_write("precall")
                payload = pc_accept_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if cmd == "activate":
                reject_ci_write("precall")
                payload = pc_activate_fn(root, args.candidate_id, force=args.force)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if cmd == "reject":
                reject_ci_write("precall")
                payload = pc_reject_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if cmd == "disable":
                reject_ci_write("precall")
                payload = pc_disable_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "federated" and args.federated_command == "summary":
            from .federated import cross_project_summary
            payload = cross_project_summary(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "eval":
            from .eval_loop import record_case, summarize_cases
            if args.eval_command == "record":
                reject_ci_write("eval")
                payload = record_case(
                    root,
                    case_id=args.case_id,
                    kind=args.kind,
                    command=args.eval_case_command,
                    outcome=args.outcome,
                    duration_ms=args.duration_ms,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.eval_command == "summary":
                payload = summarize_cases(root, latest_limit=args.latest)
                emit(payload, as_json=as_json)
                return OK
        if args.command == "embedding":
            from . import embedding as emb_mod
            cmd = args.embedding_command
            if cmd == "status":
                emit(emb_mod.status(root), as_json=as_json)
                return OK
            if cmd == "install":
                payload = emb_mod.install_model(root, verify_only=bool(args.verify))
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else 1
            if cmd == "uninstall":
                payload = emb_mod.uninstall_model(root)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else 1
        if args.command == "agents":
            from .agent_recommend import (
                accept as ag_accept_fn,
                list_visible as ag_list_visible,
                recommend as ag_recommend_fn,
                reject as ag_reject_fn,
                uninstall as ag_uninstall_fn,
            )
            cmd = args.agents_command
            if cmd == "list":
                payload = {"ok": True, "agents": ag_list_visible(root)}
                emit(payload, as_json=as_json)
                return OK
            if cmd == "recommend":
                payload = ag_recommend_fn(root, limit=args.limit, min_signal=args.min_signal)
                emit(payload, as_json=as_json)
                return OK
            if cmd == "accept":
                reject_ci_write("agents")
                payload = ag_accept_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if cmd == "reject":
                reject_ci_write("agents")
                payload = ag_reject_fn(root, args.candidate_id)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if cmd == "uninstall":
                reject_ci_write("agents")
                payload = ag_uninstall_fn(root, args.slug, force=args.force)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
        # remote-memory dispatch removed (T37)
        if args.command == "exec":
            from .sandbox import execute as sandbox_execute, fetch as sandbox_fetch, list_executions as sandbox_list, prune as sandbox_prune

            if args.exec_command == "run":
                reject_ci_write("exec")
                argv = args.argv or []
                if argv and argv[0] == "--":
                    argv = argv[1:]
                if not argv:
                    print("usage: ai exec run [--cwd PATH] [--timeout N] -- COMMAND [ARGS...]", file=sys.stderr)
                    return GENERIC_ERROR
                payload = sandbox_execute(root, command=argv, cwd=args.cwd, timeout=args.timeout)
                emit(payload, as_json=as_json)
                return OK
            if args.exec_command == "fetch":
                payload = sandbox_fetch(
                    root,
                    exec_id=args.exec_id,
                    line_start=args.line_start,
                    line_end=args.line_end,
                    grep_pattern=args.grep,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.exec_command == "list":
                payload = sandbox_list(root, limit=args.limit)
                emit(payload, as_json=as_json)
                return OK
            if args.exec_command == "prune":
                reject_ci_write("exec")
                payload = sandbox_prune(root, older_than_seconds=args.older_than_seconds)
                emit(payload, as_json=as_json)
                return OK
        if args.command == "code" and args.code_command == "query":
            payload = query(root, args.query, limit=args.limit, evidence_source="search")
            emit(payload, as_json=as_json)
            return OK
        if args.command == "code" and args.code_command == "graph":
            from . import codegraph as _cg
            gcmd = args.graph_command
            if gcmd == "callers":
                emit(_cg.query_callers(root, args.qualname, limit=args.limit), as_json=as_json)
                return OK
            if gcmd == "callees":
                emit(_cg.query_callees(root, args.qualname, limit=args.limit), as_json=as_json)
                return OK
            if gcmd == "symbol":
                emit(_cg.find_symbol(root, args.name, limit=args.limit), as_json=as_json)
                return OK
            if gcmd == "hotspots":
                emit(_cg.hotspot_callees(root, limit=args.limit), as_json=as_json)
                return OK
        if args.command == "code" and args.code_command == "verify":
            from . import ast_verify as _av
            if args.stdin or not args.source:
                src = sys.stdin.read()
                report = _av.verify_source(src).to_dict()
            else:
                report = _av.verify_file(args.source).to_dict()
            emit(report, as_json=as_json)
            return OK if report["ok"] else GENERIC_ERROR
        if args.command == "code" and args.code_command == "read-hashline":
            from .hashline import read_hashline
            payload = read_hashline(root, args.path, start=args.start, end=args.end)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "code" and args.code_command == "verify-hashline":
            from .hashline import verify_anchors
            raw = sys.stdin.read()
            anchors = json.loads(raw) if raw.strip() else []
            if not isinstance(anchors, list):
                raise ValueError("verify-hashline expects JSON array on stdin")
            payload = verify_anchors(root, args.path, anchors)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "code" and args.code_command == "map":
            from .codebase_map import build_codebase_map

            emit(build_codebase_map(root, max_entries=args.limit), as_json=as_json)
            return OK
        if args.command == "guard" and args.guard_command == "scan":
            from .stream_guard import scan_text
            text = sys.stdin.read() if args.stdin else (args.text or "")
            payload = scan_text(text, scope=args.scope)
            emit(payload, as_json=as_json)
            return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "context" and args.context_command == "pack":
            payload = context_pack(root, args.query, limit=args.limit, mode=args.mode)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "evidence":
            from .evidence import list_evidence, record_evidence, set_evidence_status
            if args.evidence_command == "record":
                reject_ci_write("evidence")
                payload = record_evidence(
                    root,
                    query=args.query,
                    path=args.path,
                    status=args.status,
                    snippet=args.snippet,
                    source=args.source,
                    note=args.note,
                )
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.evidence_command == "update":
                reject_ci_write("evidence")
                payload = set_evidence_status(root, evidence_id_value=args.id, status=args.status, note=args.note, source=args.source)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.evidence_command == "list":
                payload = list_evidence(root, status=args.status, query=args.query, limit=args.limit)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "security" and args.security_command == "finding":
            from . import security_findings
            if args.security_finding_command == "record":
                reject_ci_write("security_finding")
                payload = security_findings.record(
                    root,
                    affected_path=args.affected_path,
                    finding_type=args.finding_type,
                    detail_summary=args.detail_summary,
                    evidence_hash=args.evidence_hash,
                    repro_command=args.repro_command,
                    verification_command=args.verification_command,
                    status=args.status,
                    source=args.source,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.security_finding_command == "update":
                reject_ci_write("security_finding")
                payload = security_findings.update(
                    root,
                    finding_id=args.id,
                    status=args.status,
                    verification_command=args.verification_command,
                    source=args.source,
                )
                emit(payload, as_json=as_json)
                return OK
            if args.security_finding_command == "list":
                payload = security_findings.list_records(root, status=args.status, limit=args.limit)
                emit(payload, as_json=as_json)
                return OK
        if args.command == "session" and args.session_command == "start":
            reject_ci_write("session", dry_run=args.dry_run)
            from .session import start_session

            payload = start_session(
                root,
                agent=args.agent,
                rebuild_mode=args.rebuild,
                dry_run=args.dry_run,
                strict=args.strict,
                query_text=args.query,
                limit=args.limit,
                context_budget_mode=args.mode,
            )
            emit(payload, as_json=as_json)
            return OK if payload["ok"] or not args.strict else CONFIG_INVALID
        if args.command == "mcp":
            from .mcp_server import handle_request, serve_stdio

            if args.once_json:
                emit(handle_request(root, json.loads(args.once_json)), as_json=True)
                return OK
            return serve_stdio(root)
        if args.command == "release-gate" and args.release_gate_command == "summary":
            from .release_gate import summary as release_gate_summary

            payload = release_gate_summary(root)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "kit":
            from .global_kit import status as kit_status_fn, validate as kit_validate_fn

            if args.kit_command == "status":
                payload = kit_status_fn(root)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
            if args.kit_command == "validate":
                payload = kit_validate_fn(root)
                emit(payload, as_json=as_json)
                return OK if payload.get("ok") else GENERIC_ERROR
        if args.command == "runtime":
            from .agent_runtime import context_policy as runtime_context_policy, insights as runtime_insights

            if args.runtime_command == "insights":
                emit(runtime_insights(root), as_json=as_json)
                return OK
            if args.runtime_command == "context-policy":
                emit(runtime_context_policy(), as_json=as_json)
                return OK
        if args.command == "report" and args.report_command == "status":
            from .report import status_exit_ok, status_report

            payload = status_report(root)
            emit(payload, as_json=as_json)
            return OK if status_exit_ok(payload) else GENERIC_ERROR
        if args.command == "report" and args.report_command == "release-notes":
            from .report import release_notes

            print(release_notes(root))
            return OK
        if args.command == "report" and args.report_command == "release-gate-summary":
            from .report import release_gate_summary

            payload = release_gate_summary(root, git_sha=args.git_sha)
            emit(payload, as_json=True)
            return OK
    except PolicyDenied as exc:
        emit({"ok": False, "error": "CI_READ_ONLY", "command": exc.command, "exit_code": PERMISSION_DENIED}, as_json=True)
        return PERMISSION_DENIED
    except SystemExit as exc:
        raise exc
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "message"):
            emit({"ok": False, "error": exc.code, "detail": exc.message}, as_json=True)
            return GENERIC_ERROR
        payload = {"ok": False, "error": str(exc)}
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            payload.update(metadata)
        emit(payload, as_json=True)
        return GENERIC_ERROR
    return PERMISSION_DENIED


if __name__ == "__main__":
    raise SystemExit(main())
