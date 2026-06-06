"""Deep research session orchestration (Stage 3 runtime — deterministic state ONLY).

The LLM work (plan → subquestions, web-search query generation, synthesis) belongs to the
calling agent. The runtime only tracks the session deterministically: question,
subquestions, collected source ids, status — persisted under .state/deepresearch/<id>.json.
Source collection reuses ingest.stage_source(url=...) (SSRF-guarded); publishing reuses
ingest.commit_pages (verify-det gate). NO LLM, NO network in this module. stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import storage

_SID_RE = re.compile(r"^dr_[0-9a-f]{12}$")
STATUSES = ("planning", "collecting", "synthesizing", "published")
_MAX_SOURCE_LEN = 256   # source ids are short (src_<16hex>) — bound agent-supplied input
_MAX_SOURCES = 500      # cap collected sources per session (avoid session-file bloat)
_MAX_SUBQ = 100         # cap subquestions; single-user serial writer assumed (PRD §1.3, no lock)


def _sessions_dir(ar_root: Path) -> Path:
    d = ar_root / storage.STATE / "deepresearch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(ar_root: Path, session_id: str) -> Path | None:
    # session_id format is strictly validated → no path traversal via crafted ids
    if not _SID_RE.match(session_id or ""):
        return None
    return _sessions_dir(ar_root) / f"{session_id}.json"


def _gen_id(question: str) -> str:
    return "dr_" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]


def start(ar_root: Path, question: str) -> dict:
    sid = _gen_id(question)
    session = {"session_id": sid, "question": question, "subquestions": [],
               "sources": [], "status": "planning"}
    _sessions_dir(ar_root)
    _session_path(ar_root, sid).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    return session


def get(ar_root: Path, session_id: str) -> dict | None:
    p = _session_path(ar_root, session_id)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def update(ar_root: Path, session_id: str, *, subquestions: list[str] | None = None,
           add_source: str | None = None, status: str | None = None) -> dict | None:
    session = get(ar_root, session_id)
    if session is None:
        return None
    if subquestions is not None:
        session["subquestions"] = [str(s)[:512] for s in subquestions][:_MAX_SUBQ]
    if add_source is not None:
        if (isinstance(add_source, str) and add_source
                and len(add_source) <= _MAX_SOURCE_LEN
                and add_source not in session["sources"]
                and len(session["sources"]) < _MAX_SOURCES):
            session["sources"].append(add_source)
    if status is not None:
        if status not in STATUSES:
            return None
        session["status"] = status
    _session_path(ar_root, session_id).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    return session
