"""Agent-driven ingest pipeline. Runtime = deterministic only (PRD §12.2.2/§12.2.5).

Two phases so the LLM work stays with the calling agent:
  1. stage_source(): persist immutable raw + manifest (idempotent, lock-held),
     return a nonce-wrapped payload for the agent to summarize.
  2. commit_pages(): verify-det gate → write wiki pages + FTS + log atomically.

LLM-as-judge faithfulness stays in Stage 3. Full git-commit atomicity is a follow-up
(this commits file + FTS + log under the global ingest lock).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import storage, fts as fts_mod, locking, verify_det, nonce_verify, trust, injection_scan
from . import manifest as manifest_mod
from .models import RawManifest, WikiPageMetadata


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_id(sha256: str) -> str:
    return f"src_{sha256[:16]}"


def _raw_path(ar_root: Path, source_id: str) -> Path:
    return storage.raw_dir(ar_root) / f"{source_id}.txt"


def stage_source(
    ar_root: Path,
    *,
    content: str,
    source_url: str = "",
    title: str = "",
    trust_tier: str = "untrusted",
) -> dict:
    """Persist immutable raw + manifest (idempotent on sha256). NO summarization here.

    Returns a nonce-wrapped payload; the calling agent summarizes it into wiki pages
    and calls commit_pages(). trust_tier should be server-derived (host allowlist);
    all raw is treated as untrusted regardless (Phase 2 injection hardening).
    """
    storage.ensure_tree(ar_root)
    # Harden the nonce boundary BEFORE persisting anything: adversarial content that
    # embeds the delimiter markers is rejected up front (no raw/manifest write).
    try:
        nonce, wrapped = nonce_verify.wrap_untrusted(content)
    except nonce_verify.NonceCollision:
        return {"source_id": None, "duplicate": False, "nonce": None,
                "wrapped": None, "error": "nonce_collision"}
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    # trust_tier is SERVER-DERIVED from source_url, never from caller input (§12.2.6).
    # The `trust_tier` parameter is intentionally ignored (kept for signature stability).
    derived_tier = trust.derive_tier(source_url, trust.load_allowlist(ar_root))
    # Heuristic injection scan: flagged content is persisted but marked `quarantined`,
    # which propagates taint to any page derived from it at commit time (§12.2.6).
    inj = injection_scan.scan_injection(content)
    status = "quarantined" if inj["flagged"] else "draft"
    with locking.ingest_lock(ar_root):
        existing = manifest_mod.find_by_sha(ar_root, sha)
        if existing is not None:
            return {"source_id": existing.id, "duplicate": True, "wrapped": None}
        source_id = _gen_id(sha)
        _raw_path(ar_root, source_id).write_text(content, encoding="utf-8")
        manifest_mod.append(ar_root, RawManifest(
            id=source_id, sha256=sha, source_url=source_url, title=title or source_id,
            mime="text/plain", trust_tier=derived_tier, ingested_at=_now(),
            status=status, status_reason=",".join(inj["signals"]),
        ))
    return {"source_id": source_id, "duplicate": False, "nonce": nonce,
            "wrapped": wrapped, "quarantined": inj["flagged"]}


def _frontmatter(meta: WikiPageMetadata) -> str:
    return "\n".join([
        "---",
        f"id: {meta.id}",
        f"type: {meta.type}",
        "title: " + json.dumps(meta.title, ensure_ascii=False),
        f"sources: {json.dumps(meta.sources, ensure_ascii=False)}",
        f"updated: {meta.updated}",
        f"status: {meta.status}",
        f"taint: {str(meta.taint).lower()}",
        "---",
    ])


def _append_log(ar_root: Path, source_id: str, pages: list[str]) -> None:
    line = f"## [{_now()}] ingest {source_id}\n- touched: {', '.join(pages) or '(none)'}\n\n"
    with open(storage.log_path(ar_root), "a", encoding="utf-8") as fh:
        fh.write(line)


def commit_pages(ar_root: Path, *, source_id: str, pages: list[dict]) -> dict:
    """Verify-det gate → write wiki pages + FTS + log under the global lock.

    pages: [{rel_path, type, title, content, sources:[ids], citations:[{quote, sources}]}]
    A page failing verify-det on any citation is written with status:draft (quarantined),
    never dropped (so provenance is preserved and lint can surface it).
    """
    storage.ensure_tree(ar_root)
    rp = _raw_path(ar_root, source_id)
    source_texts = {source_id: rp.read_text(encoding="utf-8")} if rp.is_file() else {}
    wiki = storage.wiki_root(ar_root)

    # Phase 1 (no lock, no side effects): verify-det + render every page in memory.
    prepared = []  # (rel, body, sha, content, status)
    for p in pages:
        rel = str(p["rel_path"])
        content = str(p.get("content", ""))
        sources = list(p.get("sources") or [source_id])
        status = "active"
        for cit in p.get("citations", []) or []:
            r = verify_det.verify_claim(
                ar_root,
                list(cit.get("sources") or sources),
                str(cit.get("quote", "")),
                source_texts,
            )
            if not r.passed:
                status = "draft"
        # taint: a page derived from any quarantined source is tainted (laundering guard §12.2.6)
        taint = False
        for s in sources:
            rec = manifest_mod.find_by_id(ar_root, s)
            if rec is None or rec.status == "quarantined":
                taint = True  # unknown source → fail-closed; quarantined → laundering taint
                break
        meta = WikiPageMetadata(
            id=rel, type=str(p.get("type", "synthesis")),
            title=str(p.get("title", rel)), sources=sources,
            updated=_now(), status=status, taint=taint,
        )
        body = _frontmatter(meta) + "\n\n" + content
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        prepared.append((rel, body, sha, content, status))

    # Phase 2 (locked): write pages + FTS in one transaction. On any failure, roll back
    # the FTS transaction AND remove files we newly created this call (best-effort atomicity).
    written: list[str] = []
    drafted: list[str] = []
    new_files: list[Path] = []
    overwritten: dict[Path, str] = {}  # original content of pages we overwrite, for rollback
    with locking.ingest_lock(ar_root):
        conn = fts_mod.connect(ar_root)
        fts_mod.init_fts(ar_root)
        try:
            for rel, body, sha, content, status in prepared:
                page_path = wiki / rel
                existed = page_path.exists()
                page_path.parent.mkdir(parents=True, exist_ok=True)
                if existed and page_path not in overwritten:
                    try:
                        overwritten[page_path] = page_path.read_text(encoding="utf-8")
                    except OSError:
                        pass  # unreadable original — cannot restore, leave as-is
                page_path.write_text(body, encoding="utf-8")
                if not existed:
                    new_files.append(page_path)
                fts_mod.upsert_page(conn, rel, rel, sha, content)
                (written if status == "active" else drafted).append(rel)
            conn.commit()
            _append_log(ar_root, source_id, written + drafted)
        except Exception:
            # roll back FTS, remove newly-created files, restore overwritten originals
            conn.rollback()
            for pp in new_files:
                try:
                    pp.unlink()
                except OSError:
                    pass
            for pp, original in overwritten.items():
                try:
                    pp.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            raise
        finally:
            conn.close()
    return {"source_id": source_id, "written": written, "drafted": drafted}
