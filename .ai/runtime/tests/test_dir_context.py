"""G9: Read-triggered walk-up directory context injection — opt-in, sealed, per-session dedup."""
from __future__ import annotations

from pathlib import Path

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


def test_block_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.delenv("AI_DIR_CONTEXT", raising=False)
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
    monkeypatch.delenv("AI_DIR_CONTEXT", raising=False)
    off = hooks.handle_hook(root, "PostToolUse", dict(payload))
    assert not off.get("dir_context")
    monkeypatch.setenv("AI_DIR_CONTEXT", "1")
    on = hooks.handle_hook(root, "PostToolUse", dict(payload))
    assert on.get("dir_context") is True
    assert "auth guidance" in on["hookSpecificOutput"]["additionalContext"]
