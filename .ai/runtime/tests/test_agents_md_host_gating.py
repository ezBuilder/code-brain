"""_spawn_agents_md_refresh must not fire for hosts that never read the root AGENTS.md
file at all (claude — it auto-loads only CLAUDE.md, so refreshing the mirror would be
pure wasted work with zero reader). It DOES fire for codex (which auto-loads AGENTS.md
and benefits from a current file so its own SessionStart hook can skip re-injecting the
dynamic body via ``ai_core.agents_md.is_current``) and for hosts with no hook-injection
path at all (antigravity, unknown/future hosts), since those have no other route to the
dynamic memory body.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import hooks  # noqa: E402


def _spawn_would_fire(tmp_path: Path, monkeypatch, agent: str) -> bool:
    fired = {"popen": False}

    class _FakeProc:
        pid = 4242

    def fake_popen(*_args, **_kwargs):
        fired["popen"] = True
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.delenv("AI_AGENTS_MD_MEMORY", raising=False)
    hooks._spawn_agents_md_refresh(tmp_path, agent=agent)
    return fired["popen"]


def test_claude_does_not_spawn_agents_md_refresh(tmp_path: Path, monkeypatch) -> None:
    """Claude Code never auto-loads AGENTS.md (only CLAUDE.md) — no reader, no refresh."""
    assert _spawn_would_fire(tmp_path, monkeypatch, "claude") is False


def test_codex_still_spawns_agents_md_refresh(tmp_path: Path, monkeypatch) -> None:
    """Codex DOES auto-load root AGENTS.md, and the mirrored block is dynamic-only +
    fingerprint-checked, so refreshing it is not duplication: it is what keeps the file
    current for Codex's own next SessionStart (or Antigravity's, or another Codex turn) to
    see ``is_current() == True`` and skip re-injecting the dynamic body via the hook."""
    assert _spawn_would_fire(tmp_path, monkeypatch, "codex") is True


def test_antigravity_still_spawns_agents_md_refresh(tmp_path: Path, monkeypatch) -> None:
    assert _spawn_would_fire(tmp_path, monkeypatch, "antigravity") is True


def test_unknown_host_still_spawns_agents_md_refresh(tmp_path: Path, monkeypatch) -> None:
    """A future/unwired host (e.g. Kiro before it gets hook wiring) has no
    additionalContext path either, so it must keep the AGENTS.md fallback."""
    assert _spawn_would_fire(tmp_path, monkeypatch, "unknown") is True
