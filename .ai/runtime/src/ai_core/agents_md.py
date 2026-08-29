"""Render Code Brain's cross-session STATIC rules + DURABLE memory into a managed block
in ``AGENTS.md``.

Why this exists
---------------
Google Antigravity (``agy``) has **no** ``SessionStart``/``UserPromptSubmit`` hook event at
all — its only lifecycle events are ``PreToolUse``/``PostToolUse``/``PreInvocation``/
``PostInvocation``/``Stop`` — and its command (shell) hooks cannot inject model context via
stdout even where the event exists (injection is wired only for its SDK/declarative callers,
not ``jsonhook`` command hooks — verified empirically). So the ONLY way it can see Code
Brain's static behavioural rules or cross-session memory is a file it auto-loads at session
start with no tool call: ``AGENTS.md`` (verified: agy quoted this block, incl. the latest
decision).

Codex CLI ALSO auto-loads repo-root ``AGENTS.md`` (root down to cwd), while ALSO receiving
similar content via the SessionStart hook's ``additionalContext``. This module's block must
therefore be judged for CURRENTNESS by anything that might inject the same content a second
way (``hooks.build_context`` does this for the ``codex`` agent) — see ``is_current()``.

What this block contains — and does NOT contain
------------------------------------------------
ALWAYS the DURABLE memory body: decisions/todos/failures/resume/session-tail/learned rules —
``hooks._build_dynamic_sections("SessionStart", ..., include_auxiliary=False)``. It never
contains runtime-only recommendations/telemetry or VOLATILE, git/audit-derived sections
(branch state, dirty-tree
staleness banners, codebase-map, turn nudges — see ``hooks._build_volatile_sections``): those
change far more often than memory files, so mirroring them here and skipping re-injection
whenever memory happens to be unchanged would silently hide real drift. Keeping volatile
content out of this module is what makes a STAT-ONLY fingerprint of a bounded, durable file
list plus relevant environment toggles a safe, cheap currentness signal — see below.

The static Response/Search/Read behavioural rules (``hooks.static_rule_sections()`` — the
exact same text ``build_context`` injects, factored into one function so the two paths can
never drift) are included CONDITIONALLY: ``scripts/install-into.sh`` seeds a real install's
root ``AGENTS.md`` by copying the tracked ``.ai/AGENTS.md`` contract VERBATIM into the space
outside the managed markers (that copy is the file's "base"). When that already happened,
the base already carries the canonical static contract in its own words — embedding
``static_rule_sections()`` inside the managed block too would duplicate it a second time in
the SAME file. ``_base_has_static_contract()`` detects this by checking whether the base
(everything outside START/END) contains ``.ai/AGENTS.md``'s own heading line, and
``render_block()``/``refresh()`` only add the static rules to the managed block when it does
NOT — i.e. a hand-created or never-installed root ``AGENTS.md`` with no other static source
at all, which is exactly the case Codex's own SessionStart hook fallback exists for.

Currentness is a FINGERPRINT of declared durable-file stat signatures, never git or a
hash of the regenerated body
-----------------------------------------------------------------------------------------
Two earlier designs were both unsafe:

1. ``sha256(render_block())`` compared against an embedded hash re-derives the whole body
   on every SessionStart just to *possibly* discard the result (duplicates the exact cost
   ``build_context`` is trying to avoid paying twice), and at least one dynamic section
   (``_codebase_map_summary_context``, now correctly classified as VOLATILE) lists
   ``AGENTS.md`` itself as a cache-invalidation dependency — writing this very block could
   change what that section renders next, so body-equality is not stable ground truth.
2. Folding ``git status``/``HEAD`` into the fingerprint is ALSO unsafe: (a) this repo is
   frequently a linked worktree, where ``.git`` is a *file* (a gitdir pointer), not a
   directory — ``root/.git/HEAD`` simply does not exist there, silently degrading the
   fingerprint; (b) ``git status --porcelain`` on a large/dirty tree is not a bounded-cost
   operation and does not belong on the SessionStart hot path Code Brain otherwise keeps to
   single-digit milliseconds; (c) even the file-write self-reference from writing
   ``AGENTS.md`` itself was observed to flip the dirty-file count between "compute fp" and
   "verify fp" in testing. Branch/dirty state is exactly the kind of VOLATILE input that
   must stay OUT of this fingerprint and instead be delivered fresh every turn by
   ``hooks._build_volatile_sections`` (never mirrored, never currentness-gated).

Instead, ``fingerprint()`` computes a bounded STAT SIGNATURE — ``(relative path, size,
mtime_ns)`` for each of a small, explicitly declared list of the DURABLE files/directories
the static+dynamic sections actually read (``.ai/config.yaml`` plus decisions/todos/
session-current/learned-prompt/lessons/resume-snapshots dir/plans dir/peer-sync dir), plus
the current values of the bounded environment toggles that change mirrored static/durable
text — and folds them into one short hash with ``hashlib``. No git
call or subprocess: fixed inputs plus capped shallow metadata for plans, sync heartbeats,
and ``sessions/<id>/resume.json``. Two
calls against an unchanged durable-memory state always produce the same fingerprint, and a
mismatch is the safe trigger to fall back to the full static+durable body. ``AGENTS.md``
itself is deliberately NOT a fingerprint input (writing the file cannot change its own "is
it still current" answer).

To avoid git churn from rewriting a tracked file every turn, ``AGENTS.md`` is git-IGNORED
(install seeds it + adds it to the target .gitignore); durable, user-authored instructions
live in the tracked ``.ai/AGENTS.md`` instead, and any text outside the managed markers in
the root file is preserved byte-for-byte by ``compose()``.

Safety: never raise into the hook hot path (callers wrap in try/except), write only when the
rendered block changed, opt-out via ``AI_AGENTS_MD_MEMORY=0``.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .private_write import (
    atomic_write_private_text,
    list_root_confined_directory,
    read_root_confined_text,
    validate_root_confined_directory,
    validate_root_confined_regular_file,
)

START = "<!-- CODE-BRAIN:MEMORY:START (auto-generated by Code Brain; do not edit by hand) -->"
END = "<!-- CODE-BRAIN:MEMORY:END -->"
_HEADING = "## Code Brain Memory"
# Embedded in the block itself so ANY reader (this module, hooks.build_context, a future
# host) can judge currentness by comparing fingerprints without re-deriving root/agent state
# or re-rendering the body (see module docstring: "Currentness is a FINGERPRINT...").
_FP_PREFIX = "<!-- CODE-BRAIN:MEMORY:FP:"
_FP_SUFFIX = " -->"
# Bounded, explicitly declared list of the files/directories the static rules and the
# DURABLE dynamic sections in ``hooks._build_dynamic_sections`` actually read from, relative
# to repo root. Deliberately excludes anything git-derived (see module docstring point 2)
# and anything VOLATILE (``hooks._build_volatile_sections``'s inputs). The three bounded
# state directories also include shallow child signatures: an in-place plan/heartbeat
# update or replacement of ``sessions/<id>/resume.json`` need not change the top-level
# directory mtime. Listing is capped and never recursive beyond that known resume path.
#
# Kept in one place and covered by ``test_fingerprint_dependencies_cover_dynamic_inputs``
# so a new durable dynamic section that reads a new file is caught by a failing test rather
# than silently going unnoticed by is_current().
FINGERPRINT_DEPENDENCIES: tuple[str, ...] = (
    ".ai/config.yaml",  # AI_ROUTING_HINT_COMPACT-equivalent project config can live here too
    ".ai/AGENTS.md",  # canonical static source; its presence/content decides include_static
    ".ai/memory/decisions.jsonl",
    ".ai/memory/todos.jsonl",
    ".ai/memory/session-current.md",
    ".ai/memory/learned_prompt.md",
    ".ai/memory/lessons.jsonl",
    ".ai/memory/sessions",  # dir: resume snapshots (session_resume.read_latest_snapshot)
    ".ai/memory/plans",  # dir: active-plan progress (plan_state.active_summary)
    ".ai/memory/sync",  # dir: peer heartbeat summaries (memory_sync.peer_sync_summary)
)

# Environment toggles that change mirrored STATIC or DURABLE text. They must be folded into
# the fingerprint so an env change between refresh and currentness check cannot make stale
# content look current.
_FINGERPRINT_ENV: tuple[str, ...] = (
    "AI_ROUTING_HINT_COMPACT",
    "AI_PROMPT_GROWTH",
    "AI_LESSONS_INJECT",
)
_FINGERPRINT_DIRECTORY_LIMIT = 512
_FINGERPRINT_SHALLOW_DIRS: tuple[str, ...] = (
    ".ai/memory/plans",
    ".ai/memory/sessions",
    ".ai/memory/sync",
)


def enabled() -> bool:
    return os.environ.get("AI_AGENTS_MD_MEMORY", "1").lower() not in {"0", "false", "no", "off"}


def _confined_state(root: Path, path: Path):
    """No-follow state for one confined regular file or directory."""
    try:
        return validate_root_confined_regular_file(path, root=root)
    except OSError:
        try:
            return validate_root_confined_directory(
                path,
                root=root,
                require_safe_permissions=False,
            )
        except OSError:
            return None


def _state_token(rel: str, state) -> str:
    if state is None:
        return f"{rel}:_"
    return f"{rel}:{state.st_size}:{state.st_mtime_ns}"


def _shallow_directory_signature(root: Path, rel: str) -> list[str]:
    """Bounded child metadata for a declared durable state directory.

    ``sessions`` gets one additional known child (``resume.json``) per session directory;
    no arbitrary recursive walk is performed. Any trust/cap failure becomes an explicit
    token, forcing a conservative fingerprint rather than following a link.
    """
    base = root / rel
    try:
        names = list_root_confined_directory(
            base,
            root=root,
            max_entries=_FINGERPRINT_DIRECTORY_LIMIT,
            require_safe_permissions=False,
        )
    except OSError as exc:
        return [f"{rel}:children-unavailable:{type(exc).__name__}"]
    tokens: list[str] = []
    for name in names:
        child_rel = f"{rel}/{name}"
        child = base / name
        state = _confined_state(root, child)
        tokens.append(_state_token(child_rel, state))
        if rel == ".ai/memory/sessions" and state is not None:
            resume_rel = f"{child_rel}/resume.json"
            tokens.append(_state_token(resume_rel, _confined_state(root, child / "resume.json")))
    return tokens


def _stat_signature(root: Path) -> list[str]:
    """One ``"relpath:size:mtime_ns"`` token per declared dependency, missing paths as
    ``"relpath:_"`` (present-vs-absent is itself a currentness-relevant fact — e.g. todos.jsonl
    not existing yet vs. having been read once). Never raises. No git, no subprocess, no
    unbounded directory walk. Declared state directories add only capped shallow child
    metadata so in-place updates cannot look current forever."""
    root = Path(root)
    tokens: list[str] = []
    for rel in FINGERPRINT_DEPENDENCIES:
        tokens.append(_state_token(rel, _confined_state(root, root / rel)))
        if rel in _FINGERPRINT_SHALLOW_DIRS:
            tokens.extend(_shallow_directory_signature(root, rel))
    return tokens


def _env_signature() -> str:
    return "|".join(f"{k}={os.environ.get(k, '')}" for k in _FINGERPRINT_ENV)


def fingerprint(root: Path) -> str:
    """Deterministic, bounded-cost signature of the CURRENT durable-memory input state —
    NOT a hash of the rendered body, and NEVER git-derived (see module docstring for why:
    worktree ``.git``-as-file, unbounded ``git status`` cost, and self-reference all ruled
    it out). Two calls against an unchanged durable state (no memory-file write, no resume
    snapshot, no plan change, no relevant env change) always return the same value."""
    parts = _stat_signature(root) + [_env_signature()]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


_CANONICAL_SOURCE_PATH = ".ai/AGENTS.md"


def _canonical_static_heading(root: Path) -> str | None:
    """First non-blank line of the tracked ``.ai/AGENTS.md`` contract, or None if that
    file does not exist. Used only as a detectable fingerprint of "the static contract's
    own words are present", never rendered."""
    src = Path(root) / _CANONICAL_SOURCE_PATH
    try:
        text, _state = read_root_confined_text(
            src,
            root=Path(root),
            max_bytes=256 * 1024,
            require_private=False,
        )
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _base_has_static_contract(root: Path, base_text: str) -> bool:
    """True iff ``base_text`` (the root AGENTS.md content OUTSIDE the managed markers)
    already contains the canonical static contract's own heading line — i.e.
    ``scripts/install-into.sh`` already seeded this file by copying ``.ai/AGENTS.md``
    verbatim into that space. When true, the managed block must stay DURABLE-MEMORY-ONLY
    (the static rules are already present once, in the base); when false (no ``.ai/
    AGENTS.md`` at all, or a hand-authored root AGENTS.md with no other static source), the
    managed block includes the static rules as the sole fallback source for them."""
    heading = _canonical_static_heading(root)
    if not heading:
        return False
    return heading in base_text


def render_block(root: Path, *, include_static: bool) -> str:
    """Build the (optionally STATIC-rules-prefixed) DURABLE memory body (decisions/todos/
    resume/session-tail/...). When ``include_static`` is True, the static rules come
    first, in the exact text ``hooks.static_rule_sections()`` emits, so this file and
    ``build_context``'s own static block can never drift apart — used only when the base
    (outside the managed markers) has no other static source (see
    ``_base_has_static_contract``); otherwise the base's own copy of ``.ai/AGENTS.md`` is
    that source and this block stays durable-memory-only to avoid duplicating it in the
    same file.

    Deliberately calls ``hooks._build_dynamic_sections(..., include_auxiliary=False)``
    rather than
    ``build_context`` or ``hooks._build_volatile_sections``: VOLATILE, git/audit-derived
    content must never appear in this mirrored file (see module docstring). Runtime-only
    recommendations and telemetry stay on the hook path and are never freshness-gated by
    this file.
    """
    from .hooks import _build_dynamic_sections, static_rule_sections

    sections: list[str] = list(static_rule_sections()) if include_static else []
    sections.extend(
        _build_dynamic_sections(
            "SessionStart",
            {"agent": "antigravity", "dry": True},
            root,
            include_auxiliary=False,
        )
    )
    return "\n\n".join(s for s in sections if s).strip()


def compose(existing: str, block: str, *, fp: str) -> str:
    """Return ``existing`` with the managed section set to ``block`` (idempotent).

    The managed section embeds ``fp`` (a ``fingerprint()`` value, NOT a hash of ``block``)
    as an HTML comment so ``is_current()`` can judge currentness against a freshly computed
    ``fingerprint(root)`` without ever needing to re-render ``block``. Content OUTSIDE
    START/END is preserved byte-for-byte across repeated calls — this is what makes
    ``compose(compose(x, b, fp=f), b, fp=f) == compose(x, b, fp=f)`` (byte-idempotent for a
    stable block+fingerprint pair).
    """
    section = f"{START}\n{_HEADING}\n\n{_FP_PREFIX}{fp}{_FP_SUFFIX}\n\n{block}\n{END}"
    if START in existing and END in existing:
        pre = existing.split(START, 1)[0]
        post = existing.split(END, 1)[1]
        return f"{pre.rstrip()}\n\n{section}\n{post.lstrip()}".rstrip() + "\n"
    return f"{existing.rstrip()}\n\n{section}\n"


def stored_fingerprint(text: str) -> str | None:
    """Extract the embedded fingerprint from a managed block, or None if absent/malformed
    (including a pre-fingerprint block from an older Code Brain version that only embedded
    a content hash, or one from before the git-derived fingerprint was removed). Never
    raises: malformed input just yields None (treated as "not current" by ``is_current``)."""
    if START not in text or END not in text:
        return None
    try:
        block = text.split(START, 1)[1].split(END, 1)[0]
    except (IndexError, ValueError):
        return None
    idx = block.find(_FP_PREFIX)
    if idx == -1:
        return None
    rest = block[idx + len(_FP_PREFIX):]
    end = rest.find(_FP_SUFFIX)
    if end == -1:
        return None
    return rest[:end].strip() or None


def is_current(root: Path, *, path: str = "AGENTS.md") -> bool:
    """True iff ``<root>/<path>`` has a managed block whose embedded fingerprint matches
    ``fingerprint(root)`` computed right now — i.e. the auto-loaded file was written at (or
    after) the same durable-memory state the caller (e.g. Codex's SessionStart hook) sees,
    so the static rules + durable body it carries are still current.

    Cheap: only ``stat()`` calls on a bounded dependency list, never git and never a body
    re-render (see module docstring). False (never raises) when the file is missing, has no
    managed block, the block predates fingerprinting, or the fingerprint differs. False is
    always the SAFE answer here: the caller's fallback on a False is to inject the full
    body, never to skip it.
    """
    if not enabled():
        return False
    target = Path(root) / path
    try:
        text, _state = read_root_confined_text(
            target,
            root=Path(root),
            max_bytes=1024 * 1024,
            require_private=False,
        )
    except OSError:
        return False
    stored = stored_fingerprint(text)
    if not stored:
        return False
    return stored == fingerprint(root)


def _base_text(existing: str) -> str:
    """The part of an AGENTS.md file OUTSIDE the managed markers (or the whole thing, if
    there is no managed block yet) — i.e. what ``_base_has_static_contract`` inspects and
    what ``compose()`` preserves byte-for-byte."""
    if START in existing and END in existing:
        return existing.split(START, 1)[0] + existing.split(END, 1)[1]
    return existing


def refresh(root: Path, *, path: str = "AGENTS.md") -> bool:
    """Refresh the managed block in ``<root>/AGENTS.md`` (git-ignored). Returns True if
    written. No-op (False) when disabled, when there is no memory, or when unchanged
    (byte-idempotent: calling this twice with unchanged durable state writes nothing the
    second time, and content outside the managed markers is never touched).

    Whether the managed block includes the static rules depends on the file's CURRENT base
    (outside the markers) at refresh time — see ``_base_has_static_contract``. The
    fingerprint is computed BEFORE rendering the block so the two values are from the same
    instant (rendering can take long enough on a busy repo that a fingerprint taken after
    could already be stale relative to what was actually rendered).
    """
    if not enabled():
        return False
    root = Path(root)
    target = root / path
    try:
        existing, _state = read_root_confined_text(
            target,
            root=root,
            max_bytes=1024 * 1024,
            require_private=False,
        )
    except FileNotFoundError:
        existing = "# AGENTS.md\n"
    except OSError:
        return False
    include_static = not _base_has_static_contract(root, _base_text(existing))
    fp = fingerprint(root)
    block = render_block(root, include_static=include_static)
    if not block:
        return False
    new_text = compose(existing, block, fp=fp)
    if existing == new_text:
        return False
    try:
        atomic_write_private_text(target, new_text, root=root)
    except OSError:
        return False
    return True
