from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import load_config
from .doctor import as_payload, run_checks
from .hooks import handle_hook, read_payload
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
    obs_usage.add_argument("--json", action="store_true", dest="command_json")
    obs_slo = obs_sub.add_parser("slo")
    obs_slo.add_argument("--iterations", type=int, default=10)
    obs_slo.add_argument("--json", action="store_true", dest="command_json")
    obs_health = obs_sub.add_parser("health-summary")
    obs_health.add_argument("--json", action="store_true", dest="command_json")
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
    hook_parser = sub.add_parser("hook")
    hook_parser.add_argument("hook_name", nargs="?")
    hook_parser.add_argument("--json", action="store_true", dest="command_json")
    memory = sub.add_parser("memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_append_event = memory_sub.add_parser("append-event")
    memory_append_event.add_argument("--json", action="store_true", dest="command_json")
    memory_decision = memory_sub.add_parser("decision")
    memory_decision_sub = memory_decision.add_subparsers(dest="memory_decision_command", required=True)
    memory_decision_add = memory_decision_sub.add_parser("add")
    memory_decision_add.add_argument("--text", required=True)
    memory_decision_add.add_argument("--tag", action="append", default=[])
    memory_decision_add.add_argument("--source", default="operator")
    memory_decision_add.add_argument("--json", action="store_true", dest="command_json")
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
    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_append = audit_sub.add_parser("append")
    audit_append.add_argument("--action", required=True)
    audit_append.add_argument("--category", default="manual")
    audit_append.add_argument("--json", action="store_true", dest="command_json")
    audit_rebuild = audit_sub.add_parser("rebuild-index")
    audit_rebuild.add_argument("--json", action="store_true", dest="command_json")
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
    recommend_parser = sub.add_parser("recommend")
    recommend_sub = recommend_parser.add_subparsers(dest="recommend_command", required=True)
    recommend_skills = recommend_sub.add_parser("skills")
    recommend_skills_sub = recommend_skills.add_subparsers(dest="recommend_skills_command", required=False)
    recommend_skills.add_argument("--limit", type=int, default=5)
    recommend_skills.add_argument("--no-global", action="store_true", dest="no_global")
    recommend_skills.add_argument("--min-signal", type=int, default=3, dest="min_signal")
    recommend_skills.add_argument("--json", action="store_true", dest="command_json")
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

    code = sub.add_parser("code")
    code_sub = code.add_subparsers(dest="code_command", required=True)
    code_query = code_sub.add_parser("query")
    code_query.add_argument("query")
    code_query.add_argument("--limit", type=int, default=5)
    code_query.add_argument("--json", action="store_true", dest="command_json")
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_pack_parser = context_sub.add_parser("pack")
    context_pack_parser.add_argument("query")
    context_pack_parser.add_argument("--limit", type=int, default=5)
    context_pack_parser.add_argument("--json", action="store_true", dest="command_json")
    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_start = session_sub.add_parser("start")
    session_start.add_argument("--agent", default="operator")
    session_start.add_argument("--rebuild", choices=["auto", "always", "never"], default="auto")
    session_start.add_argument("--dry-run", action="store_true")
    session_start.add_argument("--strict", action="store_true")
    session_start.add_argument("--query")
    session_start.add_argument("--limit", type=int, default=5)
    session_start.add_argument("--json", action="store_true", dest="command_json")
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--once-json")
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
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


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
            payload = usage_report(root)
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
        if args.command == "hook":
            payload = handle_hook(root, args.hook_name, read_payload())
            emit(payload, as_json=True)
            return OK
        if args.command == "memory" and args.memory_command == "append-event":
            reject_ci_write("memory")
            payload = append_event(root, read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "memory" and args.memory_command == "decision" and args.memory_decision_command == "add":
            reject_ci_write("memory")
            from .memory import append_decision
            payload = append_decision(root, text=args.text, tags=args.tag, source=args.source)
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
        if args.command == "index" and args.index_command == "rebuild":
            reject_ci_write("index")
            payload = rebuild(root, single_flight=getattr(args, "single_flight", False))
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
            payload = query(root, args.query, limit=args.limit)
            emit(payload, as_json=as_json)
            return OK
        if args.command == "context" and args.context_command == "pack":
            payload = context_pack(root, args.query, limit=args.limit)
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
            )
            emit(payload, as_json=as_json)
            return OK if payload["ok"] or not args.strict else CONFIG_INVALID
        if args.command == "mcp":
            from .mcp_server import handle_request, serve_stdio

            if args.once_json:
                emit(handle_request(root, json.loads(args.once_json)), as_json=True)
                return OK
            return serve_stdio(root)
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
        emit({"ok": False, "error": str(exc)}, as_json=True)
        return GENERIC_ERROR
    return PERMISSION_DENIED


if __name__ == "__main__":
    raise SystemExit(main())
