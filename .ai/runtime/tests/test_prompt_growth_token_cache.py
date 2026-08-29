"""Regression: the Stop hook must never aggregate agent transcripts inline.

`prompt_growth._output_tokens` used to call `obs.usage_report`, which re-parses every
codex/claude session file on the host. Measured inline: 8.06s on blurivo (623,836 JSON
lines across 507 codex + 125 claude sessions), 6.5s on code-brain. `tick` reaches it on
its growth cooldown, so one turn in five stalled turn-end for 6-8s with no user-visible
error — the hook simply hung.

These tests pin the contract that made that impossible to reintroduce:
  * the inline read is cache-only and never touches `obs.usage_report`;
  * a cold/stale/corrupt cache degrades to 0 rather than scanning;
  * the value is provenance, never a ratchet decision input;
  * the detached refresher is what populates it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_core import prompt_growth as pg


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return tmp_path


def test_output_tokens_never_calls_usage_report(tmp_path: Path, monkeypatch) -> None:
    """The inline path must not reach the multi-second transcript aggregation."""
    root = _seed(tmp_path)
    from ai_core import obs

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("usage_report called from the inline hook path")

    monkeypatch.setattr(obs, "usage_report", _boom)
    assert pg._output_tokens(root) == 0


def test_tick_growth_does_not_call_usage_report(tmp_path: Path, monkeypatch) -> None:
    """Full growth step (the 1-in-5 turn that used to stall) stays scan-free."""
    root = _seed(tmp_path)
    from ai_core import obs

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("usage_report called during prompt_growth.tick")

    monkeypatch.setattr(obs, "usage_report", _boom)
    last = {}
    for _ in range(20):
        last = pg.tick(root, output_chars=1200, cooldown=5)
    # growth still happened; only the expensive provenance read was avoided
    assert last["grew"] is True
    assert "apply:brevity-boost" in last["actions"]


def test_cache_hit_is_used(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    pg._write_tokens_cache(root, 4242)
    assert pg._output_tokens(root) == 4242


def test_stale_cache_ignored(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    pg._write_tokens_cache(root, 999, now=time.time() - (pg._TOKENS_CACHE_TTL_SECONDS + 60))
    assert pg._read_tokens_cache(root) is None
    assert pg._output_tokens(root) == 0


def test_fresh_cache_boundary(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    pg._write_tokens_cache(root, 7, now=time.time() - (pg._TOKENS_CACHE_TTL_SECONDS - 30))
    assert pg._read_tokens_cache(root) == 7


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", "null", '{"ts": "x", "output_tokens": 1}', '{"output_tokens": 5}'],
)
def test_corrupt_cache_degrades_to_zero(tmp_path: Path, raw: str) -> None:
    root = _seed(tmp_path)
    path = pg._tokens_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    assert pg._read_tokens_cache(root) is None
    assert pg._output_tokens(root) == 0


def test_missing_cache_is_soft(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert not pg._tokens_cache_path(root).exists()
    assert pg._output_tokens(root) == 0


def test_minimal_root_without_config_stays_zero(tmp_path: Path) -> None:
    """No .ai/config.yaml (test/minimal root): short-circuit before any IO."""
    root = tmp_path
    (root / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    pg._write_tokens_cache(root, 500)
    assert pg._output_tokens(root) == 0


def test_refresh_writes_cache(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setattr(pg, "compute_output_tokens", lambda _root: 31337)
    assert pg.refresh_output_tokens_cache(root) == 31337
    payload = json.loads(pg._tokens_cache_path(root).read_text(encoding="utf-8"))
    assert payload["output_tokens"] == 31337
    assert payload["ts"] > 0
    assert pg._output_tokens(root) == 31337


def test_compute_still_aggregates_when_called_explicitly(tmp_path: Path, monkeypatch) -> None:
    """The slow read is preserved for out-of-band callers, just not inline."""
    root = _seed(tmp_path)
    from ai_core import obs

    monkeypatch.setattr(
        obs,
        "usage_report",
        lambda *_a, **_k: {
            "actual_token_usage": {
                "claude": {"tokens": {"output_tokens": 10}},
                "codex": {"tokens": {"output_tokens": 32}},
            }
        },
    )
    assert pg.compute_output_tokens(root) == 42


def test_baseline_tokens_is_provenance_not_a_decision_input(tmp_path: Path) -> None:
    """A stale/zero token total must not change whether a rule is kept or rolled back.

    The ratchet judges `baseline_obs_avg` / `_recent_output_avg` (local jsonl), so two runs
    that differ only in the cached token value must reach the same verdict.
    """
    verdicts = []
    for cached in (0, 10_000_000):
        root = _seed(tmp_path / f"r{cached}")
        pg._write_tokens_cache(root, cached)
        actions: list[str] = []
        for _ in range(20):  # apply
            pg.tick(root, output_chars=1200, cooldown=5)
        for _ in range(pg.RATCHET_WINDOW + 5):  # then behave well -> should graduate
            res = pg.tick(root, output_chars=40, cooldown=5)
            actions.extend(res.get("actions") or [])
        verdicts.append([a for a in actions if a.startswith(("keep:", "rollback:"))])
    assert verdicts[0] == verdicts[1]
    assert verdicts[0], "expected the ratchet to reach a verdict"


def test_refresh_is_wired_detached_from_the_hook(tmp_path: Path) -> None:
    """hooks must expose the detached refresher and reference it from the Stop path."""
    from ai_core import hooks

    assert hasattr(hooks, "_spawn_tokens_cache_refresh")
    src = Path(hooks.__file__).read_text(encoding="utf-8")
    assert "_spawn_tokens_cache_refresh(root)" in src
    # and the inline module must not reach usage_report from _output_tokens anymore
    pg_src = Path(pg.__file__).read_text(encoding="utf-8")
    inline = pg_src.split("def _output_tokens(")[1]
    assert "usage_report" not in inline


def test_detached_refresh_is_gated_by_prompt_growth_opt_in() -> None:
    from ai_core import hooks

    src = Path(hooks.__file__).read_text(encoding="utf-8")
    gate = 'if not _env_disabled("AI_PROMPT_GROWTH", default="0"):'
    gated = src.split(gate, 1)[1].split("if effective_hook in AUTO_REBUILD_HOOKS", 1)[0]
    assert "_spawn_tokens_cache_refresh(root)" in gated


def test_sleep_time_hook_jobs_never_spawn_network_commands() -> None:
    import inspect

    from ai_core import hooks

    src = inspect.getsource(hooks._spawn_sleep_time_jobs)
    assert "AI_REMOTE_FETCH" not in src
    assert '"fetch"' not in src
    assert '"push"' not in src
