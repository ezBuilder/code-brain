"""Tests for ai_core.agents_md — the managed AGENTS.md memory block used to give
Google Antigravity (agy) cross-agent memory parity (it auto-loads AGENTS.md but
cannot receive Code Brain's SessionStart hook injection).
"""
from __future__ import annotations

from pathlib import Path

import ai_core.agents_md as A


def test_compose_inserts_then_replaces() -> None:
    base = "# AGENTS.md\n\nCanonical instructions live in `.ai/AGENTS.md`.\n"
    out1 = A.compose(base, "BLOCK-ONE", fp="fp-1")
    assert A.START in out1 and A.END in out1 and "BLOCK-ONE" in out1
    # never clobbers existing content outside the markers
    assert "Canonical instructions live in `.ai/AGENTS.md`." in out1
    # re-composing swaps the body and keeps exactly one managed section
    out2 = A.compose(out1, "BLOCK-TWO", fp="fp-2")
    assert out2.count(A.START) == 1 and out2.count(A.END) == 1
    assert "BLOCK-TWO" in out2 and "BLOCK-ONE" not in out2
    assert "Canonical instructions live in `.ai/AGENTS.md`." in out2


def test_refresh_writes_then_change_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "MEMORY-SNAP-1")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is True
    assert "MEMORY-SNAP-1" in agents.read_text(encoding="utf-8")
    # identical memory -> no rewrite (does not churn the file every turn)
    assert A.refresh(tmp_path) is False
    # changed memory -> rewrites, still a single managed section
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "MEMORY-SNAP-2")
    assert A.refresh(tmp_path) is True
    txt = agents.read_text(encoding="utf-8")
    assert txt.count(A.START) == 1 and "MEMORY-SNAP-2" in txt and "MEMORY-SNAP-1" not in txt


def test_refresh_disabled_via_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "X")
    monkeypatch.setenv("AI_AGENTS_MD_MEMORY", "0")
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is False


def test_refresh_empty_block_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "")
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    assert A.refresh(tmp_path) is False


def test_refresh_twice_with_same_block_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """Two refreshes with an unchanged memory block must produce byte-identical file
    content (idempotent), and content outside the managed markers must be untouched."""
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "STABLE-MEMORY-BLOCK")
    agents = tmp_path / "AGENTS.md"
    user_preamble = "# AGENTS.md\n\nUser-authored notes that must survive verbatim.\n"
    agents.write_text(user_preamble, encoding="utf-8")

    assert A.refresh(tmp_path) is True
    first_bytes = agents.read_bytes()
    assert user_preamble.strip() in first_bytes.decode("utf-8")

    assert A.refresh(tmp_path) is False  # unchanged -> no rewrite at all
    second_bytes = agents.read_bytes()
    assert first_bytes == second_bytes  # byte-idempotent

    # Force an actual second WRITE with the identical block (not just skip-on-unchanged)
    # by rewriting through compose() directly, to prove compose() itself is idempotent
    # even if refresh()'s early-return were ever removed.
    existing = agents.read_text(encoding="utf-8")
    recomposed = A.compose(existing, "STABLE-MEMORY-BLOCK", fp=A.stored_fingerprint(existing) or "")
    assert recomposed.encode("utf-8") == first_bytes
    assert user_preamble.strip() in recomposed


def test_refresh_refuses_agents_md_symlink_without_touching_target(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(A, "render_block", lambda root, **kw: "MEMORY")
    external = tmp_path.parent / f"{tmp_path.name}-external-agents"
    external.write_text("DO-NOT-TOUCH\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(external)

    assert A.refresh(tmp_path) is False
    assert external.read_text(encoding="utf-8") == "DO-NOT-TOUCH\n"
    assert A.is_current(tmp_path) is False
