"""AutoResearch — Stage 0 knowledge-wiki MVP (agent-driven, no-deps, offline-first).

The runtime owns ONLY deterministic work: file I/O, FTS indexing, verify-det,
locking, git-friendly storage. LLM steps (summarize, synthesize, judge) are
performed by the *calling agent* and written back through these primitives.
This preserves code-brain's no-deps default and the doctor.py default-off gate
(embeddings/remote_llm/external_notifications = false stays invariant).

See docs/prd.md v1.1 §3 (Stage 0) and §12 (review supplements).
"""
from __future__ import annotations

__all__ = ["storage", "models", "manifest", "fts", "verify_det", "locking"]
