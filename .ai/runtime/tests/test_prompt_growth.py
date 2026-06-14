"""prompt growth — autonomous, deterministic, non-blocking project-prompt self-improvement."""
from __future__ import annotations

from pathlib import Path

from ai_core import hooks
from ai_core import prompt_growth as pg


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_grows_brevity_rule_after_sustained_verbosity(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    last = {}
    for _ in range(20):
        last = pg.tick(root, output_chars=1200, cooldown=5)
    assert last["grew"] is True
    assert "apply:brevity-boost" in last["actions"]
    assert pg.learned_path(root).exists()
    assert "<=50 Korean chars" in pg.learned_prompt_text(root)


def test_no_growth_when_concise(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(30):
        pg.tick(root, output_chars=40, cooldown=5)
    assert not pg.learned_path(root).exists()
    assert pg.learned_prompt_text(root) == ""


def test_cooldown_limits_growth_frequency(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    r1 = pg.tick(root, output_chars=1200, cooldown=5)
    assert r1["grew"] is False and r1["turns"] == 1  # not on a cooldown boundary


def test_apply_is_idempotent(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(40):
        pg.tick(root, output_chars=1200, cooldown=5)
    active = [r for r in pg.status(root)["rules"] if r["status"] in {"active", "kept"}]
    assert len(active) == 1  # rule applied once, never duplicated


def test_kept_rule_stays_injected(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    for _ in range(55):
        pg.tick(root, output_chars=1200, cooldown=5)
    rule = [r for r in pg.status(root)["rules"] if r["id"] == "brevity-boost"][0]
    assert rule["status"] == "kept"
    assert "<=50 Korean chars" in pg.learned_prompt_text(root)


def test_old_kept_brevity_rule_upgrades_text(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    state = {
        "turns": 50,
        "rules": {
            "brevity-boost": {
                "id": "brevity-boost",
                "text": "보고는 핵심 1줄(≤50자)로 강제한다.",
                "status": "kept",
                "applied_at": "2026-01-01T00:00:00Z",
            }
        },
    }
    pg._write_state(root, state)
    result = pg.evaluate_and_grow(root)
    assert "update:brevity-boost" in result["actions"]
    assert "<=50 Korean chars" in pg.learned_prompt_text(root)


def test_injection_reflects_learned_file(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    assert hooks._learned_prompt_context(root) == ""
    for _ in range(20):
        pg.tick(root, output_chars=1200, cooldown=5)
    assert "Learned project rules" in hooks._learned_prompt_context(root)


def test_record_turn_never_raises(tmp_path: Path) -> None:
    # even with a missing tree it must fail soft
    pg.record_turn(tmp_path / "nope", output_chars=10)
    assert pg.evaluate_and_grow(tmp_path / "nope")["ok"] in {True, False}
