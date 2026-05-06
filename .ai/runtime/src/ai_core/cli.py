from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_config
from .doctor import as_payload, run_checks
from .hooks import handle_hook, read_payload
from .memory import append_audit, append_event
from .mcp_server import handle_request, serve_stdio
from .paths import find_repo_root
from .policy import CONFIG_INVALID, GENERIC_ERROR, OK, PERMISSION_DENIED, reject_ci_write
from .render import render
from .search import context_pack, query, rebuild
from .worker.ipc import IpcError, health, parse_envelope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai")
    parser.add_argument("--json", action="store_true", help="emit JSON")
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
        root = find_repo_root()
        reject_ci_write(args.command, dry_run=getattr(args, "dry_run", False))
        if args.command == "version":
            emit({"version": __version__, "protocol_version": 1}, as_json=as_json)
            return OK
        if args.command == "config" and args.config_command == "show":
            emit(load_config(root), as_json=as_json)
            return OK
        if args.command == "render":
            result = render(root, dry_run=args.dry_run, no_overwrite=args.no_overwrite)
            emit(result, as_json=as_json)
            return OK
        if args.command == "doctor":
            checks = run_checks(root)
            payload = as_payload(checks)
            emit(payload, as_json=as_json)
            return OK if payload["ok"] or not args.strict else CONFIG_INVALID
        if args.command == "worker" and args.worker_command == "health":
            payload = health(root, parse_envelope(args.envelope_json))
            emit(payload, as_json=as_json)
            return OK
        if args.command == "hook":
            payload = handle_hook(root, args.hook_name, read_payload())
            emit(payload, as_json=True)
            return OK
        if args.command == "memory" and args.memory_command == "append-event":
            payload = append_event(root, read_payload())
            emit(payload, as_json=as_json)
            return OK
        if args.command == "audit" and args.audit_command == "append":
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
            if args.once_json:
                emit(handle_request(root, json.loads(args.once_json)), as_json=True)
                return OK
            return serve_stdio(root)
    except IpcError as exc:
        emit({"ok": False, "error": exc.code, "detail": exc.message}, as_json=True)
        return GENERIC_ERROR
    except SystemExit as exc:
        raise exc
    except Exception as exc:
        emit({"ok": False, "error": str(exc)}, as_json=True)
        return GENERIC_ERROR
    return PERMISSION_DENIED


if __name__ == "__main__":
    raise SystemExit(main())
