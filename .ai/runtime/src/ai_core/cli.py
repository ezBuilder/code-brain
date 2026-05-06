from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_config
from .doctor import as_payload, run_checks
from .paths import find_repo_root
from .policy import CONFIG_INVALID, GENERIC_ERROR, OK, PERMISSION_DENIED, reject_ci_write
from .render import render
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
    sub.add_parser("hook")
    sub.add_parser("mcp")
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
        if args.command in {"hook", "mcp"}:
            emit({"ok": False, "error": "not implemented in M1 scaffold"}, as_json=True)
            return GENERIC_ERROR
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
