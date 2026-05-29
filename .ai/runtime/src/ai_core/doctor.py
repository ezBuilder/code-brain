from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import load_config
from .redact import SECRET_PATTERNS
from .redact import redact_value
from .render import build_manifest
from .trust import parse_simple_toml


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(root: Path) -> list[Check]:
    checks = [
        check_layout(root),
        check_config(root),
        check_gitattributes(root),
        check_sqlite_features(),
        check_index_freshness(root),
        check_manifest(root),
        check_trust(root),
        check_jsonl(root),
        check_audit_index(root),
        check_audit_chain(root),
        check_hot_path_slo(root),
        check_secret_scan(root),
        check_no_token_estimates(root),
        check_mcp_methods_registered(root),
        check_redaction_self_test(),
        check_bootstrap_preflight(root),
        check_worker_singleton_lock(root),
        check_queue_lease_recovery(root),
        check_queue_age(root),
        check_diagnostics(root),
        check_skills_catalog(root),
        check_precall_rules(root),
        check_antigravity_artifacts(root),
    ]
    return checks


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
                    for required in ("PreToolUse", "PostToolUse", "Stop"):
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
        ".ai/memory/audit-index.jsonl",
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
    db = root / ".ai" / "cache" / "code.sqlite"
    if not db.exists():
        return Check("index_freshness", True, "not indexed")
    stale: list[str] = []
    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row["name"] for row in conn.execute("pragma table_info(chunks)").fetchall()}
            if "content" in columns or "summary" not in columns or not {"path", "sha256"}.issubset(columns):
                return Check("index_freshness", False, "legacy index schema; run ai index rebuild")
            rows = conn.execute("select path, sha256 from chunks order by path").fetchall()
    except sqlite3.Error as exc:
        return Check("index_freshness", False, f"index unreadable: {exc}")
    seen: set[str] = set()
    for row in rows:
        rel_path = str(row["path"])
        # Function-level chunks store "<file>:<qualname>"; file-level chunks
        # cover freshness for the whole file. Skip the per-function rows so
        # they don't appear as missing file paths.
        if ":" in rel_path:
            continue
        if rel_path in seen:
            continue
        seen.add(rel_path)
        path = root / rel_path
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            stale.append(rel_path)
            continue
        redacted = str(redact_value(content))
        if hashlib.sha256(redacted.encode("utf-8")).hexdigest() != row["sha256"]:
            stale.append(rel_path)
        if len(stale) >= 10:
            break
    if stale:
        return Check("index_freshness", False, "stale: " + ", ".join(stale))
    return Check("index_freshness", True, f"ok indexed={len(rows)}")


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


def check_jsonl(root: Path) -> Check:
    bad: list[str] = []
    for path in root.joinpath(".ai", "memory").rglob("*.jsonl"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad.append(f"{path.relative_to(root)}:{line_no}")
    return Check("jsonl", not bad, "ok" if not bad else "invalid: " + ", ".join(bad))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                records.append(loaded)
    return records


def audit_key(record: dict[str, object], path: str | None = None) -> tuple[object, object, object, object]:
    return (record.get("ts"), record.get("action"), record.get("category"), path or record.get("path"))


def check_audit_index(root: Path) -> Check:
    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    bad: list[str] = []
    index_records = read_jsonl(index_path)
    index_keys = {audit_key(record) for record in index_records}

    for record in index_records:
        rel_path = record.get("path")
        if not isinstance(rel_path, str):
            bad.append("audit-index:path")
            continue
        target = root / rel_path
        if not target.exists() or target.parent != audit_root or target.suffix != ".jsonl":
            bad.append(rel_path)

    audit_keys: set[tuple[object, object, object, object]] = set()
    for path in sorted(audit_root.glob("*.jsonl")):
        rel_path = path.relative_to(root).as_posix()
        for record in read_jsonl(path):
            key = audit_key(record, rel_path)
            audit_keys.add(key)
            if key not in index_keys:
                bad.append(f"{rel_path}:missing-index:{record.get('ts')}")

    for key in sorted(index_keys - audit_keys, key=str):
        bad.append(f"audit-index:orphan:{key[0]}")

    return Check("audit_index", not bad, "ok" if not bad else "invalid: " + ", ".join(bad[:10]))


def check_audit_chain(root: Path) -> Check:
    audit_root = root / ".ai" / "memory" / "audit"
    bad: list[str] = []
    chained = 0

    for path in sorted(audit_root.glob("*.jsonl")):
        previous_line: str | None = None
        previous_was_chained = False
        rel_path = path.relative_to(root).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                bad.append(f"{rel_path}:line {line_no}:invalid_json:{exc.msg}")
                previous_line = line
                previous_was_chained = False
                continue
            if not isinstance(record, dict):
                bad.append(f"{rel_path}:line {line_no}:not_object")
                previous_line = line
                previous_was_chained = False
                continue

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


def check_hot_path_slo(root: Path) -> Check:
    from .hooks import HOT_PATH_TARGET_MS, SESSION_START_TARGET_MS, handle_hook

    samples = []
    for _ in range(10):
        payload = handle_hook(root, "DoctorSLOBaseline", {"agent": "doctor", "dry": True})
        samples.append(int(payload["elapsed_ms"]))
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else 0
    start_payload = handle_hook(root, "SessionStart", {"agent": "doctor", "dry": True})
    start_ms = int(start_payload["elapsed_ms"])
    ok = p95 <= HOT_PATH_TARGET_MS and start_ms <= SESSION_START_TARGET_MS
    return Check(
        "hot_path_slo",
        ok,
        f"p95_ms={p95}, target_ms={HOT_PATH_TARGET_MS}, session_start_ms={start_ms}, "
        f"session_start_target_ms={SESSION_START_TARGET_MS}",
    )


def check_secret_scan(root: Path) -> Check:
    allowlist = read_secret_scan_allowlist(root)
    flagged: list[str] = []
    acknowledged: list[str] = []
    for hit in secret_hits(root):
        (acknowledged if hit in allowlist else flagged).append(hit)
    if flagged:
        detail = (
            f"flagged={len(flagged)} acknowledged={len(acknowledged)} "
            f"allowlist=.ai/secret_scan_allowlist.txt: " + ", ".join(flagged[:10])
        )
        return Check("secret_scan", False, detail)
    if acknowledged:
        return Check("secret_scan", True, f"ok (flagged=0 acknowledged={len(acknowledged)} via allowlist)")
    return Check("secret_scan", True, "ok")


def read_secret_scan_allowlist(root: Path) -> set[str]:
    path = root / ".ai" / "secret_scan_allowlist.txt"
    if not path.exists():
        return set()
    entries: set[str] = set()
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
)

REQUIRED_CODEX_PROMPT_FILES = (
    ".codex/prompts/cb-usage.md",
    ".codex/prompts/cb-health.md",
    ".codex/prompts/cb-search.md",
    ".codex/prompts/cb-doctor.md",
    ".codex/prompts/cb-exec.md",
)


def check_mcp_methods_registered(root: Path) -> Check:
    from .mcp_server import MCP_METHODS

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
    if not isinstance(server, dict) or server.get("command") != ".ai/bin/ai-mcp":
        return Check("mcp_methods_registered", False, ".mcp.json code-brain.command is not .ai/bin/ai-mcp")
    missing_slash = [path for path in REQUIRED_SLASH_COMMAND_FILES if not (root / path).exists()]
    missing_codex = [path for path in REQUIRED_CODEX_PROMPT_FILES if not (root / path).exists()]
    method_count = len(MCP_METHODS)
    if missing_slash:
        return Check("mcp_methods_registered", False, "missing claude commands: " + ", ".join(missing_slash))
    if missing_codex:
        return Check("mcp_methods_registered", False, "missing codex prompts: " + ", ".join(missing_codex))
    return Check(
        "mcp_methods_registered",
        True,
        f"ok mcp_methods={method_count} claude_commands={len(REQUIRED_SLASH_COMMAND_FILES)} "
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


def check_bootstrap_preflight(root: Path) -> Check:
    script = root / "scripts" / "preflight.sh"
    if not script.exists():
        return Check("bootstrap_preflight", False, "scripts/preflight.sh missing")
    result = subprocess.run(
        [str(script), "--check-only", "--json"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return Check("bootstrap_preflight", False, detail[:500])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return Check("bootstrap_preflight", False, f"invalid json: {exc}")
    return Check("bootstrap_preflight", payload.get("ok") is True, "ok" if payload.get("ok") is True else "failed")


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

        payload = diagnostics(root, dry_run=True, include_doctor=False)
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


def secret_hits(root: Path) -> Iterable[str]:
    for path in secret_scan_files(root):
        rel_parts = set(path.relative_to(root).parts)
        if path.is_dir() or rel_parts & SECRET_SCAN_IGNORED_PARTS:
            continue
        if path.name in SECRET_SCAN_IGNORED_NAMES:
            continue
        if any(path.name.endswith(suffix) for suffix in SECRET_SCAN_IGNORED_SUFFIXES):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".sqlite", ".db", ".rdb", ".zip", ".gz", ".tar"}:
            continue
        if path.stat().st_size > 1_000_000:
            continue
        if path.name.endswith(".enc.yaml") or path.name.endswith(".enc.yml"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # PermissionError happens in mixed-ownership repos (e.g. Phalanx
            # has root-owned .pipeline_output/*.json that cc can't read). The
            # secret scan can't inspect what it can't read; skip rather than
            # aborting doctor with a fatal error.
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                yield path.relative_to(root).as_posix()
                break


def secret_scan_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(path for path in root.rglob("*") if path.is_file())
    rels = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return sorted(path for rel in rels if (path := root / rel).is_file())


def as_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks],
    }
