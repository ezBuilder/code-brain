from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ai_core import mcp_server, plan_state


def test_plan_parent_symlink_is_rejected_without_external_write(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix directory symlink semantics")
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    memory = root / ".ai" / "memory"
    memory.mkdir(parents=True)
    (memory / "plans").symlink_to(external, target_is_directory=True)

    result = plan_state.init_plan(root, plan_id="alpha", steps=["one"])

    assert result == {"ok": False, "reason": "unsafe_plan_path", "plan_id": "alpha"}
    assert list(external.iterdir()) == []


def test_symlinked_plan_is_not_read_and_force_repair_preserves_external(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix symlink semantics")
    root = tmp_path / "repo"
    external = tmp_path / "external-plan.md"
    external.write_text("# outside\n- [ ] secret\n", encoding="utf-8")
    plan_dir = root / ".ai" / "memory" / "plans" / "alpha"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "plan.md"
    plan.symlink_to(external)

    before = plan_state.read_plan(root, "alpha")
    repaired = plan_state.init_plan(root, plan_id="alpha", steps=["inside"], force=True)

    assert before == {"ok": False, "reason": "read_error", "plan_id": "alpha"}
    assert repaired["ok"] is True
    assert not plan.is_symlink()
    assert external.read_text(encoding="utf-8") == "# outside\n- [ ] secret\n"
    assert "inside" in plan.read_text(encoding="utf-8")


def test_hardlinked_plan_is_rejected_and_force_repair_detaches(tmp_path: Path) -> None:
    if not hasattr(os, "link"):
        pytest.skip("hard links unavailable")
    root = tmp_path / "repo"
    external = tmp_path / "external-plan.md"
    external.write_text("# outside\n- [ ] secret\n", encoding="utf-8")
    plan_dir = root / ".ai" / "memory" / "plans" / "alpha"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "plan.md"
    os.link(external, plan)

    assert plan_state.read_plan(root, "alpha")["reason"] == "read_error"
    repaired = plan_state.init_plan(root, plan_id="alpha", steps=["inside"], force=True)

    assert repaired["ok"] is True
    assert plan.stat().st_ino != external.stat().st_ino
    assert external.read_text(encoding="utf-8") == "# outside\n- [ ] secret\n"


def test_plan_write_is_private_and_oversized_plan_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    created = plan_state.init_plan(root, plan_id="alpha", steps=["one"])
    path = plan_state.plan_path(root, "alpha")

    assert created["ok"] is True
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("x" * (plan_state.PLAN_MAX_BYTES + 1), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)

    assert plan_state.read_plan(root, "alpha") == {
        "ok": False,
        "reason": "read_error",
        "plan_id": "alpha",
    }


def test_plan_listing_ignores_linked_and_invalid_entries(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert plan_state.init_plan(root, plan_id="alpha", steps=["one"])["ok"] is True
    plans = plan_state.plans_root(root)
    (plans / "bad.name").mkdir()
    if os.name != "nt":
        external = tmp_path / "external-dir"
        external.mkdir()
        (external / "plan.md").write_text("- [ ] outside\n", encoding="utf-8")
        (plans / "linked").symlink_to(external, target_is_directory=True)

    listed = plan_state.list_plans(root)

    assert listed["count"] == 1
    assert listed["plans"][0]["plan_id"] == "alpha"


def test_mcp_session_symlink_is_not_listed_or_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("Unix symlink semantics")
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    root = tmp_path / "repo"
    external = tmp_path / "outside-session.md"
    external.write_text("outside secret\n", encoding="utf-8")
    session = root / ".ai" / "memory" / "session-current.md"
    session.parent.mkdir(parents=True)
    session.symlink_to(external)

    listed = mcp_server.handle_request(root, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    read = mcp_server.handle_request(
        root,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "codebrain://session/current"},
        },
    )

    assert "codebrain://session/current" not in {item["uri"] for item in listed["result"]["resources"]}
    assert read["error"] == {"code": -32602, "message": "invalid resource uri"}
    assert external.read_text(encoding="utf-8") == "outside secret\n"


def test_mcp_prompt_listing_rejects_linked_directory(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix directory symlink semantics")
    root = tmp_path / "repo"
    external = tmp_path / "outside-commands"
    external.mkdir()
    (external / "cb-secret.md").write_text("outside secret\n", encoding="utf-8")
    claude = root / ".claude"
    claude.mkdir(parents=True)
    (claude / "commands").symlink_to(external, target_is_directory=True)

    assert mcp_server._list_prompts(root) == []
    with pytest.raises(KeyError):
        mcp_server._get_prompt(root, "cb-secret", {})


def test_mcp_resource_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MCP_RESOURCES", "1")
    root = tmp_path / "repo"
    session = root / ".ai" / "memory" / "session-current.md"
    session.parent.mkdir(parents=True)
    session.write_text("x" * (mcp_server.MCP_RESOURCE_MAX_BYTES + 1), encoding="utf-8")

    response = mcp_server.handle_request(
        root,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "codebrain://session/current"},
        },
    )

    assert response["error"] == {"code": -32602, "message": "invalid resource uri"}
