from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_operations_documents_incremental_and_strict_trust_contract() -> None:
    text = (ROOT / "OPERATIONS.md").read_text(encoding="utf-8")

    required = (
        "mode=incremental baseline=cache total=... reused=... rescanned=... unreadable=... unstable=...",
        "baseline=filesystem",
        "doctor --strict` remains authoritative",
        "bypasses tracked-file and search-candidate caches",
        "reads the live Git baseline",
        "per-file size/mtime_ns/ctime_ns state",
        ".chatgpt2codex/",
        ".ai/cache/preflight-proof.json",
        "up to one hour",
        "fingerprint",
        "mode `0600`",
    )
    for marker in required:
        assert marker in text, marker