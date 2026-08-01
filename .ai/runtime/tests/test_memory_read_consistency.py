"""Read/write consistency for durable decisions and todos.

Four regressions guarded here:
  1. close_todo used to raise KeyError on a legacy id-less todo the user could see.
  2. expired/refuted decisions leaked into the HOT cache and into recommend evidence,
     because only two of the decision readers honored expires_at/retired status.
  3. expires_at accepted any string, so a typo like "2026" killed the record on arrival.
  4. four readers still bypassed memory.live_decision_records — agent_recommend's tag
     mining, the conflict scanner, the resume snapshot, and federated's cross-project tag
     mining — so a dead decision could still vote on drafted agents, be reported as a
     *live* conflict, ride resume.json into the next session, or carry a dead tag out of
     a sibling project and into THIS project's injected context.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import agent_recommend as ar  # noqa: E402
from ai_core import federated as fed  # noqa: E402
from ai_core import memory  # noqa: E402
from ai_core import memory_conflicts as mc  # noqa: E402
from ai_core import memory_tier as mt  # noqa: E402
from ai_core import recommend  # noqa: E402
from ai_core import session_resume as sr  # noqa: E402

_LEGACY_DECISION_KEYS = {"id", "decided_at", "decision", "tags", "source"}


def _seed(tmp_path: Path) -> Path:
    (tmp_path / ".ai" / "memory" / "audit").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed_install(home: Path, name: str) -> Path:
    """A Code Brain install discoverable by federated.discover_installations."""
    proj = home / "workspace" / name
    (proj / ".ai" / "generated").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "generated" / "install-manifest.json").write_text("{}", encoding="utf-8")
    return _seed(proj)


def _iso(delta_days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return dt.isoformat().replace("+00:00", "Z")


def _date_only(delta_days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _surface_texts(root: Path) -> list[str]:
    plain, _ = memory.read_decisions_for_surface(root, limit=50)
    return [p["decision"] for p in plain]


# --- (1) BUG-1: legacy id-less todo -----------------------------------------

def test_legacy_idless_todo_is_open_and_closes_without_raising(tmp_path: Path) -> None:
    """The exact crashing input: a row with a title but no id.

    It surfaces as an open todo, so closing it must work rather than raise KeyError.
    """
    root = _seed(tmp_path)
    path = memory.todos_path(root)
    path.write_text(json.dumps({"title": "legacy chore", "status": "open"}) + "\n", encoding="utf-8")
    path.chmod(0o600)  # readers reject group/other-writable state files regardless of umask

    assert [t.get("title") for t in memory.read_jsonl_open_todos(path, 10)] == ["legacy chore"]

    out = memory.close_todo(root, match="legacy chore", status="done")
    assert out["ok"] is True
    # the update must key on the SAME synthetic id the reader derives, or it never folds
    assert out["record"]["id"] == "legacy:legacy chore"
    assert memory.read_jsonl_open_todos(path, 10) == []  # really left the open list


def test_legacy_idless_todo_without_title_field_still_folds(tmp_path: Path) -> None:
    """A row carrying only `text` keys on legacy:<text> in both reader and writer."""
    root = _seed(tmp_path)
    path = memory.todos_path(root)
    path.write_text(json.dumps({"text": "old note task"}) + "\n", encoding="utf-8")
    path.chmod(0o600)

    out = memory.close_todo(root, match="old note", status="done")
    assert out["ok"] is True
    assert out["record"]["id"] == "legacy:old note task"
    assert memory.read_jsonl_open_todos(path, 10) == []


def test_todo_with_id_closes_unchanged(tmp_path: Path) -> None:
    """Rows that do have an id keep it verbatim — the fix must not rewrite them."""
    root = _seed(tmp_path)
    rec = memory.append_todo(root, title="normal chore", source="test")["record"]
    out = memory.close_todo(root, match="normal chore", status="done")
    assert out["ok"] is True
    assert out["record"]["id"] == rec["id"]
    assert memory.read_jsonl_open_todos(memory.todos_path(root), 10) == []


# --- (2)/(3) BUG-3: expires_at validation ------------------------------------

@pytest.mark.parametrize("bad", ["2026", "not-a-date", "  ", "2026-13-45", "2026-07", "later"])
def test_malformed_expires_at_omitted_and_record_still_surfaces(tmp_path: Path, bad: str) -> None:
    """A malformed bound is dropped (fail-soft), never stored — so it cannot kill the record."""
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="bounded decision", tags=["x"], source="op",
                                 expires_at=bad)["record"]
    assert "expires_at" not in rec
    assert set(rec.keys()) == _LEGACY_DECISION_KEYS  # no expires_at:null churn either
    assert "bounded decision" in _surface_texts(root)


def test_date_only_expires_at_widens_to_end_of_day(tmp_path: Path) -> None:
    """`expires_at: 2026-12-31` means "valid through that day", not "dead at 00:00Z"."""
    root = _seed(tmp_path)
    today = _date_only(0)
    rec = memory.append_decision(root, text="valid through today", expires_at=today)["record"]
    assert rec["expires_at"] == f"{today}T23:59:59.999999Z"
    assert "valid through today" in _surface_texts(root)


def test_date_only_expires_at_in_the_past_still_expires(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="yesterday bound", expires_at=_date_only(-1))
    assert "yesterday bound" not in _surface_texts(root)


def test_future_expires_at_round_trips_byte_identical(tmp_path: Path) -> None:
    """A well-formed UTC bound must be stored exactly as given (no normalization drift)."""
    root = _seed(tmp_path)
    bound = _iso(30)
    rec = memory.append_decision(root, text="future bound", expires_at=bound)["record"]
    assert rec["expires_at"] == bound
    stored = memory.read_jsonl_all(memory.decisions_path(root))[-1]
    assert stored["expires_at"] == bound
    assert "future bound" in _surface_texts(root)


def test_past_expires_at_excluded_from_surface(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="already dead", expires_at=_iso(-1))
    assert "already dead" not in _surface_texts(root)


def test_offset_expires_at_normalized_to_utc(tmp_path: Path) -> None:
    """Lexical comparison against now_iso() only works on UTC, so offsets are converted."""
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="offset bound",
                                 expires_at="2099-01-01T09:00:00+09:00")["record"]
    assert rec["expires_at"] == "2099-01-01T00:00:00Z"
    assert "offset bound" in _surface_texts(root)


@pytest.mark.parametrize("edge", ["9999-12-31T23:59:59-14:00", "0001-01-01T00:00:00+14:00"])
def test_datetime_domain_edge_expires_at_dropped_not_raised(tmp_path: Path, edge: str) -> None:
    """These parse fine but overflow datetime when shifted to UTC — still fail-soft, not raise.

    A max/min date carrying a far offset pushes astimezone(utc) past datetime.max /
    before datetime.min, which raises OverflowError rather than ValueError. The
    validator promises a malformed bound is dropped, so the write must survive.
    """
    assert memory._valid_expires_at(edge) is None
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="edge bound", tags=["x"], source="op",
                                 expires_at=edge)["record"]
    assert "expires_at" not in rec
    assert set(rec.keys()) == _LEGACY_DECISION_KEYS
    assert "edge bound" in _surface_texts(root)


# --- (4) BUG-2: dead decisions must not reach injected context ---------------

def test_expired_and_refuted_absent_from_scored_durable_items(tmp_path: Path) -> None:
    """scored_durable_items feeds the SessionStart HOT cache — dead rows must not be scored."""
    root = _seed(tmp_path)
    live = memory.append_decision(root, text="live decision", tags=["seed"])["record"]
    expired = memory.append_decision(root, text="time-boxed decision",
                                     expires_at=_iso(-1))["record"]
    failure = memory.append_decision(root, text="fp8 broke", kind="failure",
                                     observed_versions={"torch": "2.4.0"})["record"]
    memory.append_decision(root, text="fp8 works now", kind="failure", status="refuted",
                           supersedes_id=failure["id"])

    refs = [item["ref"] for item in mt.scored_durable_items(root)]
    assert live["id"] in refs
    assert expired["id"] not in refs
    assert failure["id"] not in refs  # folded by id, then dropped as refuted
    assert len(refs) == len(set(refs))  # folded once, not twice


def test_expired_and_refuted_absent_from_recommend_decision_tags(tmp_path: Path) -> None:
    """This path copies decision TEXT into a drafted command body."""
    root = _seed(tmp_path)
    for i in range(3):
        memory.append_decision(root, text=f"live infra choice {i}", tags=["infra"])
    for i in range(3):
        memory.append_decision(root, text=f"dead ledger choice {i}", tags=["ledger"],
                               expires_at=_iso(-1))
    failure = memory.append_decision(root, text="fp8 tag failure", kind="failure",
                                     tags=["fp8"])["record"]
    memory.append_decision(root, text="fp8 tag failure resolved", kind="failure",
                           tags=["fp8"], status="refuted", supersedes_id=failure["id"])

    signals = recommend.gather_signals(root, include_global=False)
    cands = recommend._candidates_from_decision_tags(signals, min_signal=1)
    slugs = " ".join(c.slug for c in cands)
    blob = json.dumps([c.evidence for c in cands], ensure_ascii=False)

    assert "infra" in slugs
    assert "live infra choice" in blob
    for dead in ("ledger", "fp8"):
        assert dead not in slugs
    assert "dead ledger choice" not in blob
    assert "fp8 tag failure" not in blob


# --- (5) backward compatibility ---------------------------------------------

def test_plain_decision_record_stays_byte_identical(tmp_path: Path) -> None:
    """A repo using none of the new behavior must write exactly the legacy record."""
    root = _seed(tmp_path)
    rec = memory.append_decision(root, text="plain decision", tags=["x"], source="op")["record"]
    assert set(rec.keys()) == _LEGACY_DECISION_KEYS
    line = memory.decisions_path(root).read_text(encoding="utf-8").strip()
    assert json.loads(line) == rec
    assert "expires_at" not in line
    assert line == json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --- (6) the three remaining readers now share the live filter ----------------

def test_expired_and_refuted_absent_from_agent_recommend_decision_tags(tmp_path: Path) -> None:
    """Direct twin of recommend's tag miner: these counts draft `.claude/agents/*.md`."""
    root = _seed(tmp_path)
    memory.append_decision(root, text="live infra choice", tags=["infra"])
    memory.append_decision(root, text="dead ledger choice", tags=["ledger"], expires_at=_iso(-1))
    failure = memory.append_decision(root, text="fp8 tag failure", kind="failure",
                                     tags=["fp8"])["record"]
    memory.append_decision(root, text="fp8 tag failure resolved", kind="failure", tags=["fp8"],
                           status="refuted", supersedes_id=failure["id"])

    counts = ar._gather_decision_tags(root)
    assert counts["infra"] == 1
    assert counts["ledger"] == 0  # expired → no longer votes
    assert counts["fp8"] == 0     # folded by id, then dropped as refuted


def test_expired_decision_not_reported_as_live_conflict(tmp_path: Path) -> None:
    """The conflict scanner ignored expires_at, so a lapsed rule stayed "live" forever."""
    root = _seed(tmp_path)
    memory.append_decision(root, text="use ruff for linting python code")
    memory.append_decision(root, text="never use ruff for linting python code",
                           expires_at=_iso(-1))

    out = mc.scan_conflicts(root, dry_run=True)
    assert out["ok"] is True
    assert out["scanned"] == 1  # only the surviving side was compared
    assert out["candidates"] == []
    assert not mc.conflicts_path(root).exists()


def test_refuted_failure_not_reported_as_live_conflict(tmp_path: Path) -> None:
    root = _seed(tmp_path)
    memory.append_decision(root, text="always cache embeddings on disk")
    failure = memory.append_decision(root, text="do not cache embeddings on disk",
                                     kind="failure")["record"]
    memory.append_decision(root, text="do not cache embeddings on disk", kind="failure",
                           status="refuted", supersedes_id=failure["id"])

    out = mc.scan_conflicts(root, dry_run=True)
    assert out["scanned"] == 1
    assert out["candidates"] == []


def test_conflict_scan_keeps_idless_rows_distinct_and_drops_textless(tmp_path: Path) -> None:
    """The removed `_anon{n}` fold keys existed only to keep id-LESS rows distinct.

    live_decision_records appends plain rows verbatim in file order, so two legacy id-less
    rows still compare against each other (scanned == 2, not 1); a row with no decision text
    is still filtered out before comparison.
    """
    root = _seed(tmp_path)
    path = memory.decisions_path(root)
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {"decision": "use ruff for linting python code"},
                {"decision": "never use ruff for linting python code"},
                {"decision": "   "},
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)  # readers reject group/other-writable state files regardless of umask

    out = mc.scan_conflicts(root, dry_run=True)
    assert out["scanned"] == 2   # both id-less rows survived as separate rows
    assert out["candidates"] == []  # ...but a pair with no ids can never be recorded


def test_conflict_tail_window_is_preserved(tmp_path: Path) -> None:
    """`scan` still bounds the comparison to the most recent N live rows."""
    root = _seed(tmp_path)
    for i in range(4):
        memory.append_decision(root, text=f"always deploy service {i} nightly")
    assert mc.scan_conflicts(root, dry_run=True, scan=2)["scanned"] == 2
    assert mc.scan_conflicts(root, dry_run=True, scan=0)["scanned"] == 1  # max(1, scan)


def test_expired_and_refuted_absent_from_resume_decisions_tail(tmp_path: Path) -> None:
    """resume.json is injected verbatim into the next session by hooks.py."""
    root = _seed(tmp_path)
    live = memory.append_decision(root, text="live decision", tags=["seed"])["record"]
    memory.append_decision(root, text="time-boxed decision", expires_at=_iso(-1))
    failure = memory.append_decision(root, text="fp8 broke", kind="failure")["record"]
    memory.append_decision(root, text="fp8 works now", kind="failure", status="refuted",
                           supersedes_id=failure["id"])

    tail = sr._decisions_tail(root)
    assert [r["id"] for r in tail] == [live["id"]]
    texts = [str(r.get("decision") or "") for r in tail]
    assert "time-boxed decision" not in texts
    assert "fp8 broke" not in texts and "fp8 works now" not in texts


def test_resume_decisions_tail_keeps_five_row_window(tmp_path: Path) -> None:
    """The live filter runs before the tail; the 5-row window itself is unchanged."""
    root = _seed(tmp_path)
    ids = [memory.append_decision(root, text=f"live decision {i}")["record"]["id"]
           for i in range(8)]
    assert [r["id"] for r in sr._decisions_tail(root)] == ids[-5:]


def test_expired_and_refuted_absent_from_federated_decision_tags(tmp_path: Path) -> None:
    """The cross-project miner was the last reader bypassing the shared live filter.

    Seeded through the real writer in a sibling install, then read back the way hooks.py
    does: a dead row in ANOTHER project must not cross into this project's context.
    """
    home = tmp_path
    self_root = _seed_install(home, "self_proj")
    other = _seed_install(home, "other_proj")

    memory.append_decision(other, text="live infra choice", tags=["infra"])
    memory.append_decision(other, text="dead ledger choice", tags=["ledger"],
                           expires_at=_iso(-1))
    failure = memory.append_decision(other, text="fp8 tag failure", kind="failure",
                                     tags=["fp8"])["record"]
    memory.append_decision(other, text="fp8 tag failure resolved", kind="failure", tags=["fp8"],
                           status="refuted", supersedes_id=failure["id"])

    tags = fed.gather_cross_project_signals(self_root, home=home)["decision_tags"]
    assert tags["infra"] == 1
    assert "ledger" not in tags  # expired → no longer crosses projects
    assert "fp8" not in tags     # folded by id, then dropped as refuted
