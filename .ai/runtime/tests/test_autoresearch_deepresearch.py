"""Deep research session tests (Stage 3 runtime) — deterministic state, path-traversal safe."""
from __future__ import annotations

from ai_core.autoresearch import deepresearch as dr, storage


def test_start_creates_session(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "what is reciprocal rank fusion?")
    assert s["session_id"].startswith("dr_") and s["status"] == "planning"
    assert s["question"] and s["subquestions"] == [] and s["sources"] == []


def test_get_roundtrip(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "q1")
    got = dr.get(ar, s["session_id"])
    assert got == s


def test_update_subquestions_sources_status(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "q2")
    sid = s["session_id"]
    dr.update(ar, sid, subquestions=["a", "b"], status="collecting")
    dr.update(ar, sid, add_source="src_x")
    dr.update(ar, sid, add_source="src_x")  # dedup
    got = dr.get(ar, sid)
    assert got["subquestions"] == ["a", "b"] and got["status"] == "collecting"
    assert got["sources"] == ["src_x"]


def test_update_rejects_bad_status(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "q3")
    assert dr.update(ar, s["session_id"], status="hacked") is None


def test_get_rejects_path_traversal(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    assert dr.get(ar, "../../etc/passwd") is None
    assert dr.get(ar, "dr_xx") is None         # wrong length
    assert dr.get(ar, "not_a_session") is None


def test_get_missing_session(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    assert dr.get(ar, "dr_0123456789ab") is None


def test_update_rejects_oversized_source(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "q")
    dr.update(ar, s["session_id"], add_source="x" * 1000)  # over _MAX_SOURCE_LEN
    assert dr.get(ar, s["session_id"])["sources"] == []


def test_update_caps_source_count(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    s = dr.start(ar, "q")
    sid = s["session_id"]
    for i in range(dr._MAX_SOURCES + 50):
        dr.update(ar, sid, add_source=f"src_{i:016x}")
    assert len(dr.get(ar, sid)["sources"]) == dr._MAX_SOURCES
