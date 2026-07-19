"""G9: Read-triggered walk-up directory context injection — opt-in, sealed, per-session dedup."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ai_core import dir_context as dc


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    sub = tmp_path / "pkg" / "auth"
    sub.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "AGENTS.md").write_text("pkg guidance", encoding="utf-8")
    (sub / "AGENTS.md").write_text("auth guidance", encoding="utf-8")
    (sub / "login.py").write_text("# code", encoding="utf-8")
    return tmp_path


def test_find_walks_up_and_skips_root(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    files = dc.find_context_files(root, str(root / "pkg" / "auth" / "login.py"))
    rels = [f.parent.name for f in files]
    assert rels == ["auth", "pkg"]            # nearest-first, root AGENTS.md skipped


def test_sealed_to_root(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    # a file at root level → only root dir in chain, which is skipped → nothing
    assert dc.find_context_files(root, str(root / "AGENTS.md")) == []


def test_block_disabled_when_off(tmp_path: Path, monkeypatch) -> None:
    # default is now ON; explicit AI_DIR_CONTEXT=0 disables.
    root = _seed(tmp_path)
    monkeypatch.setenv("AI_DIR_CONTEXT", "0")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
               "session_id": "s1"}
    assert dc.directory_context_for_read(root, payload) == ""


def test_block_surfaces_then_dedups(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
               "session_id": "s1"}
    first = dc.directory_context_for_read(root, payload)
    assert "auth guidance" in first and "pkg guidance" in first and "Directory Context:" in first
    second = dc.directory_context_for_read(root, payload)
    assert second == ""                        # already surfaced this session


def test_non_read_tool_ignored(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"}
    assert dc.directory_context_for_read(root, payload) == ""


def test_label_redaction(tmp_path: Path, monkeypatch) -> None:
    # home-path secret: redacted by redact_value but not a secret_scan SECRET_PATTERN hit, so this
    # test file does not trip the repo secret scan / allowlist invariant.
    root = _seed(tmp_path)
    sub = root / "pkg" / "auth"
    (sub / "AGENTS.md").write_text("see /Users/alice/private/notes here", encoding="utf-8")
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(sub / "login.py")}, "session_id": "s2"}
    out = dc.directory_context_for_read(root, payload)
    assert "/Users/alice" not in out


def test_hook_postooluse_read_injects_when_enabled(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    from ai_core import hooks
    payload = {"agent": "claude", "session_id": "s3", "dry": True, "tool_name": "Read",
               "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")}}
    monkeypatch.setenv("AI_DIR_CONTEXT", "0")
    off = hooks.handle_hook(root, "PostToolUse", dict(payload))
    assert not off.get("dir_context")
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    on = hooks.handle_hook(root, "PostToolUse", dict(payload))
    assert on.get("dir_context") is True
    assert "auth guidance" in on["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_external_context_symlink_is_never_injected(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external-agents.md"
    external.write_text("EXTERNAL_CONTEXT_INJECTION", encoding="utf-8")
    context = root / "pkg" / "auth" / "AGENTS.md"
    context.unlink()
    context.symlink_to(external)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
        "session_id": "external-context",
    }

    result = dc.directory_context_for_read(root, payload)

    assert "EXTERNAL_CONTEXT_INJECTION" not in result
    assert "pkg guidance" in result


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_internal_context_symlink_remains_supported(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    auth = root / "pkg" / "auth"
    agents = auth / "AGENTS.md"
    claude = auth / "CLAUDE.md"
    agents.rename(claude)
    agents.symlink_to(claude.name)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(auth / "login.py")},
        "session_id": "internal-context",
    }

    result = dc.directory_context_for_read(root, payload)

    assert "auth guidance" in result


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_external_context_hardlink_is_never_injected(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    external = tmp_path / "external-agents.md"
    external.write_text("EXTERNAL_HARDLINK_CONTEXT", encoding="utf-8")
    context = root / "pkg" / "auth" / "AGENTS.md"
    context.unlink()
    os.link(external, context)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
        "session_id": "hardlinked-context",
    }

    result = dc.directory_context_for_read(root, payload)

    assert "EXTERNAL_HARDLINK_CONTEXT" not in result


@pytest.mark.skipif(os.name == "nt", reason="Unix cache trust semantics")
def test_seen_cache_symlink_and_public_mode_are_ignored_and_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _seed(tmp_path)
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "pkg" / "auth" / "login.py")},
        "session_id": "seen-cache",
    }
    cache = dc._seen_path(root, "seen-cache")
    cache.parent.mkdir(parents=True)
    external = tmp_path / "external-seen.json"
    external.write_text(
        json.dumps([str(root / "pkg" / "auth" / "AGENTS.md")]),
        encoding="utf-8",
    )
    cache.symlink_to(external)

    first = dc.directory_context_for_read(root, payload)
    assert "auth guidance" in first
    assert not cache.is_symlink()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600

    cache.write_text("[]", encoding="utf-8")
    cache.chmod(0o644)
    second = dc.directory_context_for_read(root, payload)
    assert "auth guidance" in second
