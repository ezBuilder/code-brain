from __future__ import annotations

import json
import sqlite3
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
        check_manifest(root),
        check_trust(root),
        check_jsonl(root),
        check_audit_index(root),
        check_hot_path_slo(root),
        check_secret_scan(root),
        check_redaction_self_test(),
        check_diagnostics(root),
    ]
    return checks


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


def check_hot_path_slo(root: Path) -> Check:
    from .hooks import HOT_PATH_TARGET_MS, handle_hook

    samples = []
    for _ in range(10):
        payload = handle_hook(root, "DoctorSLOBaseline", {"agent": "doctor", "dry": True})
        samples.append(int(payload["elapsed_ms"]))
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else 0
    ok = p95 <= HOT_PATH_TARGET_MS
    return Check("hot_path_slo", ok, f"p95_ms={p95}, target_ms={HOT_PATH_TARGET_MS}")


def check_secret_scan(root: Path) -> Check:
    hits = list(secret_hits(root))
    return Check("secret_scan", not hits, "ok" if not hits else "hits: " + ", ".join(hits[:10]))


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


def check_diagnostics(root: Path) -> Check:
    try:
        from .obs import diagnostics

        payload = diagnostics(root, dry_run=True)
    except Exception as exc:
        return Check("diagnostics_dry_run", False, str(exc))
    return Check("diagnostics_dry_run", bool(payload.get("ok")), "ok" if payload.get("ok") else "failed")


def secret_hits(root: Path) -> Iterable[str]:
    ignored_parts = {".venv", "cache", ".git", ".claude"}
    for path in root.rglob("*"):
        rel_parts = set(path.relative_to(root).parts)
        if path.is_dir() or rel_parts & ignored_parts:
            continue
        if path.suffix in {".png", ".sqlite", ".db"}:
            continue
        if path.name.endswith(".enc.yaml") or path.name.endswith(".enc.yml"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                yield path.relative_to(root).as_posix()
                break


def as_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks],
    }
