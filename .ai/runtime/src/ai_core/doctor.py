from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import load_config
from .render import build_manifest
from .trust import parse_simple_toml

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9./+=-]{20,}['\"]?"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


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
        check_secret_scan(root),
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


def check_secret_scan(root: Path) -> Check:
    hits = list(secret_hits(root))
    return Check("secret_scan", not hits, "ok" if not hits else "hits: " + ", ".join(hits[:10]))


def check_diagnostics(root: Path) -> Check:
    try:
        from .obs import diagnostics

        payload = diagnostics(root, dry_run=True)
    except Exception as exc:
        return Check("diagnostics_dry_run", False, str(exc))
    return Check("diagnostics_dry_run", bool(payload.get("ok")), "ok" if payload.get("ok") else "failed")


def secret_hits(root: Path) -> Iterable[str]:
    ignored_parts = {".venv", "cache", ".git"}
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
