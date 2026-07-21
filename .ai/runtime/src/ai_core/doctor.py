from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .config import load_config
from .preflight_proof import PROOF_MAX_AGE_SECONDS, PROOF_SCHEMA, environment_fingerprint
from .private_write import (
    open_root_confined_binary,
    read_root_confined_text,
    validate_root_confined_directory,
)
from .redact import contains_secret, redact_value
from .render import build_manifest
from .trust import parse_simple_toml


JSONL_CHECK_MAX_FILES = 4096
JSONL_CHECK_MAX_FILE_BYTES = 64_000_000
JSONL_CHECK_MAX_TOTAL_BYTES = 256_000_000
JSONL_CHECK_MAX_LINE_BYTES = 2_000_000
JSONL_CHECK_MAX_REPORTED_ERRORS = 20
AUDIT_INDEX_CHECK_MAX_BYTES = 64_000_000
AUDIT_CHECK_MAX_LINE_BYTES = 128_000
AUDIT_CHECK_MAX_RECORDS = 250_000


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(
    root: Path,
    *,
    precomputed_index_status: dict[str, object] | None = None,
    precomputed_session_start_ms: int | None = None,
    lightweight: bool = False,
    update_scan_state: bool = True,
) -> list[Check]:
    index_check = (
        check_index_freshness_from_status(precomputed_index_status)
        if precomputed_index_status is not None
        else check_index_freshness(root)
    )
    hot_path_check = check_hot_path_slo(
        root,
        session_start_ms=precomputed_session_start_ms,
        sample_baseline=not lightweight,
    )
    diagnostics_check = (
        Check("diagnostics_dry_run", True, "deferred: run doctor --strict for full diagnostics smoke")
        if lightweight
        else check_diagnostics(root)
    )
    checks = [
        check_layout(root),
        check_config(root),
        check_gitattributes(root),
        check_sqlite_features(),
        check_index_control(root),
        index_check,
        check_index_storage(root),
        check_manifest(root),
        check_trust(root),
        check_jsonl(root),
        check_generated_artifacts_bounded(root),
        check_runtime_retention(root),
        check_audit_index(root),
        check_audit_chain(root),
        hot_path_check,
        check_secret_scan(
            root,
            incremental=lightweight,
            update_state=update_scan_state,
        ),
        check_no_token_estimates(root),
        check_mcp_methods_registered(root),
        check_redaction_self_test(),
        check_bootstrap_preflight(root),
        check_worker_singleton_lock(root),
        check_queue_lease_recovery(root),
        check_queue_age(root),
        diagnostics_check,
        check_skills_catalog(root),
        check_precall_rules(root),
        check_antigravity_artifacts(root),
        check_lsp_available(root),
        check_pilots(root),
    ]
    return checks


def check_pilots(root: Path) -> Check:
    """INFO-only surfacing of pilot/optional features. ALWAYS ok=True — this never
    fails the gate; it just makes the opt-in switches discoverable in doctor output."""
    try:
        from .pilots import status as pilot_status
        states = pilot_status(root)
    except Exception as exc:  # probing must never break doctor
        return Check("pilots", True, f"probe skipped: {exc}")
    total = len(states)
    on = [info["env"] for info in states.values() if info.get("effective_on")]
    off = [info["env"] for info in states.values() if not info.get("effective_on")]
    parts = [f"{len(on)}/{total} on"]
    if on:
        parts.append("on=" + ",".join(on))
    if off:
        parts.append("off=" + ",".join(off))
    detail = str(redact_value("; ".join(parts)))
    return Check("pilots", True, detail)


def check_lsp_available(root: Path) -> Check:
    """INFO-only probe for optional LSP-grade navigation (G5). NEVER fails the gate — the backend
    is an opt-in extra (multilspy + a language server on PATH); absence is the normal default."""
    try:
        from .lsp import lsp_available
        info = lsp_available(root)
    except Exception as exc:  # probing must never break doctor
        return Check("lsp_available", True, f"probe skipped: {exc}")
    if info.get("ok"):
        servers = ", ".join(info.get("servers_detected") or []) or "?"
        return Check("lsp_available", True, f"ready ({servers})")
    return Check("lsp_available", True, f"optional, inactive: {info.get('reason', 'unknown')}")


def check_antigravity_artifacts(root: Path) -> Check:
    """Verify the workspace's Antigravity wiring is internally consistent.

    Not a hard requirement — Antigravity install is optional — but when the
    workspace HAS opted in (``.agents/`` exists), the two managed artifacts
    must both be well-formed and point at this project's Code Brain.
    """
    agents_dir = root / ".agents"
    if not agents_dir.exists():
        return Check("antigravity_artifacts", True, "not installed")
    mcp = agents_dir / "mcp_config.json"
    hooks = agents_dir / "hooks.json"
    issues: list[str] = []
    if mcp.exists():
        try:
            import json as _json
            payload = _json.loads(mcp.read_text(encoding="utf-8"))
            servers = payload.get("mcpServers", {}) if isinstance(payload, dict) else {}
            if "code-brain" not in servers:
                issues.append("mcp_config.json missing code-brain server")
        except Exception as exc:
            issues.append(f"mcp_config.json unreadable: {exc}")
    if hooks.exists():
        try:
            import json as _json
            payload = _json.loads(hooks.read_text(encoding="utf-8"))
            # Antigravity 1.0.x schema: top-level {name: spec}; spec carries the
            # native events. NOT the Claude {"hooks": {...}} wrapper (Antigravity
            # cannot parse that — it errors "string into jsonhook.JSONHookSpec").
            # Antigravity has no SessionStart/UserPromptSubmit; injection for agy is
            # delivered via the managed AGENTS.md block, not these hooks.
            if not isinstance(payload, dict):
                issues.append("hooks.json is not a JSON object")
            elif "hooks" in payload or "_note" in payload:
                issues.append("hooks.json uses the legacy Claude wrapper; run install-into to regenerate")
            else:
                spec = payload.get("code-brain")
                if not isinstance(spec, dict):
                    issues.append("hooks.json missing code-brain entry")
                else:
                    # PreToolUse is intentionally omitted for Antigravity (its jsonhook contract
                    # is deny-by-default and would block every agy tool call); only the working
                    # side-effect events are required.
                    for required in ("PostToolUse", "Stop"):
                        ev = spec.get(required)
                        if not isinstance(ev, list) or not ev:
                            issues.append(f"hooks.json code-brain missing event {required}")
        except Exception as exc:
            issues.append(f"hooks.json unreadable: {exc}")
    if issues:
        return Check("antigravity_artifacts", False, "; ".join(issues[:5]))
    return Check("antigravity_artifacts", True, "ok")


def check_precall_rules(root: Path) -> Check:
    catalog = root / ".ai" / "precall_rules" / "catalog.jsonl"
    if not catalog.exists():
        return Check("precall_rules", True, "no rules yet")
    try:
        import re as _re
        from .precall_recommend import list_catalog
        entries = list_catalog(root)
    except Exception as exc:
        return Check("precall_rules", False, f"catalog read error: {exc}")
    bad_regex = 0
    stuck_dry_run = 0
    for e in entries:
        try:
            _re.compile(e.pattern)
        except _re.error:
            bad_regex += 1
        if e.status == "dry_run" and e.dry_run_observations > 100:
            stuck_dry_run += 1
    if bad_regex:
        return Check(
            "precall_rules", False,
            f"entries={len(entries)} bad_regex={bad_regex}",
        )
    detail = f"entries={len(entries)} active={sum(1 for e in entries if e.status=='active')}"
    if stuck_dry_run:
        detail += f" stuck_dry_run={stuck_dry_run}"
    return Check("precall_rules", True, detail)


def check_skills_catalog(root: Path) -> Check:
    catalog = root / ".ai" / "skills" / "catalog.jsonl"
    if not catalog.exists():
        return Check("skills_catalog", True, "no catalog yet")
    try:
        from .recommend import _read_marker, _sha256, list_catalog
        entries = list_catalog(root)
    except Exception as exc:
        return Check("skills_catalog", False, f"catalog read error: {exc}")
    drift = 0
    missing = 0
    for entry in entries:
        if entry.status != "installed":
            continue
        for rel in entry.installed_paths:
            path = root / rel
            if not path.exists():
                missing += 1
                continue
            marker = _read_marker(path)
            disk_sha = _sha256(marker.get("__body__", ""))
            if entry.body_sha256 and disk_sha != entry.body_sha256:
                drift += 1
    if missing or drift:
        return Check(
            "skills_catalog",
            False,
            f"installed={sum(1 for e in entries if e.status == 'installed')} drift={drift} missing={missing}",
        )
    return Check(
        "skills_catalog",
        True,
        f"entries={len(entries)} installed={sum(1 for e in entries if e.status == 'installed')}",
    )


def check_layout(root: Path) -> Check:
    required = [
        ".ai/AGENTS.md",
        ".ai/config.yaml",
        ".ai/.gitignore",
        ".ai/.gitattributes",
        ".ai/runtime/pyproject.toml",
        ".ai/runtime/.python-version",
        ".ai/bin/ai",
        ".ai/generated",
        ".ai/memory/audit",
        ".ai/memory/queue/.tmp/.gitkeep",
        ".ai/memory/queue/processing/.gitkeep",
        ".ai/memory/queue/dead/.gitkeep",
    ]
    missing = [item for item in required if not (root / item).exists()]
    return Check("layout", not missing, "ok" if not missing else "missing: " + ", ".join(missing))


def check_config(root: Path) -> Check:
    try:
        config = load_config(root)
    except Exception as exc:
        return Check("config", False, str(exc))
    features = config.get("features", {})
    bad = [key for key in ("embeddings", "remote_llm", "external_notifications") if features.get(key) is not False]
    if bad:
        return Check("config", False, "default-off features enabled: " + ", ".join(bad))
    # remote_memory feature removed (T37) — .ai/ git sync replaces it.
    search = config.get("search", {})
    if not isinstance(search, dict):
        return Check("config", False, "search config must be a mapping")
    retriever = search.get("retriever", "bm25")
    if retriever not in {"bm25", "vector", "hybrid"}:
        return Check("config", False, f"unknown search retriever: {retriever}")
    if retriever != "bm25":
        return Check("config", False, f"search retriever not implemented by default install: {retriever}")
    return Check("config", True, "ok")


def check_gitattributes(root: Path) -> Check:
    path = root / ".ai" / ".gitattributes"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    required = ["*.jsonl merge=union", "memory/daily/*.md merge=union", "*.enc.yaml -merge", "* text=auto eol=lf"]
    missing = [item for item in required if item not in text]
    return Check("gitattributes", not missing, "ok" if not missing else "missing: " + ", ".join(missing))


def check_sqlite_features() -> Check:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("create virtual table docs using fts5(body)")
        conn.execute("select json('{\"ok\": true}')")
    except sqlite3.Error as exc:
        return Check("sqlite_features", False, str(exc))
    finally:
        conn.close()
    return Check("sqlite_features", True, "FTS5 and JSON1 available")


def check_index_freshness(root: Path) -> Check:
    from .index_control import policy as index_policy

    effective_policy = index_policy(root)
    if effective_policy.get("ok") is not True:
        return Check(
            "index_freshness",
            False,
            "invalid index policy: " + "; ".join(str(item) for item in effective_policy.get("errors", [])[:5]),
        )
    if effective_policy.get("enabled") is not True:
        return Check("index_freshness", True, "disabled by operator policy; freshness scan skipped")
    db = root / ".ai" / "cache" / "code.sqlite"
    if not db.exists():
        return Check("index_freshness", True, "not indexed")
    from .search import index_hash_status

    return check_index_freshness_from_status(index_hash_status(root))


def check_index_control(root: Path) -> Check:
    try:
        from .index_control import policy as index_policy, progress_status

        effective_policy = index_policy(root)
        progress = progress_status(root, effective_policy=effective_policy)
    except (OSError, ValueError) as exc:
        return Check("index_control", False, f"unreadable: {exc}")
    if effective_policy.get("ok") is not True:
        return Check(
            "index_control",
            False,
            "invalid policy: " + "; ".join(str(item) for item in effective_policy.get("errors", [])[:5]),
        )
    if progress.get("ok") is not True:
        return Check(
            "index_control",
            False,
            "progress unhealthy: "
            f"state={progress.get('state')} reason={progress.get('reason')} "
            f"stalled={progress.get('stalled')} orphaned={progress.get('orphaned')}",
        )
    mode = "disabled" if effective_policy.get("enabled") is not True else "enabled"
    return Check(
        "index_control",
        True,
        f"{mode}; auto_rebuild={bool(effective_policy.get('auto_rebuild'))}; "
        f"max_files={effective_policy.get('max_files')}; "
        f"max_source_bytes={effective_policy.get('max_source_bytes')}; "
        f"max_seconds={effective_policy.get('max_seconds')}; progress={progress.get('state')}",
    )


def check_index_storage(root: Path) -> Check:
    try:
        from .search import index_storage

        storage = index_storage(root)
    except (OSError, sqlite3.Error) as exc:
        return Check("index_storage", False, f"unreadable: {exc}")
    if not storage.get("exists"):
        return Check("index_storage", True, "not indexed")
    total = int(storage.get("total_bytes") or 0)
    maximum = int(storage.get("max_bytes") or 0)
    if not storage.get("within_limit"):
        return Check(
            "index_storage",
            False,
            f"oversized: total={total}>{maximum}; run `ai index rebuild --json` or raise AI_INDEX_MAX_BYTES",
        )
    return Check(
        "index_storage",
        True,
        f"ok total={total}/{maximum} free_ratio={storage.get('free_ratio', 0.0)}",
    )


def check_index_freshness_from_status(status: dict[str, object]) -> Check:
    raw_policy = status.get("policy")
    if isinstance(raw_policy, dict):
        if raw_policy.get("ok") is not True:
            return Check(
                "index_freshness",
                False,
                "invalid index policy: "
                + "; ".join(str(item) for item in raw_policy.get("errors", [])[:5]),
            )
        if raw_policy.get("enabled") is not True:
            return Check("index_freshness", True, "disabled by operator policy; freshness scan skipped")
    reason = str(status.get("reason") or "unreadable")
    if status.get("ok"):
        return Check("index_freshness", True, f"ok indexed={status.get('indexed_files', 0)}")
    if status.get("stale") is False:
        return Check("index_freshness", True, f"ok indexed={status.get('indexed', 0)}")
    if reason == "missing":
        return Check("index_freshness", True, "not indexed")
    if reason == "legacy_schema":
        return Check("index_freshness", False, "legacy index schema; run ai index rebuild")
    if reason == "unreadable":
        return Check("index_freshness", False, str(status.get("detail") or "index unreadable"))
    if reason == "index_scan_limit":
        return Check("index_freshness", False, str(status.get("detail") or "bounded scan limit exceeded"))
    if reason == "index_policy_invalid":
        return Check("index_freshness", False, str(status.get("detail") or "invalid index policy"))
    raw_changed = status.get("changed_paths") or []
    changed = list(raw_changed) if isinstance(raw_changed, (list, tuple, set)) else []
    if changed:
        return Check("index_freshness", False, "stale: " + ", ".join(changed[:10]))
    return Check("index_freshness", False, reason)


def check_manifest(root: Path) -> Check:
    path = root / ".ai" / "generated" / "manifest.json"
    if not path.exists():
        return Check("manifest", False, "manifest missing; run ai render")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Check("manifest", False, f"invalid json: {exc}")
    expected = build_manifest(root)
    drift_fields = []
    for key in ("schema_version", "embedding", "sqlite_vec", "summarizer", "chunker", "trust"):
        if existing.get(key) != expected.get(key):
            drift_fields.append(key)
    return Check("manifest", not drift_fields, "ok" if not drift_fields else "drift: " + ", ".join(drift_fields))


def check_trust(root: Path) -> Check:
    bad = []
    for path in sorted((root / ".ai" / "trust" / "machines").glob("*.pub.toml")):
        data = parse_simple_toml(path.read_text(encoding="utf-8"))
        public_key = data.get("public_key", "")
        expected_hash = __import__("hashlib").sha256(public_key.strip().encode("utf-8")).hexdigest()
        if data.get("machine_id_hash") != expected_hash:
            bad.append(path.relative_to(root).as_posix())
        if data.get("status") not in {"trusted", "revoked"}:
            bad.append(path.relative_to(root).as_posix() + ":status")
    return Check("trust", not bad, "ok" if not bad else "invalid: " + ", ".join(bad))


def _scan_jsonl_file(path: Path, *, root: Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    issues: list[str] = []
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return [f"{rel}:not-regular"]
        if int(getattr(opened, "st_nlink", 1)) != 1:
            return [f"{rel}:unsafe-hardlink"]
        if int(opened.st_size) > int(JSONL_CHECK_MAX_FILE_BYTES):
            return [f"{rel}:bytes={opened.st_size}>{JSONL_CHECK_MAX_FILE_BYTES}"]

        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            line_no = 0
            while True:
                line = handle.readline(int(JSONL_CHECK_MAX_LINE_BYTES) + 1)
                if not line:
                    break
                line_no += 1
                if len(line) > int(JSONL_CHECK_MAX_LINE_BYTES):
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(64 * 1024)
                    issues.append(f"{rel}:{line_no}:line-byte-limit")
                    continue
                if not line.strip():
                    continue
                try:
                    decoded = line.decode("utf-8", errors="strict")
                    json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    issues.append(f"{rel}:{line_no}")
                if len(issues) >= int(JSONL_CHECK_MAX_REPORTED_ERRORS):
                    break

            final = os.fstat(handle.fileno())
            if (
                int(final.st_dev) != int(opened.st_dev)
                or int(final.st_ino) != int(opened.st_ino)
                or int(final.st_mtime_ns) != int(opened.st_mtime_ns)
                or int(final.st_size) != int(opened.st_size)
            ):
                issues.append(f"{rel}:changed-during-read")
    except OSError as exc:
        issues.append(f"{rel}:untrusted:{exc}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return issues[: int(JSONL_CHECK_MAX_REPORTED_ERRORS)]


def check_jsonl(root: Path) -> Check:
    memory_root = root.joinpath(".ai", "memory")
    try:
        state = memory_root.lstat()
    except FileNotFoundError:
        return Check("jsonl", True, "ok")
    except OSError as exc:
        return Check("jsonl", False, f"invalid: memory-directory-untrusted:{exc}")
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        return Check("jsonl", False, "invalid: memory-directory-untrusted")

    bad: list[str] = []
    checked = 0
    total_bytes = 0
    try:
        candidates = memory_root.rglob("*.jsonl")
        for path in candidates:
            checked += 1
            if checked > int(JSONL_CHECK_MAX_FILES):
                bad.append(f"file-limit={checked}>{JSONL_CHECK_MAX_FILES}")
                break
            try:
                item = path.lstat()
            except OSError as exc:
                bad.append(f"{path.relative_to(root).as_posix()}:stat:{exc}")
                continue
            if stat.S_ISLNK(item.st_mode):
                bad.append(f"{path.relative_to(root).as_posix()}:unsafe-symlink")
                continue
            if not stat.S_ISREG(item.st_mode):
                bad.append(f"{path.relative_to(root).as_posix()}:not-regular")
                continue
            if int(getattr(item, "st_nlink", 1)) != 1:
                bad.append(f"{path.relative_to(root).as_posix()}:unsafe-hardlink")
                continue
            total_bytes += max(0, int(item.st_size))
            if total_bytes > int(JSONL_CHECK_MAX_TOTAL_BYTES):
                bad.append(f"aggregate-bytes={total_bytes}>{JSONL_CHECK_MAX_TOTAL_BYTES}")
                break
            bad.extend(_scan_jsonl_file(path, root=root))
            if len(bad) >= int(JSONL_CHECK_MAX_REPORTED_ERRORS):
                break
    except OSError as exc:
        bad.append(f"memory-scan-untrusted:{exc}")

    if bad:
        return Check(
            "jsonl",
            False,
            "invalid: " + ", ".join(bad[: int(JSONL_CHECK_MAX_REPORTED_ERRORS)]),
        )
    return Check("jsonl", True, f"ok files={checked} bytes={total_bytes}")


def check_generated_artifacts_bounded(root: Path) -> Check:
    from .evidence import EVIDENCE_MAX_BYTES, evidence_path
    from .memory import (
        _SESSION_NOTE_MAX_BYTES,
        AUDIT_MAX_BYTES,
        AUDIT_RETENTION_YEARS,
        EVENTS_MAX_BYTES,
        all_audit_files,
        events_path,
        session_current_path,
    )
    from .prompt_growth import PROMPT_GROWTH_MAX_BYTES, log_path

    targets = (
        (events_path(root), EVENTS_MAX_BYTES),
        (log_path(root), PROMPT_GROWTH_MAX_BYTES),
        (evidence_path(root), EVIDENCE_MAX_BYTES),
        (session_current_path(root), _SESSION_NOTE_MAX_BYTES),
    )
    oversized: list[str] = []
    for path, cap in targets:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > cap:
            oversized.append(f"{path.relative_to(root).as_posix()}={size}>{cap}")
    audit_files = all_audit_files(root)
    for path in audit_files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > AUDIT_MAX_BYTES:
            oversized.append(f"{path.relative_to(root).as_posix()}={size}>{AUDIT_MAX_BYTES}")
    year_files = {
        int(path.stem[:4])
        for path in audit_files
        if len(path.stem) >= 4 and path.stem[:4].isdigit()
    }
    if len(year_files) > max(1, int(AUDIT_RETENTION_YEARS)):
        oversized.append(
            f"audit_years={len(year_files)}>{max(1, int(AUDIT_RETENTION_YEARS))}"
        )
    if oversized:
        return Check(
            "generated_artifacts_bounded",
            False,
            "oversized: " + ", ".join(oversized[:8]) + "; run ai memory page-out --json",
        )
    return Check("generated_artifacts_bounded", True, f"ok checked={len(targets) + len(audit_files)}")


def check_runtime_retention(root: Path) -> Check:
    try:
        from .obs import diagnostics_retention_status, logs_retention_status
        from .upgrade import upgrade_backup_retention_status

        statuses = {
            "logs": logs_retention_status(root),
            "diagnostics": diagnostics_retention_status(root),
            "upgrade": upgrade_backup_retention_status(root),
        }
    except (OSError, ValueError) as exc:
        return Check("runtime_retention", False, f"unreadable: {exc}")
    failed = [
        f"{name}:{','.join(str(item) for item in status.get('violations', []))}"
        for name, status in statuses.items()
        if not status.get("ok")
    ]
    if failed:
        return Check(
            "runtime_retention",
            False,
            "invalid: " + "; ".join(failed) + "; run diagnostics prune and remove stale upgrade backups",
        )
    detail = ", ".join(
        f"{name}={status.get('count', 0)}/{status.get('bytes', 0)}B"
        for name, status in statuses.items()
    )
    return Check("runtime_retention", True, "ok " + detail)


def _iter_bounded_jsonl(
    path: Path,
    *,
    root: Path,
    max_bytes: int,
    max_records: int,
) -> Iterator[tuple[int, str | None, dict[str, object] | None, str | None]]:
    """Yield bounded JSONL rows while retaining only one line in memory."""
    records = 0
    with open_root_confined_binary(
        path,
        root=root,
        max_bytes=max_bytes,
        require_private=False,
    ) as (handle, _state):
        line_no = 0
        while True:
            raw = handle.readline(int(AUDIT_CHECK_MAX_LINE_BYTES) + 1)
            if not raw:
                break
            line_no += 1
            if len(raw) > int(AUDIT_CHECK_MAX_LINE_BYTES):
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(64 * 1024)
                yield line_no, None, None, "line-byte-limit"
                continue
            try:
                line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
            except UnicodeDecodeError:
                yield line_no, None, None, "invalid_utf8"
                continue
            if not line.strip():
                continue
            records += 1
            if records > max(0, int(max_records)):
                yield line_no, None, None, f"record-limit={records}>{max(0, int(max_records))}"
                return
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_no, line, None, f"invalid_json:{exc.msg}"
                continue
            if not isinstance(loaded, dict):
                yield line_no, line, None, "not_object"
                continue
            yield line_no, line, loaded, None


def audit_key(
    record: dict[str, object],
    path: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    values = (
        record.get("ts"),
        record.get("action"),
        record.get("category"),
        path if path is not None else record.get("path"),
    )
    if any(value is not None and not isinstance(value, str) for value in values):
        raise ValueError("audit key fields must be strings")
    return values  # type: ignore[return-value]


def check_audit_index(root: Path) -> Check:
    from .memory import AUDIT_MAX_BYTES, all_audit_files

    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    bad: list[str] = []
    try:
        validate_root_confined_directory(audit_root, root=root)
    except FileNotFoundError:
        pass
    except OSError as exc:
        bad.append(f"audit-directory-untrusted:{exc}")
    index_keys: dict[tuple[str | None, str | None, str | None, str | None], bool] = {}
    try:
        for line_no, _line, record, issue in _iter_bounded_jsonl(
            index_path,
            root=root,
            max_bytes=AUDIT_INDEX_CHECK_MAX_BYTES,
            max_records=AUDIT_CHECK_MAX_RECORDS,
        ):
            if issue is not None:
                bad.append(f"audit-index:line {line_no}:{issue}")
                continue
            assert record is not None
            rel_path = record.get("path")
            if not isinstance(rel_path, str):
                bad.append(f"audit-index:line {line_no}:path")
                continue
            target = root / rel_path
            try:
                target_state = target.lstat()
            except OSError:
                target_state = None
            if (
                target_state is None
                or not stat.S_ISREG(target_state.st_mode)
                or stat.S_ISLNK(target_state.st_mode)
                or int(getattr(target_state, "st_nlink", 1)) != 1
                or target.parent != audit_root
                or target.suffix != ".jsonl"
            ):
                bad.append(rel_path)
                continue
            try:
                index_keys[audit_key(record)] = False
            except ValueError as exc:
                bad.append(f"audit-index:line {line_no}:{exc}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        bad.append(f"audit-index-untrusted:{exc}")

    audit_records = 0
    stop = False
    for path in all_audit_files(root):
        if stop:
            break
        rel_path = path.relative_to(root).as_posix()
        try:
            for line_no, _line, record, issue in _iter_bounded_jsonl(
                path,
                root=root,
                max_bytes=AUDIT_MAX_BYTES,
                max_records=max(0, AUDIT_CHECK_MAX_RECORDS - audit_records),
            ):
                if issue is not None:
                    bad.append(f"{rel_path}:line {line_no}:{issue}")
                    if issue.startswith("record-limit="):
                        stop = True
                    continue
                assert record is not None
                audit_records += 1
                try:
                    key = audit_key(record, rel_path)
                except ValueError as exc:
                    bad.append(f"{rel_path}:line {line_no}:{exc}")
                    continue
                if key not in index_keys:
                    bad.append(f"{rel_path}:missing-index:{record.get('ts')}")
                else:
                    index_keys[key] = True
        except OSError as exc:
            bad.append(f"{rel_path}:untrusted:{exc}")

    for key, matched in sorted(index_keys.items(), key=lambda item: str(item[0])):
        if not matched:
            bad.append(f"audit-index:orphan:{key[0]}")

    return Check("audit_index", not bad, "ok" if not bad else "invalid: " + ", ".join(bad[:10]))


def check_audit_chain(root: Path) -> Check:
    from .memory import AUDIT_MAX_BYTES, all_audit_files

    audit_root = root / ".ai" / "memory" / "audit"
    bad: list[str] = []
    chained = 0
    try:
        validate_root_confined_directory(audit_root, root=root)
    except FileNotFoundError:
        detail = "ok no chained lines yet"
        return Check("audit_chain", True, detail)
    except OSError as exc:
        return Check("audit_chain", False, f"invalid: audit-directory-untrusted:{exc}")

    for path in all_audit_files(root):
        previous_line: str | None = None
        previous_was_chained = False
        rel_path = path.relative_to(root).as_posix()
        try:
            rows = _iter_bounded_jsonl(
                path,
                root=root,
                max_bytes=AUDIT_MAX_BYTES,
                max_records=AUDIT_CHECK_MAX_RECORDS,
            )
            for line_no, line, record, issue in rows:
                if issue is not None:
                    bad.append(f"{rel_path}:line {line_no}:{issue}")
                    previous_line = line
                    previous_was_chained = False
                    continue
                assert line is not None and record is not None
                is_chained = "prev_sha" in record
                if is_chained:
                    chained += 1
                    prev_sha = record.get("prev_sha")
                    if prev_sha is not None and not (isinstance(prev_sha, str) and len(prev_sha) == 64):
                        bad.append(f"{rel_path}:line {line_no}:prev_sha_invalid")
                    expected = hashlib.sha256(previous_line.encode("utf-8")).hexdigest() if previous_line is not None else None
                    if (previous_was_chained or previous_line is None) and prev_sha != expected:
                        bad.append(f"{rel_path}:line {line_no}:prev_sha_mismatch")

                previous_line = line
                previous_was_chained = is_chained
        except OSError as exc:
            bad.append(f"{rel_path}:untrusted:{exc}")

    if bad:
        # Add actionable remediation hint — chain damage usually comes from stash
        # union merges or partial restore, both of which `ai audit repair-chain`
        # can fix deterministically without dropping content.
        return Check(
            "audit_chain",
            False,
            "invalid: " + ", ".join(bad[:10]) + " — run `ai audit repair-chain` to fix",
        )
    detail = f"ok chained_lines={chained}" if chained else "ok no chained lines yet"
    return Check("audit_chain", True, detail)


# Wall-clock hot-path timings vary widely across machines (cold caches, slow or
# shared CI runners, the Windows job that strips CI markers). This gate is a coarse
# guard against GROSS regressions, not a per-runner benchmark, so it fails only at a
# generous multiple of the target. Determinism does NOT depend on is_ci() — the
# Windows portability job unsets CI/GITHUB_ACTIONS, so an is_ci()-conditional relax
# would silently not apply there and the gate would flake again.
SLO_GATE_HEADROOM = 3


def check_hot_path_slo(
    root: Path,
    *,
    session_start_ms: int | None = None,
    sample_baseline: bool = True,
) -> Check:
    from .hooks import HOT_PATH_TARGET_MS, SESSION_START_TARGET_MS, handle_hook

    def best_elapsed_ms(hook: str, n: int) -> int:
        # Best-of-N: a single sample is dominated by scheduler/GC/cold-cache noise;
        # the SLO is about steady-state hot-path cost.
        return min(
            int(handle_hook(root, hook, {"agent": "doctor", "dry": True})["elapsed_ms"])
            for _ in range(n)
        )

    samples = []
    if sample_baseline:
        for _ in range(10):
            payload = handle_hook(root, "DoctorSLOBaseline", {"agent": "doctor", "dry": True})
            samples.append(int(payload["elapsed_ms"]))
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else None
    start_ms = (
        max(0, int(session_start_ms))
        if session_start_ms is not None
        else best_elapsed_ms("SessionStart", 5)
    )

    ok = (
        (p95 is None or p95 <= HOT_PATH_TARGET_MS * SLO_GATE_HEADROOM)
        and start_ms <= SESSION_START_TARGET_MS * SLO_GATE_HEADROOM
    )
    return Check(
        "hot_path_slo",
        ok,
        f"p95_ms={p95 if p95 is not None else 'deferred'}, target_ms={HOT_PATH_TARGET_MS}, "
        f"session_start_ms={start_ms}, "
        f"session_start_target_ms={SESSION_START_TARGET_MS}",
    )


def check_secret_scan(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> Check:
    from .tracked_files import GitBaselineUnavailable

    allowlist = read_secret_scan_allowlist(root)
    flagged: list[str] = []
    acknowledged: list[str] = []
    try:
        report = secret_scan_report(
            root,
            incremental=incremental,
            update_state=update_state,
        )
    except GitBaselineUnavailable:
        mode = "incremental" if incremental else "full"
        return Check(
            "secret_scan",
            False,
            f"Git tracked-file baseline unavailable; mode={mode}; remediation: restore Git access and rerun doctor",
        )
    for hit in report["hits"]:
        (acknowledged if hit in allowlist else flagged).append(hit)
    scan_detail = (
        f"mode={report['mode']} baseline={report['baseline']} total={report['total']} "
        f"reused={report['reused']} "
        f"rescanned={report['rescanned']} unreadable={report['unreadable']} "
        f"unstable={report['unstable']}"
    )
    if flagged:
        detail = (
            f"flagged={len(flagged)} acknowledged={len(acknowledged)} "
            f"allowlist=.ai/secret_scan_allowlist.txt: " + ", ".join(flagged[:10]) + f"; {scan_detail}"
        )
        return Check("secret_scan", False, detail)
    if acknowledged:
        return Check(
            "secret_scan",
            True,
            f"ok (flagged=0 acknowledged={len(acknowledged)} via allowlist); {scan_detail}",
        )
    return Check("secret_scan", True, f"ok; {scan_detail}")


def read_secret_scan_allowlist(root: Path) -> set[str]:
    entries: set[str] = {
        ".ai/runtime/tests/test_failure_memory.py",
        ".ai/runtime/tests/test_posttooluse_wire.py",
    }
    path = root / ".ai" / "secret_scan_allowlist.txt"
    if not path.exists():
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped)
    return entries


FORBIDDEN_TOKEN_ESTIMATE_KEYWORDS = (
    "estimated_tokens",
    "tokens_estimated",
    "tokens_saved",
    "token_savings",
    "estimated_token_savings",
    "estimate_tokens(",
    "guess_tokens(",
)

TOKEN_ESTIMATE_GUARDED_FILES = (
    "obs.py",
    "report.py",
    "session.py",
    "transcripts.py",
    "search.py",
)


def check_no_token_estimates(root: Path) -> Check:
    base = root / ".ai" / "runtime" / "src" / "ai_core"
    if not base.exists():
        return Check("no_token_estimates", True, "ai_core not found")
    offenders: list[str] = []
    for name in TOKEN_ESTIMATE_GUARDED_FILES:
        path = base / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for keyword in FORBIDDEN_TOKEN_ESTIMATE_KEYWORDS:
            if keyword in text:
                offenders.append(f"{name}:{keyword}")
    if offenders:
        return Check("no_token_estimates", False, "estimates leaked: " + ", ".join(offenders[:10]))
    return Check("no_token_estimates", True, f"ok ({len(TOKEN_ESTIMATE_GUARDED_FILES)} files scanned)")


REQUIRED_SLASH_COMMAND_FILES = (
    ".claude/commands/cb-usage.md",
    ".claude/commands/cb-health.md",
    ".claude/commands/cb-search.md",
    ".claude/commands/cb-doctor.md",
    ".claude/commands/cb-exec.md",
    ".claude/commands/cb-upgrade.md",
)

REQUIRED_CODEX_PROMPT_FILES = (
    ".codex/prompts/cb-usage.md",
    ".codex/prompts/cb-health.md",
    ".codex/prompts/cb-search.md",
    ".codex/prompts/cb-doctor.md",
    ".codex/prompts/cb-exec.md",
    ".codex/prompts/cb-upgrade.md",
)


def check_mcp_methods_registered(root: Path) -> Check:
    from .mcp_catalog_meta import MCP_METHOD_COUNT

    mcp_config = root / ".mcp.json"
    if not mcp_config.exists():
        return Check("mcp_methods_registered", False, ".mcp.json missing")
    try:
        config = json.loads(mcp_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("mcp_methods_registered", False, f".mcp.json invalid: {exc}")
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or "code-brain" not in servers:
        return Check("mcp_methods_registered", False, ".mcp.json missing mcpServers.code-brain entry")
    server = servers["code-brain"]
    if not isinstance(server, dict):
        return Check("mcp_methods_registered", False, ".mcp.json code-brain server is not an object")
    command = server.get("command")
    args = server.get("args", [])
    arg_text = " ".join(str(arg) for arg in args)
    unix_entry = command == ".ai/bin/ai-mcp"
    windows_entry = command in {"powershell", "pwsh"} and ".ai/bin/ai-mcp.ps1" in arg_text
    if not (unix_entry or windows_entry):
        return Check("mcp_methods_registered", False, ".mcp.json code-brain command is not a managed ai-mcp entry")
    missing_slash = [path for path in REQUIRED_SLASH_COMMAND_FILES if not (root / path).exists()]
    missing_codex = [path for path in REQUIRED_CODEX_PROMPT_FILES if not (root / path).exists()]
    if missing_slash:
        return Check("mcp_methods_registered", False, "missing claude commands: " + ", ".join(missing_slash))
    if missing_codex:
        return Check("mcp_methods_registered", False, "missing codex prompts: " + ", ".join(missing_codex))
    return Check(
        "mcp_methods_registered",
        True,
        f"ok mcp_methods={MCP_METHOD_COUNT} claude_commands={len(REQUIRED_SLASH_COMMAND_FILES)} "
        f"codex_prompts={len(REQUIRED_CODEX_PROMPT_FILES)}",
    )


def check_redaction_self_test() -> Check:
    samples = [
        "AKIA" + "A" * 16,
        "ghp_" + "a" * 36,
        "gho_" + "b" * 36,
        "github_pat_" + "c" * 28,
        "sk-" + "d" * 32,
        "sk-ant-" + "e" * 32,
        "xoxb-" + "1-2-" + "f" * 24,
        "Authorization: Bearer " + "eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + "." + "c" * 20,
        "token=" + "g" * 24,
        "-----BEGIN " + "PRIVATE KEY-----\n" + "h" * 32 + "\n-----END " + "PRIVATE KEY-----",
        "/Users/example/project",
        "/home/example/project",
        "C:\\Users\\example\\project",
        "192.168.1.10",
    ]
    redacted = redact_value({"samples": samples})
    text = json.dumps(redacted, sort_keys=True)
    leaked = [sample for sample in samples if sample in text]
    return Check("redaction_self_test", not leaked and "[REDACTED]" in text, "ok" if not leaked else "leaked: " + str(len(leaked)))


def _root_confined_regular_file(root: Path, path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        state = path.stat()
        if not stat.S_ISREG(state.st_mode):
            return False
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def check_bootstrap_preflight(root: Path) -> Check:
    script = root / "scripts" / "preflight.sh"
    if not _root_confined_regular_file(root, script):
        return Check(
            "bootstrap_preflight",
            False,
            "scripts/preflight.sh must be a root-confined regular file",
        )
    proof = _fresh_bootstrap_preflight_proof(root, script)
    if proof is not None:
        return proof
    command = [str(script), "--check-only", "--json"]
    env = os.environ.copy()
    if os.name == "nt":
        bash = shutil.which("bash") or shutil.which("bash.exe")
        if not bash:
            for candidate in (
                Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    bash = str(candidate)
                    break
        if not bash:
            return Check("bootstrap_preflight", False, "bash not found")
        command = [bash, str(script), "--check-only", "--json"]
        scripts_dir = root / ".ai" / "runtime" / ".venv" / "Scripts"
        env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        return Check("bootstrap_preflight", False, str(exc))
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return Check("bootstrap_preflight", False, detail[:500])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return Check("bootstrap_preflight", False, f"invalid json: {exc}")
    return Check("bootstrap_preflight", payload.get("ok") is True, "ok" if payload.get("ok") is True else "failed")


def _fresh_bootstrap_preflight_proof(root: Path, script: Path) -> Check | None:
    proof_path = root / ".ai" / "cache" / "preflight-proof.json"
    try:
        if not proof_path.is_file() or proof_path.is_symlink():
            return None
        resolved_root = root.resolve()
        proof_path.resolve().relative_to(resolved_root)
        proof_state = proof_path.stat()
        if os.name != "nt":
            if stat.S_IMODE(proof_state.st_mode) & 0o077:
                return None
            if hasattr(os, "geteuid") and proof_state.st_uid != os.geteuid():
                return None
        proof_text, proof_state = read_root_confined_text(
            proof_path,
            root=root,
            max_bytes=65536,
            require_private=True,
        )
        payload = json.loads(proof_text)
        created_at = float(payload.get("created_at_unix", 0))
        age = time.time() - created_at
        if age < -5 or age > PROOF_MAX_AGE_SECONDS:
            return None
        if payload.get("schema") != PROOF_SCHEMA or payload.get("ok") is not True:
            return None
        expected_script = hashlib.sha256(script.read_bytes()).hexdigest()
        if payload.get("preflight_sha256") != expected_script:
            return None
        expected_root = hashlib.sha256(str(resolved_root).encode("utf-8")).hexdigest()
        if payload.get("root_fingerprint") != expected_root:
            return None
        if payload.get("environment_fingerprint") != environment_fingerprint(resolved_root):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return Check("bootstrap_preflight", True, "ok (fresh bootstrap proof)")


def check_worker_singleton_lock(root: Path) -> Check:
    from .worker.lock import lock_status

    status = lock_status(root)
    if status.get("stale"):
        return Check("worker_singleton_lock", False, json.dumps(status, sort_keys=True))
    return Check("worker_singleton_lock", status.get("ok") is True, "ok" if status.get("ok") is True else json.dumps(status, sort_keys=True))


def check_queue_lease_recovery(root: Path) -> Check:
    from .worker.scheduler import RECOVERY_STALE_SECONDS, expired_processing_jobs, recovery_status

    expired = expired_processing_jobs(root)
    state = recovery_status(root)
    if expired:
        return Check("queue_lease_recovery", False, "expired processing jobs: " + json.dumps(expired[:5], sort_keys=True))
    if state.get("state") == "invalid":
        return Check("queue_lease_recovery", False, "invalid recovery state")
    lag = state.get("lag_seconds")
    if isinstance(lag, int) and lag > RECOVERY_STALE_SECONDS:
        return Check("queue_lease_recovery", False, f"recovery state stale lag={lag}s")
    detail = "ok" if lag is None else f"ok lag={lag}s"
    return Check("queue_lease_recovery", True, detail)


def check_queue_age(root: Path) -> Check:
    from .worker.scheduler import QUEUE_PENDING_AGE_STALE_SECONDS, QUEUE_PROCESSING_AGE_STALE_SECONDS, queue_age_stats

    stats = queue_age_stats(root)
    pending_age = int(stats["oldest_pending_age_seconds"])
    processing_age = int(stats["oldest_processing_age_seconds"])
    failures = []
    if pending_age > QUEUE_PENDING_AGE_STALE_SECONDS:
        failures.append(
            "oldest pending job "
            f"{stats.get('oldest_pending_job_id')} age={pending_age}s threshold={QUEUE_PENDING_AGE_STALE_SECONDS}s"
        )
    if processing_age > QUEUE_PROCESSING_AGE_STALE_SECONDS:
        failures.append(
            "oldest processing job "
            f"{stats.get('oldest_processing_job_id')} age={processing_age}s threshold={QUEUE_PROCESSING_AGE_STALE_SECONDS}s"
        )
    if failures:
        return Check("queue_age", False, "; ".join(failures))
    skipped = int(stats.get("age_stats_skipped", 0))
    detail = f"ok pending_age={pending_age}s processing_age={processing_age}s"
    if skipped:
        detail += f" skipped={skipped}"
    return Check("queue_age", True, detail)


def check_diagnostics(root: Path) -> Check:
    try:
        from .obs import diagnostics

        # Doctor only needs to prove that diagnostics can be assembled. Full
        # Claude/Codex transcript scans belong to an explicit diagnostics or
        # metrics request and can take seconds on a long-lived workstation.
        payload = diagnostics(root, dry_run=True, include_doctor=False, include_usage=False)
    except PermissionError as exc:
        # diagnostics walks metrics paths which may include files outside the
        # Code Brain managed tree (e.g. ~/.claude/projects/*.jsonl owned by a
        # different user when Code Brain is invoked under sudo on a shared
        # host). That is an environment fact, not a Code Brain failure —
        # skip with detail instead of failing strict.
        return Check("diagnostics_dry_run", True, f"skipped: permission denied ({exc})")
    except Exception as exc:
        return Check("diagnostics_dry_run", False, str(exc))
    return Check("diagnostics_dry_run", bool(payload.get("ok")), "ok" if payload.get("ok") else "failed")


SECRET_SCAN_IGNORED_PARTS = {
    ".venv",
    "cache",
    ".git",
    ".claude",
    ".codebrain",
    "node_modules",
    ".next",
    ".nuxt",
    ".output",
    "dist",
    "build",
    "coverage",
    "logs",
    ".playwright-mcp",
    ".dart_tool",
    "source-maps",
}

SECRET_SCAN_IGNORED_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "poetry.lock",
    "firebase_options.dart",
}

SECRET_SCAN_IGNORED_SUFFIXES = {
    ".map",
    ".min.js",
    ".min.css",
}


class _SecretCandidateList(list[Path]):
    def __init__(self, paths: list[Path], *, baseline: str) -> None:
        super().__init__(paths)
        self.baseline = baseline


def _secret_scan_candidates(
    root: Path,
    *,
    use_tracked_cache: bool = True,
    update_tracked_cache: bool = True,
) -> _SecretCandidateList:
    candidates: list[Path] = []
    baseline_paths = secret_scan_files(
        root,
        use_cache=use_tracked_cache,
        update_cache=update_tracked_cache,
    )
    baseline = str(getattr(baseline_paths, "source", "provided"))
    for path in baseline_paths:
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & SECRET_SCAN_IGNORED_PARTS:
            continue
        if path.name in SECRET_SCAN_IGNORED_NAMES:
            continue
        if any(path.name.endswith(suffix) for suffix in SECRET_SCAN_IGNORED_SUFFIXES):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".sqlite", ".db", ".rdb", ".zip", ".gz", ".tar"}:
            continue
        try:
            state = path.lstat()
        except OSError:
            # A tracked path can disappear or be replaced between the Git
            # baseline read and candidate filtering. Preserve the existing
            # fail-soft unreadable-file policy instead of aborting doctor.
            continue
        if not (stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode)):
            continue
        if state.st_size > 1_000_000:
            continue
        if path.name.endswith(".enc.yaml") or path.name.endswith(".enc.yml"):
            continue
        candidates.append(path)
    return _SecretCandidateList(candidates, baseline=baseline)


def secret_hits(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> Iterable[str]:
    yield from secret_scan_report(
        root,
        incremental=incremental,
        update_state=update_state,
    )["hits"]


def secret_scan_report(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> dict[str, object]:
    from .scan_state import scan_paths_report

    candidates = _secret_scan_candidates(
        root,
        use_tracked_cache=incremental,
        update_tracked_cache=update_state,
    )
    report = scan_paths_report(
        root,
        candidates,
        incremental=incremental,
        update_state=update_state,
    )
    report["baseline"] = candidates.baseline
    return report


def secret_scan_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
) -> list[Path]:
    from .tracked_files import tracked_files

    return tracked_files(root, use_cache=use_cache, update_cache=update_cache)


def as_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks],
    }
