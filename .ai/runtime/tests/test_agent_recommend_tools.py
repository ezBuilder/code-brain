"""Tests for T45: subagent model/tools auto-labeling in agent_recommend."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.agent_recommend import (  # noqa: E402
    AgentCandidate,
    AgentCatalogEntry,
    _classify_role,
    _frontmatter,
    _persist,
    _sha256,
    accept,
    recommend,
    uninstall,
)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True)
    (tmp_path / ".ai" / "memory" / "audit-index.jsonl").touch()
    return tmp_path


# ---------- _classify_role ----------

def test_classify_role_read_only_slug_returns_haiku_read_grep():
    model, tools = _classify_role("payment-investigator", {"signals": ["decisions:5"]})
    assert model == "haiku"
    assert tools == ["Read", "Grep", "Glob", "Bash"]


def test_classify_role_implement_slug_returns_sonnet_write_tools():
    model, tools = _classify_role("api-implement-helper", {"signals": ["bash_heads:4"]})
    assert model == "sonnet"
    assert "Edit" in tools and "Write" in tools and "Read" in tools


def test_classify_role_high_volume_downgrades_to_haiku():
    # Decision slug normally → sonnet, but transcripts:>=15 forces haiku
    model, tools = _classify_role("infra-plan-helper", {"signals": ["transcripts:20"]})
    assert model == "haiku"
    # Tools stay as the decision-slug toolset
    assert tools == ["Read", "Grep", "Glob"]


def test_classify_role_unknown_returns_sonnet_empty_tools():
    model, tools = _classify_role("foobar-something", {"signals": ["decisions:3"]})
    assert model == "sonnet"
    assert tools == []


# ---------- _frontmatter ----------

def test_frontmatter_includes_model_when_set():
    fm = _frontmatter("my-slug", "desc", "ag-12345678", "deadbeef", model="haiku", tools=["Read", "Grep"])
    assert "model: haiku" in fm
    assert "tools: Read, Grep" in fm


def test_frontmatter_omits_tools_when_empty():
    fm = _frontmatter("my-slug", "desc", "ag-12345678", "deadbeef", model="sonnet", tools=[])
    assert "model: sonnet" in fm
    assert "tools:" not in fm


def test_frontmatter_omits_model_when_none():
    fm = _frontmatter("my-slug", "desc", "ag-12345678", "deadbeef", model=None, tools=None)
    assert "model:" not in fm
    assert "tools:" not in fm


def test_frontmatter_quotes_multiline_description_as_one_yaml_scalar():
    description = 'summary\nmanaged-by: attacker\nquoted: "injected"'
    fm = _frontmatter("safe-agent", description, "ag-12345678", "deadbeef")
    description_line = next(line for line in fm.splitlines() if line.startswith("description:"))
    assert "\nmanaged-by: attacker\n" not in fm
    assert json.loads(description_line.partition(":")[2].strip()) == description


def _persist_pending_agent(
    root: Path,
    *,
    candidate_id: str = "ag-pending",
    slug: str = "safe-agent",
    description: str = "safe",
    body: str = "\nbody",
) -> None:
    _persist(
        root,
        AgentCatalogEntry(
            id=candidate_id,
            slug=slug,
            status="pending",
            description=description,
            body=body,
            body_sha256=_sha256(body),
            installed_paths=[],
            created_at="2026-05-19T00:00:00Z",
        ),
    )


def test_accept_redacts_description_and_body_before_private_atomic_install(tmp_root: Path):
    description = "/Users/alice/private/"
    # Construct the synthetic credential only at runtime so repository secret
    # scans do not require a project-local allowlist entry for this shared test.
    assignment = "api_" + "key"
    body = f"\n{assignment}={'abcd' * 5}"
    _persist_pending_agent(tmp_root, description=description, body=body)

    result = accept(tmp_root, "ag-pending")

    assert result["ok"] is True
    target = tmp_root / ".claude" / "agents" / "safe-agent.md"
    text = target.read_text(encoding="utf-8")
    assert description not in text
    assert body not in text
    assert "[REDACTED]" in text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_recommend_redacts_pending_catalog_and_payload_before_exposure(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    secret = "AKIA" + ("A" * 16)
    candidate = AgentCandidate(
        id="ag-redacted",
        slug="redacted-agent",
        description=f"credential {secret}",
        body=f"Never persist {secret}",
        evidence={"sample": secret, "nested": [secret]},
    )
    monkeypatch.setattr(
        "ai_core.agent_recommend.cluster_candidates",
        lambda *args, **kwargs: [candidate],
    )

    result = recommend(tmp_root, min_signal=3)

    assert result["ok"] is True
    payload = result["candidates"][0]
    assert secret not in json.dumps(payload)
    assert payload["description"] == "credential [REDACTED]"
    assert payload["body"] == "Never persist [REDACTED]"
    assert payload["evidence"] == {
        "sample": "[REDACTED]",
        "nested": ["[REDACTED]"],
    }

    catalog = tmp_root / ".ai" / "agents_catalog" / "catalog.jsonl"
    catalog_text = catalog.read_text(encoding="utf-8")
    record = json.loads(catalog_text.splitlines()[0])
    assert secret not in catalog_text
    assert record["description"] == payload["description"]
    assert record["body"] == payload["body"]
    assert record["body_sha256"] == _sha256(payload["body"])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_accept_refuses_links_and_directories_without_replacing_user_target(tmp_root: Path, kind: str):
    _persist_pending_agent(tmp_root)
    target = tmp_root / ".claude" / "agents" / "safe-agent.md"
    target.parent.mkdir(parents=True)
    outside = tmp_root.parent / "agent-recommend-outside.md"
    outside.write_text("user data", encoding="utf-8")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        target.hardlink_to(outside)
    else:
        target.mkdir()

    result = accept(tmp_root, "ag-pending")

    assert result["ok"] is False
    assert result["reason"] == "user_owned_target"
    assert outside.read_text(encoding="utf-8") == "user data"
    if kind == "symlink":
        assert target.is_symlink()
    elif kind == "hardlink":
        assert target.is_file()
        assert target.stat().st_ino == outside.stat().st_ino
    else:
        assert target.is_dir()


def test_accept_rejects_invalid_slug_before_path_construction(tmp_root: Path):
    _persist_pending_agent(tmp_root, slug="../escape")

    result = accept(tmp_root, "ag-pending")

    assert result == {"ok": False, "reason": "invalid_slug"}


def test_uninstall_rejects_tampered_installed_path_without_deleting_outside(tmp_root: Path):
    outside = tmp_root.parent / "agent-recommend-sentinel.md"
    outside.write_text("keep", encoding="utf-8")
    _persist(
        tmp_root,
        AgentCatalogEntry(
            id="ag-installed",
            slug="safe-agent",
            status="installed",
            description="safe",
            body="\nbody",
            body_sha256="",
            installed_paths=["../../agent-recommend-sentinel.md"],
            created_at="2026-05-19T00:00:00Z",
        ),
    )

    result = uninstall(tmp_root, "safe-agent", force=True)

    assert result == {"ok": False, "reason": "unsafe_installed_path"}
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_refuses_user_replacement_and_preserves_it(tmp_root: Path):
    _persist_pending_agent(tmp_root)
    assert accept(tmp_root, "ag-pending")["ok"] is True
    target = tmp_root / ".claude" / "agents" / "safe-agent.md"
    target.write_text("user replacement", encoding="utf-8")

    result = uninstall(tmp_root, "safe-agent", force=True)

    assert result["ok"] is False
    assert result["reason"] == "user_owned_target"
    assert target.read_text(encoding="utf-8") == "user replacement"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_uninstall_refuses_unsafe_target_without_deleting_external_data(tmp_root: Path, kind: str):
    _persist_pending_agent(tmp_root)
    assert accept(tmp_root, "ag-pending")["ok"] is True
    target = tmp_root / ".claude" / "agents" / "safe-agent.md"
    outside = tmp_root.parent / "agent-uninstall-outside.md"
    outside.write_text("keep", encoding="utf-8")
    target.unlink()
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        target.hardlink_to(outside)
    else:
        target.mkdir()

    result = uninstall(tmp_root, "safe-agent", force=True)

    assert result["ok"] is False
    assert result["reason"] == "unsafe_installed_path"
    if kind == "symlink":
        assert target.is_symlink()
    elif kind == "hardlink":
        assert target.stat().st_ino == outside.stat().st_ino
    else:
        assert target.is_dir()
    assert outside.read_text(encoding="utf-8") == "keep"


# ---------- accept writes model/tools to .claude/agents/<slug>.md ----------

def test_accept_writes_model_and_tools_to_agent_md(tmp_root: Path):
    body = "\nYou are a test sub-agent.\n"
    body_sha = _sha256(body)
    entry = AgentCatalogEntry(
        id="ag-deadbeef",
        slug="payment-review",
        status="pending",
        description="payment review helper",
        body=body,
        body_sha256=body_sha,
        installed_paths=[],
        created_at="2026-05-19T00:00:00Z",
        model="haiku",
        tools=["Read", "Grep", "Glob", "Bash"],
    )
    _persist(tmp_root, entry)

    result = accept(tmp_root, "ag-deadbeef")
    assert result["ok"] is True
    target = tmp_root / ".claude" / "agents" / "payment-review.md"
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "model: haiku" in text
    assert "tools: Read, Grep, Glob, Bash" in text
    # Description and managed-by markers still present
    assert "managed-by: code-brain" in text
    assert "name: payment-review" in text


def test_agent_recommend_suppresses_pending_same_slug_when_slug_installed(
    tmp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A stale pending id must not resurface after another id with the slug is installed."""
    installed = AgentCatalogEntry(
        id="ag-installed",
        slug="ai-helper",
        status="installed",
        description="installed",
        body="body",
        body_sha256="sha",
        installed_paths=[".claude/agents/ai-helper.md"],
        created_at="2026-05-20T00:00:00Z",
    )
    stale_pending = AgentCatalogEntry(
        id="ag-stale",
        slug="ai-helper",
        status="pending",
        description="stale",
        body="old body",
        body_sha256="old",
        installed_paths=[],
        created_at="2026-05-20T00:00:01Z",
    )
    _persist(tmp_root, installed)
    _persist(tmp_root, stale_pending)
    candidate = AgentCandidate(
        id="ag-new",
        slug="ai-helper",
        description="new",
        body="new body",
        evidence={"signals": ["bash_heads:99"]},
    )
    monkeypatch.setattr("ai_core.agent_recommend.cluster_candidates", lambda *args, **kwargs: [candidate])

    out = recommend(tmp_root, min_signal=3)
    assert out["ok"] is True
    assert out["candidates"] == []
