"""Tests for ai_core.agents_md — the managed AGENTS.md memory block used to give
Google Antigravity (agy) cross-agent memory parity (it auto-loads AGENTS.md but
cannot receive Code Brain's SessionStart hook injection).
"""
from __future__ import annotations

from pathlib import Path

import ai_core.agents_md as A


def test_compose_inserts_then_replaces() -> None:
    base = "# AGENTS.md\n\nCanonical instructions live in `.ai/AGENTS.md`.\n"
    out1 = A.compose(base, "BLOCK-ONE")
    assert A.START in out1 and A.END in out1 and "BLOCK-ONE" in out1
    # never clobbers existing content outside the markers
    assert "Canonical instructions live in `.ai/AGENTS.md`." in out1
    # re-composing swaps the body and keeps exactly one managed section
    out2 = A.compose(out1, "BLOCK-TWO")
    assert out2.count(A.START) == 1 and out2.count(A.END) == 1
    assert "BLOCK-TWO" in out2 and "BLOCK-ONE" not in out2
    assert "Canonical instructions live in `.ai/AGENTS.md`." in out2


def test_refresh_writes_then_change_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root: "MEMORY-SNAP-1")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is True
    assert "MEMORY-SNAP-1" in agents.read_text(encoding="utf-8")
    # identical memory -> no rewrite (does not churn the file every turn)
    assert A.refresh(tmp_path) is False
    # changed memory -> rewrites, still a single managed section
    monkeypatch.setattr(A, "render_block", lambda root: "MEMORY-SNAP-2")
    assert A.refresh(tmp_path) is True
    txt = agents.read_text(encoding="utf-8")
    assert txt.count(A.START) == 1 and "MEMORY-SNAP-2" in txt and "MEMORY-SNAP-1" not in txt


def test_refresh_disabled_via_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root: "X")
    monkeypatch.setenv("AI_AGENTS_MD_MEMORY", "0")
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is False


def test_refresh_empty_block_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root: "")
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is False
