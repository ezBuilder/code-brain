"""Turn-change snapshot + bounded summary nudge (deterministic, git-facts only)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_core import turn_report  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def test_parse_shortstat_all_shapes() -> None:
    assert turn_report._parse_shortstat(" 3 files changed, 12 insertions(+), 4 deletions(-)") == (3, 12, 4)
    assert turn_report._parse_shortstat(" 1 file changed, 2 insertions(+)") == (1, 2, 0)
    assert turn_report._parse_shortstat(" 1 file changed, 5 deletions(-)") == (1, 0, 5)
    assert turn_report._parse_shortstat("") == (0, 0, 0)


def test_measure_off_repo_is_soft(tmp_path: Path) -> None:
    out = turn_report.measure(tmp_path)
    assert out["git"] is False
    assert out["files"] == 0


def test_measure_counts_unstaged_staged_and_untracked(repo: Path) -> None:
    (repo / "seed.txt").write_text("seed\nchanged\n", encoding="utf-8")
    (repo / "staged.txt").write_text("a\nb\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "brand_new.txt").write_text("x\n", encoding="utf-8")

    out = turn_report.measure(repo)
    assert out["git"] is True
    assert out["files"] == 2, out           # one unstaged + one staged
    assert out["insertions"] >= 3, out
    assert out["untracked"] == 1, out
    assert out["head"]


def test_first_snapshot_is_baseline_and_never_nudges(repo: Path) -> None:
    """A repo can already be dirty before Code Brain ever ran; the first measurement
    must not be attributed to the current turn."""
    for i in range(30):
        (repo / f"pre{i}.txt").write_text("pre\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="codex", now=500.0)
    assert snap["baseline"] is True
    assert snap["files"] == 0 and snap["untracked"] == 0, snap
    assert snap["measured"]["untracked"] == 30, snap
    assert turn_report.is_large(snap) is False
    assert turn_report.nudge_line(repo, now=501.0) == ""


def test_write_snapshot_then_no_nudge_for_small_turn(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="codex", now=999.0)  # baseline
    (repo / "seed.txt").write_text("seed\ntiny\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="codex", now=1000.0)
    assert snap and snap["reported"] is False
    assert snap["baseline"] is False
    assert turn_report.is_large(snap) is False

    # Small change → no nudge, and the snapshot is consumed so it cannot fire later.
    assert turn_report.nudge_line(repo, now=1001.0) == ""
    assert json.loads(turn_report.state_path(repo).read_text(encoding="utf-8"))["reported"] is True


def test_large_turn_nudges_once_only(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="claude", now=1999.0)  # baseline
    for i in range(12):
        (repo / f"f{i}.txt").write_text(f"content {i}\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="claude", now=2000.0)
    assert turn_report.is_large(snap) is True

    line = turn_report.nudge_line(repo, now=2001.0)
    assert line.startswith("cb-turn:"), line
    assert "summarize" in line
    # The UserPromptSubmit budget is 2048B and build_context truncates from the tail, so
    # this line must stay small or it evicts the memory sections that follow it.
    assert len(line.encode("utf-8")) <= 120, len(line.encode("utf-8"))
    # Single-shot: the immediate follow-up turn is not nagged again.
    assert turn_report.nudge_line(repo, now=2002.0) == ""


def test_line_churn_alone_triggers_on_few_files(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="", now=2999.0)  # baseline
    (repo / "seed.txt").write_text("\n".join(f"line {i}" for i in range(400)) + "\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="", now=3000.0)
    assert snap["files"] == 1
    assert turn_report.is_large(snap) is True, snap


def test_stale_snapshot_is_ignored(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="", now=4999.0)  # baseline
    for i in range(12):
        (repo / f"g{i}.txt").write_text("x\n", encoding="utf-8")
    turn_report.write_snapshot(repo, agent="", now=5000.0)
    later = 5000.0 + turn_report.SNAPSHOT_MAX_AGE_SECONDS + 1
    assert turn_report.nudge_line(repo, now=later) == ""


def test_head_moved_detected_across_commit(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="", now=6000.0)
    (repo / "seed.txt").write_text("seed\nmore\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "advance")
    snap = turn_report.write_snapshot(repo, agent="", now=6001.0)
    assert snap["head_moved"] is True, snap


def test_disable_switch_kills_both_sides(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(12):
        (repo / f"h{i}.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AI_TURN_REPORT", "0")
    assert turn_report.write_snapshot(repo, agent="", now=7000.0) == {}
    assert turn_report.nudge_line(repo, now=7001.0) == ""
    assert not turn_report.state_path(repo).exists()


def test_thresholds_are_env_tunable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    turn_report.write_snapshot(repo, agent="", now=7999.0)  # baseline
    (repo / "one.txt").write_text("x\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="", now=8000.0)
    assert turn_report.is_large(snap) is False
    monkeypatch.setenv("AI_TURN_REPORT_MIN_FILES", "1")
    assert turn_report.is_large(snap) is True


def test_corrupt_state_is_soft(repo: Path) -> None:
    path = turn_report.state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert turn_report.nudge_line(repo, now=9000.0) == ""

def test_delta_is_absolute_so_a_revert_counts(repo: Path) -> None:
    """Reverting work is as large a change as doing it; a signed delta would hide it."""
    for i in range(12):
        (repo / f"r{i}.txt").write_text("x\n", encoding="utf-8")
    turn_report.write_snapshot(repo, agent="", now=10_000.0)   # baseline at 12 untracked
    for i in range(12):
        (repo / f"r{i}.txt").unlink()
    snap = turn_report.write_snapshot(repo, agent="", now=10_001.0)
    assert snap["untracked"] == 12, snap
    assert turn_report.is_large(snap) is True


def test_quiet_turn_on_a_dirty_repo_does_not_nudge(repo: Path) -> None:
    """The regression this delta logic exists for: a repo dirty at rest (measured 933
    files on blurivo) must not mark every single turn as large."""
    for i in range(50):
        (repo / f"d{i}.txt").write_text("pre-existing\n", encoding="utf-8")
    turn_report.write_snapshot(repo, agent="", now=11_000.0)   # baseline absorbs the mess
    snap = turn_report.write_snapshot(repo, agent="", now=11_001.0)  # nothing happened
    assert snap["files"] == 0 and snap["untracked"] == 0, snap
    assert turn_report.is_large(snap) is False
    assert turn_report.nudge_line(repo, now=11_002.0) == ""


def test_code_brain_own_writes_are_not_attributed_to_the_turn(repo: Path) -> None:
    """Regression: Code Brain writes .ai/ on nearly every hook (audit jsonl, caches, the
    snapshot file itself). Counting it made a no-op turn look like a change and produced
    a phantom delta on every turn."""
    turn_report.write_snapshot(repo, agent="", now=12_000.0)  # baseline
    ai_dir = repo / ".ai" / "memory"
    ai_dir.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        (ai_dir / f"audit{i}.jsonl").write_text('{"a":1}\n' * 50, encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="", now=12_001.0)
    assert snap["files"] == 0, snap
    assert snap["untracked"] == 0, snap
    assert snap["measured"]["untracked"] == 0, snap
    assert turn_report.nudge_line(repo, now=12_002.0) == ""


def test_real_user_change_still_counted_alongside_ai_writes(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="", now=13_000.0)
    (repo / ".ai").mkdir(exist_ok=True)
    (repo / ".ai" / "noise.json").write_text("{}\n", encoding="utf-8")
    for i in range(10):
        (repo / f"src{i}.py").write_text("print(1)\n", encoding="utf-8")
    snap = turn_report.write_snapshot(repo, agent="", now=13_001.0)
    assert snap["untracked"] == 10, snap
    assert turn_report.is_large(snap) is True


def test_stale_consumer_cannot_overwrite_a_newer_snapshot(repo: Path) -> None:
    old = turn_report.write_snapshot(repo, agent="claude", now=14_000.0)
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    newer = turn_report.write_snapshot(repo, agent="codex", now=14_001.0)
    turn_report._mark_reported(repo, old)
    current = json.loads(turn_report.state_path(repo).read_text(encoding="utf-8"))
    assert current["ts"] == newer["ts"]
    assert current["reported"] is False


def test_snapshot_state_is_private(repo: Path) -> None:
    turn_report.write_snapshot(repo, agent="", now=15_000.0)
    assert turn_report.state_path(repo).stat().st_mode & 0o077 == 0
