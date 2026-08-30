"""Fingerprint-based currentness (ai_core.agents_md.fingerprint/is_current).

Covers two design corrections, in order:

1. Currentness must NOT be judged by re-rendering the dynamic body and hashing it (unsafe
   — see agents_md module docstring for the self-reference bug this replaced:
   _codebase_map_summary_context lists AGENTS.md itself as a cache dependency, so writing
   the managed block could change what that section renders next).
2. Currentness must NOT depend on git at all (a second, independent self-reference/cost
   bug: writing AGENTS.md changes `git status`'s dirty count on the very next call; a
   linked worktree's `.git` is a FILE, not a directory, silently breaking HEAD reads;
   `git status --porcelain` is not bounded-cost on a large/dirty tree). Currentness is a
   bounded stat()-only signature over a declared DURABLE file list plus one env toggle —
   no subprocess, no directory walk, no git.

Also covers the "no duplicate static contract" invariant: a real install seeds root
AGENTS.md's base (outside the managed markers) with a verbatim copy of `.ai/AGENTS.md`, so
the managed block must stay durable-memory-only in that case; only a base with no other
static source at all gets the static rules folded into the managed block.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

import ai_core.agents_md as A  # noqa: E402
import ai_core.hooks as hooks  # noqa: E402


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)
    return tmp_path


def _init_worktree_style_repo(tmp_path: Path) -> Path:
    """A repo where .git is a FILE (gitdir pointer), like a real git worktree — the
    linked worktree this suite itself commonly runs in. A git-derived fingerprint input
    would silently degrade here; a purely file-stat-based one must not care at all."""
    root = _init_repo(tmp_path)
    real_git = root / ".git"
    contents = None
    if real_git.is_dir():
        import shutil

        moved = tmp_path.parent / f"{tmp_path.name}-gitdir"
        shutil.move(str(real_git), str(moved))
        real_git.write_text(f"gitdir: {moved}\n", encoding="utf-8")
    return root


def test_fingerprint_dependencies_cover_known_dynamic_inputs() -> None:
    """Every file the static rules or _build_dynamic_sections are known to read for
    SessionStart (decisions, todos, session tail, learned-prompt rules, lessons, resume
    snapshots, active plans, the canonical static source) must be represented in
    FINGERPRINT_DEPENDENCIES. A new dynamic section that reads a new memory file and
    forgets to add it here would otherwise let is_current() return a stale True forever
    after that file changes — this test exists to catch that class of regression at review
    time rather than in production."""
    required = {
        ".ai/AGENTS.md",
        ".ai/memory/decisions.jsonl",
        ".ai/memory/todos.jsonl",
        ".ai/memory/session-current.md",
        ".ai/memory/learned_prompt.md",
        ".ai/memory/lessons.jsonl",
        ".ai/memory/sessions",
        ".ai/memory/plans",
    }
    declared = set(A.FINGERPRINT_DEPENDENCIES)
    missing = required - declared
    assert not missing, f"FINGERPRINT_DEPENDENCIES is missing known dynamic inputs: {missing}"


def test_fingerprint_never_calls_git(monkeypatch, tmp_path: Path) -> None:
    """Regression guard for the git-derived fingerprint design that was removed: a repo
    with NO .git at all (or any git binary) must still produce a stable fingerprint, and
    subprocess must never be invoked by fingerprint()."""
    called = {"n": 0}

    def fail_if_called(*_a, **_kw):
        called["n"] += 1
        raise AssertionError("fingerprint() must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    fp1 = A.fingerprint(tmp_path)
    fp2 = A.fingerprint(tmp_path)
    assert fp1 == fp2
    assert called["n"] == 0


def test_fingerprint_stable_across_new_commits(tmp_path: Path) -> None:
    """A new git commit alone (no memory-file change) must NOT change the fingerprint —
    branch/commit state is volatile and lives only in hooks._build_volatile_sections,
    never in the durable fingerprint."""
    root = _init_repo(tmp_path)
    fp_before = A.fingerprint(root)
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=str(root), check=True)
    fp_after = A.fingerprint(root)
    assert fp_before == fp_after


def test_fingerprint_stable_in_worktree_style_repo(tmp_path: Path) -> None:
    """A repo where .git is a file (gitdir pointer, like a linked worktree) must not
    degrade or crash the fingerprint — it never reads .git at all."""
    root = _init_worktree_style_repo(tmp_path)
    assert (root / ".git").is_file()
    fp1 = A.fingerprint(root)
    fp2 = A.fingerprint(root)
    assert fp1 == fp2


def test_fingerprint_changes_when_a_declared_file_changes(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    fp_before = A.fingerprint(root)
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text('{"decision": "x"}\n', encoding="utf-8")
    fp_after = A.fingerprint(root)
    assert fp_before != fp_after


def test_fingerprint_changes_when_mirrored_feature_toggle_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_PROMPT_GROWTH", raising=False)
    before = A.fingerprint(tmp_path)
    monkeypatch.setenv("AI_PROMPT_GROWTH", "1")
    assert A.fingerprint(tmp_path) != before


def test_fingerprint_changes_when_nested_resume_file_changes(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    session = root / ".ai" / "memory" / "sessions" / "session-a"
    session.mkdir(parents=True)
    resume = session / "resume.json"
    resume.write_text('{"state":"a"}\n', encoding="utf-8")
    before = A.fingerprint(root)

    resume.write_text('{"state":"b"}\n', encoding="utf-8")

    assert A.fingerprint(root) != before


def test_fingerprint_does_not_depend_on_agents_md_itself(tmp_path: Path) -> None:
    """The root AGENTS.md being written must never be a fingerprint input — writing the
    managed block would otherwise change the very signature used to judge whether that
    write is still current (self-reference). Note: `.ai/AGENTS.md` (the tracked SOURCE) is
    a legitimate dependency; the generated root `AGENTS.md` (the file this module writes)
    is not."""
    root = _init_repo(tmp_path)
    fp_before = A.fingerprint(root)
    (root / "AGENTS.md").write_text("# AGENTS.md\nsome unrelated user text\n", encoding="utf-8")
    fp_after = A.fingerprint(root)
    assert fp_before == fp_after


def test_is_current_true_after_refresh_then_false_after_a_new_decision(
    tmp_path: Path, monkeypatch
) -> None:
    root = _init_repo(tmp_path)
    monkeypatch.setattr(A, "render_block", lambda r, **kw: "DYNAMIC-BODY-X")
    assert A.refresh(root) is True
    assert A.is_current(root) is True

    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text('{"decision": "new one"}\n', encoding="utf-8")
    assert A.is_current(root) is False


def test_is_current_false_when_file_missing_or_unmanaged(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    assert A.is_current(root) is False  # no AGENTS.md at all
    (root / "AGENTS.md").write_text("# AGENTS.md\nno managed block here\n", encoding="utf-8")
    assert A.is_current(root) is False


def test_is_current_false_when_agents_md_memory_is_disabled(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    monkeypatch.setattr(A, "render_block", lambda r, **kw: "DURABLE")
    assert A.refresh(root) is True
    assert A.is_current(root) is True
    monkeypatch.setenv("AI_AGENTS_MD_MEMORY", "0")
    assert A.is_current(root) is False


def test_renderer_schema_change_invalidates_managed_block(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    assert A.refresh(root) is True
    assert A.is_current(root) is True

    monkeypatch.setattr(A, "_RENDERER_SCHEMA_VERSION", "test-next")

    assert A.is_current(root) is False


def test_render_block_omits_static_rules_when_base_has_canonical_contract(
    tmp_path: Path, monkeypatch
) -> None:
    """Scenario (a): the tracked .ai/AGENTS.md contract exists and the root AGENTS.md base
    (outside markers) already carries it verbatim — the real install-into.sh seeding
    outcome. The managed block must be durable-memory-only; the static rules must not
    appear inside it a second time."""
    root = _init_repo(tmp_path)
    ai_dir = root / ".ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "AGENTS.md").write_text(
        "# Code Brain Agent Contract\n\n## Response\n\n- rule text\n", encoding="utf-8"
    )
    # simulate install-into.sh's seed: base = verbatim copy of .ai/AGENTS.md
    (root / "AGENTS.md").write_text(
        (ai_dir / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(A, "render_block", A.render_block)  # keep real implementation

    assert A.refresh(root) is True
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    block = text.split(A.START, 1)[1].split(A.END, 1)[0]
    assert "Response: match the user's language" not in block
    assert "# Code Brain Agent Contract" in text  # base preserved outside the markers


def test_render_block_includes_static_rules_when_base_has_no_static_source(
    tmp_path: Path
) -> None:
    """Scenario (b): no `.ai/AGENTS.md` at all — a hand-authored/never-installed root
    AGENTS.md with no other static source. The managed block must include the static
    rules (the only fallback source for them on this host)."""
    root = _init_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        "# AGENTS.md\n\nSome user-authored notes unrelated to Code Brain.\n", encoding="utf-8"
    )

    assert A.refresh(root) is True
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    block = text.split(A.START, 1)[1].split(A.END, 1)[0]
    assert "Response: match the user's language" in block
    assert "Some user-authored notes unrelated to Code Brain." in text  # base preserved


def test_codex_session_start_skips_repeat_in_both_base_scenarios(tmp_path: Path) -> None:
    """Both base scenarios (a) and (b) must leave Codex's SessionStart hook seeing the
    file as current and NOT repeating the body via additionalContext."""
    for scenario in ("with_canonical_base", "without_canonical_base"):
        root = tmp_path / scenario
        root.mkdir()
        _init_repo(root)
        if scenario == "with_canonical_base":
            ai_dir = root / ".ai"
            ai_dir.mkdir(parents=True, exist_ok=True)
            (ai_dir / "AGENTS.md").write_text(
                "# Code Brain Agent Contract\n\n## Response\n\n- rule text\n", encoding="utf-8"
            )
            (root / "AGENTS.md").write_text(
                (ai_dir / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8"
            )
        mem = root / ".ai" / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        (mem / "decisions.jsonl").write_text(
            '{"decision": "Adopt MCP code_query for search"}\n', encoding="utf-8"
        )
        assert A.refresh(root) is True

        for payload in ({"agent": "codex"}, {}):
            ctx = hooks.build_context("SessionStart", payload, root=root)
            assert ctx.count("Adopt MCP code_query for search") == 0, (scenario, payload)
            assert "not repeated here" in ctx, (scenario, payload)


def test_codex_session_start_skips_dynamic_render_when_current(
    tmp_path: Path, monkeypatch
) -> None:
    """When AGENTS.md's managed block is current, build_context must not even call
    _build_dynamic_sections (bounded stat() check only, no body re-render on the hot
    path) and must emit the short pointer line instead of the full body."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(A, "render_block", lambda r, **kw: "UNIQUE-DYNAMIC-MARKER-abc123")
    assert A.refresh(root) is True

    called = {"n": 0}
    real = hooks._build_dynamic_sections

    def spy(hook_name, payload, r):
        called["n"] += 1
        return real(hook_name, payload, r)

    monkeypatch.setattr(hooks, "_build_dynamic_sections", spy)
    ctx = hooks.build_context("SessionStart", {"agent": "codex"}, root=root)
    assert called["n"] == 0
    assert "not repeated here" in ctx


def test_current_agents_md_does_not_suppress_runtime_auxiliary_sections(
    tmp_path: Path, monkeypatch
) -> None:
    root = _init_repo(tmp_path)
    monkeypatch.setattr(A, "render_block", lambda r, **kw: "DURABLE-ONLY")
    assert A.refresh(root) is True
    monkeypatch.setattr(
        hooks,
        "_build_auxiliary_sections",
        lambda hook_name, payload, r: ["OPTED-IN-RUNTIME-AUX"],
    )

    ctx = hooks.build_context("SessionStart", {"agent": "codex"}, root=root)

    assert "not repeated here" in ctx
    assert "OPTED-IN-RUNTIME-AUX" in ctx


def test_codex_session_start_falls_back_when_stale(tmp_path: Path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    monkeypatch.setattr(A, "render_block", lambda r, **kw: "STALE-BODY")
    assert A.refresh(root) is True
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text('{"decision": "Adopt MCP code_query"}\n', encoding="utf-8")

    ctx = hooks.build_context("SessionStart", {"agent": "codex"}, root=root)
    assert "not repeated here" not in ctx


def test_codex_hot_path_time_regression(tmp_path: Path, monkeypatch) -> None:
    """When current, build_context("SessionStart", agent=codex) must be fast — bounded
    stat() calls only, no git, not proportional to memory-file size. Guards against
    regressing back to a body-regeneration or git-based currentness check."""
    root = _init_repo(tmp_path)
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    big = "\n".join('{"decision": "d%d"}' % i for i in range(5000))
    (mem / "decisions.jsonl").write_text(big + "\n", encoding="utf-8")

    assert A.refresh(root) is True
    assert A.is_current(root) is True

    start = time.monotonic()
    for _ in range(20):
        hooks.build_context("SessionStart", {"agent": "codex"}, root=root)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"20 currentness-gated SessionStart calls took {elapsed:.2f}s"


def test_codex_hot_path_time_with_large_dirty_worktree(tmp_path: Path) -> None:
    """Regression guard for the git-based design that was removed: even with 800+ dirty
    working-tree entries and .git as a worktree-style file, currentness must stay fast and
    correct, because it never shells out to git at all."""
    root = _init_worktree_style_repo(tmp_path)
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text('{"decision": "d"}\n', encoding="utf-8")
    assert A.refresh(root) is True
    assert A.is_current(root) is True

    for i in range(800):
        (root / f"dirty_{i}.txt").write_text("x\n", encoding="utf-8")

    start = time.monotonic()
    for _ in range(20):
        assert A.is_current(root) is True
        hooks.build_context("SessionStart", {"agent": "codex"}, root=root)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"20 checks with 800 dirty files took {elapsed:.2f}s"


def test_antigravity_then_codex_no_duplication_shared_repo(tmp_path: Path) -> None:
    """Ordering regression: Antigravity refreshes the shared AGENTS.md managed block
    first (its only memory channel); Codex's SessionStart hook must then see the file as
    current and NOT repeat the body a second time via additionalContext."""
    root = _init_repo(tmp_path)
    mem = root / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text(
        '{"decision": "Adopt MCP code_query for search"}\n', encoding="utf-8"
    )

    assert A.refresh(root) is True  # simulates Antigravity's own refresh path
    agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Adopt MCP code_query for search" in agents_text

    ctx = hooks.build_context("SessionStart", {"agent": "codex"}, root=root)
    assert ctx.count("Adopt MCP code_query for search") == 0
    assert "not repeated here" in ctx
