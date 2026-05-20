"""Tests for T45: subagent model/tools auto-labeling in agent_recommend."""
from __future__ import annotations

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
