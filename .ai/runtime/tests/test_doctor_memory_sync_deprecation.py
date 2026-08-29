"""check_config must diagnose a lingering memory_sync.enabled: true as a deprecated
no-op — informational only, never a failing check — since the hook auto-spawn that
used to read it was removed (network I/O is banned on the hooks/MCP hot path, even
when spawned detached)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.doctor import check_config  # noqa: E402


def _write_config(root: Path, body: str) -> None:
    (root / ".ai").mkdir(parents=True, exist_ok=True)
    (root / ".ai" / "config.yaml").write_text(body, encoding="utf-8")


_BASE = (
    "version: 1\n"
    "project_name: t\n"
    "features:\n"
    "  embeddings: false\n"
    "  remote_llm: false\n"
    "  external_notifications: false\n"
    "search:\n"
    "  retriever: bm25\n"
)


def test_memory_sync_disabled_is_plain_ok(tmp_path: Path) -> None:
    _write_config(tmp_path, _BASE + "memory_sync:\n  enabled: false\n")
    check = check_config(tmp_path)
    assert check.ok is True
    assert check.detail == "ok"


def test_memory_sync_enabled_true_is_flagged_deprecated_but_still_ok(tmp_path: Path) -> None:
    _write_config(tmp_path, _BASE + "memory_sync:\n  enabled: true\n")
    check = check_config(tmp_path)
    assert check.ok is True  # informational: must never fail doctor
    assert "deprecated" in check.detail
    assert "memory_sync.enabled" in check.detail


def test_no_memory_sync_block_is_plain_ok(tmp_path: Path) -> None:
    _write_config(tmp_path, _BASE)
    check = check_config(tmp_path)
    assert check.ok is True
    assert check.detail == "ok"
