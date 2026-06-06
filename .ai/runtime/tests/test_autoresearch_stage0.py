"""Stage 0 core primitive tests (PRD §12.2 / §10.5 test strategy).

Deterministic only — no LLM, no network. Validates manifest idempotency, FTS
indexing/search/rebuild, verify-det gate, and global ingest lock mutual exclusion.
"""
from __future__ import annotations

import pathlib

import pytest

from ai_core.autoresearch import storage, models, manifest, fts, verify_det, locking


@pytest.fixture()
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    r = tmp_path / "ar"
    storage.ensure_tree(r)
    return r


def _mk(id_="src_1", sha="abc") -> models.RawManifest:
    return models.RawManifest(
        id=id_, sha256=sha, source_url="https://x", title="T",
        mime="text/markdown", trust_tier="untrusted", ingested_at="2026-06-05T00:00:00Z",
    )


def test_tree_created(root):
    assert storage.manifest_path(root).parent.is_dir()
    assert storage.wiki_root(root).is_dir()
    assert storage.locks_dir(root).is_dir()


def test_manifest_append_idempotent(root):
    m = _mk()
    assert manifest.append(root, m) is True
    assert manifest.append(root, m) is False  # idempotent on sha256
    assert manifest.id_exists(root, "src_1")
    assert manifest.find_by_sha(root, "abc").title == "T"
    assert len(manifest.read_all(root)) == 1


def test_manifest_distinct_sha_appends(root):
    assert manifest.append(root, _mk("src_1", "aaa")) is True
    assert manifest.append(root, _mk("src_2", "bbb")) is True
    assert len(manifest.read_all(root)) == 2


def test_fts_index_and_search(root):
    fts.init_fts(root)
    c = fts.connect(root)
    fts.upsert_page(c, "concepts/rrf.md", "concepts/rrf.md", "sha",
                    "reciprocal rank fusion combines bm25 and dense retrieval")
    c.commit()
    c.close()
    res = fts.search(root, "fusion", k=5)
    assert res and res[0]["page"] == "concepts/rrf.md"


def test_fts_search_missing_db_is_empty(root):
    assert fts.search(root, "anything", k=5) == []


def test_fts_rebuild_from_wiki(root):
    (storage.wiki_root(root) / "concepts" / "hybrid.md").write_text(
        "# Hybrid\nhybrid search uses embeddings and bm25", encoding="utf-8")
    n = fts.rebuild_index(root)
    assert n == 1
    res = fts.search(root, "embeddings", k=5)
    assert res and "hybrid" in res[0]["page"]


def test_verify_det_pass(root):
    manifest.append(root, _mk())
    r = verify_det.verify_claim(root, ["src_1"], "the title", {"src_1": "this is THE Title text"})
    assert r.passed and r.status == "active" and r.failed_reasons == []


def test_verify_det_missing_source(root):
    r = verify_det.verify_claim(root, ["nope"], "x", {})
    assert not r.passed and r.status == "draft"
    assert "source_id_missing" in r.failed_reasons


def test_verify_det_quote_mismatch(root):
    manifest.append(root, _mk())
    r = verify_det.verify_claim(root, ["src_1"], "absent phrase", {"src_1": "different content"})
    assert not r.passed and "quote_not_in_source" in r.failed_reasons


def test_verify_det_bad_format(root):
    r = verify_det.verify_claim(root, [], "x", {})
    assert not r.passed and "citation_format" in r.failed_reasons


def test_parse_citations():
    assert verify_det.parse_citations("see [[src_1]] and (source: src_2) end") == ["src_1", "src_2"]


def test_lock_mutual_exclusion(root):
    with locking.ingest_lock(root, timeout_s=2):
        with pytest.raises(locking.LockBusy):
            with locking.ingest_lock(root, timeout_s=0.3):
                pass


def test_lock_released_after_exit(root):
    with locking.ingest_lock(root, timeout_s=2):
        pass
    with locking.ingest_lock(root, timeout_s=2):  # must re-acquire cleanly
        pass
