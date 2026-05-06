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
from .memory import append_audit, append_event
from .obs import diagnostics, metrics, prune_diagnostics, slo_bench, write_log
from .paths import find_repo_root
from .policy import CONFIG_INVALID, GENERIC_ERROR, OK, PERMISSION_DENIED, PolicyDenied, reject_ci_write
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
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true", dest="command_json")
    doctor_parser.add_argument("--strict", action="store_true")
    worker = sub.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_health = worker_sub.add_parser("health")
    worker_health.add_argument("--json", action="store_true", dest="command_json")
    worker_health.add_argument("--envelope-json")
    queue = sub.add_parser("queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_enqueue = queue_sub.add_parser("enqueue")
    queue_enqueue.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], required=True)
    queue_enqueue.add_argument("--kind", required=True)
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
    obs_slo = obs_sub.add_parser("slo")
    obs_slo.add_argument("--iterations", type=int, default=10)
    obs_slo.add_argument("--json", action="store_true", dest="command_json")
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
    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_append = audit_sub.add_parser("append")
    audit_append.add_argument("--action", required=True)
    audit_append.add_argument("--category", default="manual")
    audit_append.add_argument("--json", action="store_true", dest="command_json")
    index = sub.add_parser("index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_rebuild = index_sub.add_parser("rebuild")
    index_rebuild.add_argument("--json", action="store_true", dest="command_json")
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
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--once-json")
    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_status = report_sub.add_parser("status")
    report_status.add_argument("--json", action="store_true", dest="command_json")
    report_sub.add_parser("release-notes")
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
            result = render(root, dry_run=args.dry_run, no_overwrite=args.no_overwrite)
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
        if args.command == "queue" and args.queue_command == "enqueue":
            reject_ci_write("queue")
            from .worker.scheduler import enqueue

            payload = enqueue(root, args.priority, args.kind, read_payload())
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
        if args.command == "obs" and args.obs_command == "slo":
            payload = slo_bench(root, iterations=args.iterations)
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
        if args.command == "audit" and args.audit_command == "append":
            reject_ci_write("audit")
            payload = append_audit(root, action=args.action, category=args.category, payload=read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "index" and args.index_command == "rebuild":
            reject_ci_write("index")
            payload = rebuild(root)
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
        if args.command == "mcp":
            from .mcp_server import handle_request, serve_stdio

            if args.once_json:
                emit(handle_request(root, json.loads(args.once_json)), as_json=True)
                return OK
            return serve_stdio(root)
        if args.command == "report" and args.report_command == "status":
            from .report import status_report

            payload = status_report(root)
            emit(payload, as_json=as_json)
            return OK if payload["ok"] else GENERIC_ERROR
        if args.command == "report" and args.report_command == "release-notes":
            from .report import release_notes

            print(release_notes(root))
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
