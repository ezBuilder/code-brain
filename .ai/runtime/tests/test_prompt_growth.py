"""prompt growth — autonomous, deterministic, non-blocking project-prompt self-improvement."""
from __future__ import annotations

import json
from pathlib import Path

from ai_core import hooks
from ai_core import prompt_growth as pg
from ai_core import self_improve as si


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
    assert "Self-initiated progress/output <=10 words" in pg.learned_prompt_text(root)
    assert "Answers to user questions concise by default" in pg.learned_prompt_text(root)


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
    assert "Answers to user questions concise by default" in pg.learned_prompt_text(root)


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
    assert "Answers to user questions concise by default" in pg.learned_prompt_text(root)


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


def test_prompt_growth_log_rotates_by_bytes(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setattr(pg, "PROMPT_GROWTH_MAX_BYTES", 700)
    monkeypatch.setattr(pg, "PROMPT_GROWTH_KEEP", 100)
    for _ in range(30):
        pg.record_turn(root, output_chars=123456789, agent="claude")
    path = pg.log_path(root)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 30
    assert path.stat().st_size <= 700
    assert json.loads(lines[-1])["output_chars"] == 123456789


def test_prompt_growth_prunes_version_snapshots(tmp_path: Path, monkeypatch) -> None:
    root = _seed(tmp_path)
    monkeypatch.setattr(pg, "PROMPT_GROWTH_VERSION_KEEP", 3)
    vdir = root.joinpath(*pg.VERSIONS_PARTS)
    vdir.mkdir(parents=True)
    for i in range(8):
        (vdir / f"2026010100000{i}.json").write_text("{}", encoding="utf-8")
    result = pg.prune_versions(root)
    assert len(result["pruned"]) == 5
    assert [path.name for path in sorted(vdir.glob("*.json"))] == [
        "20260101000005.json",
        "20260101000006.json",
        "20260101000007.json",
    ]


# --- eval_loop <-> ratchet fitness coupling (GEPA) ---


def _rule_by_text(root: Path, text: str) -> dict:
    return [r for r in pg._active_rules(pg._read_state(root)).values()
            if r.get("text") == text][0]


def test_eval_gate_noop_without_cases(tmp_path: Path) -> None:
    # With NO cases.jsonl, the eval gate must be a strict no-op: steady (non-regressing)
    # output_chars graduate the brevity rule to "kept" — degrade-to-token-only is provable.
    root = _seed(tmp_path)
    for _ in range(60):
        pg.tick(root, output_chars=1200, cooldown=5)
    rule = [r for r in pg._active_rules(pg._read_state(root)).values()
            if r.get("id") == "brevity-boost"][0]
    assert rule["status"] == "kept"
    # the baseline was captured but flagged unavailable, so the gate never fired
    assert rule["baseline_eval_available"] is False
    assert rule.get("regressed_eval") in (False, None)
    assert not (root / ".ai" / "eval" / "cases.jsonl").exists()


def test_eval_gate_rolls_back_on_pass_rate_drop(tmp_path: Path, monkeypatch) -> None:
    from ai_core import eval_loop

    root = _seed(tmp_path)
    pg.tick(root, output_chars=40, cooldown=999)  # turns=1, flat low output

    # Baseline at apply time: eval available and HIGH pass-rate.
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 3, "passed": 3, "pass_rate": 1.0})
    out = si.propose_rule(root, text="가능하면 짧게 답한다")
    assert out["status"] == "applied"
    rule = _rule_by_text(root, "가능하면 짧게 답한다")
    assert rule["baseline_eval_available"] is True
    assert rule["baseline_eval_pass_rate"] == 1.0

    # Judge time: eval still available but pass-rate DROPPED, while output_chars stay flat (the
    # token signal alone would keep the rule). The correctness gate must roll it back.
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 3, "passed": 1, "pass_rate": 0.3333})
    for _ in range(pg.RATCHET_WINDOW + 2):
        pg.tick(root, output_chars=40, cooldown=1)

    judged = _rule_by_text(root, "가능하면 짧게 답한다")
    assert judged["status"] == "regressed"
    assert judged["regressed_eval"] is True  # rolled back for correctness, not tokens


def test_eval_gate_action_tag_marks_eval(tmp_path: Path, monkeypatch) -> None:
    from ai_core import eval_loop

    root = _seed(tmp_path)
    pg.tick(root, output_chars=40, cooldown=999)
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 2, "passed": 2, "pass_rate": 1.0})
    si.propose_rule(root, text="짧게 답한다")
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 2, "passed": 0, "pass_rate": 0.0})
    # advance to the ratchet boundary without judging, then judge once and inspect the action tag
    for _ in range(pg.RATCHET_WINDOW):
        pg.tick(root, output_chars=40, cooldown=999)
    result = pg.evaluate_and_grow(root)
    assert any(a.startswith("rollback:") and a.endswith(":eval") for a in result["actions"])


def test_eval_gate_keeps_when_pass_rate_holds_or_improves(tmp_path: Path, monkeypatch) -> None:
    from ai_core import eval_loop

    root = _seed(tmp_path)
    pg.tick(root, output_chars=40, cooldown=999)
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 4, "passed": 2, "pass_rate": 0.5})
    si.propose_rule(root, text="요점만 답한다")
    # judge time: pass-rate IMPROVED; output flat → no regression on either signal → kept
    monkeypatch.setattr(eval_loop, "eval_fitness",
                        lambda r: {"available": True, "total": 4, "passed": 4, "pass_rate": 1.0})
    for _ in range(pg.RATCHET_WINDOW + 2):
        pg.tick(root, output_chars=40, cooldown=1)
    judged = _rule_by_text(root, "요점만 답한다")
    assert judged["status"] == "kept"
    assert judged["regressed_eval"] is False
