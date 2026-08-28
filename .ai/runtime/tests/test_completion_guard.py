"""Contract tests for the hard premature-stop guard.

The guard blocks a turn end when the WORKING TREE says work is unfinished. Two things must
both hold and they pull in opposite directions: it must actually fire (the previous guard,
`loop_continuation`, was dead in every installed project because its trigger was never
satisfied and its env flag was never plumbed), and it must be structurally incapable of
self-looping. Every test below pins one side of that tension.

The suite builds real git repos in tmp_path rather than monkeypatching `_git`: the signals
are defined in terms of what git reports, so a fake would test the mock instead of the
contract. `git` is required; the tests skip if it is unavailable.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_core import completion_guard as cg

pytestmark = pytest.mark.usefixtures("_guard_default_env")


@pytest.fixture
def _guard_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize inherited env so tests assert the shipped defaults, not the host's."""
    for name in ("AI_COMPLETION_GUARD", "AI_COMPLETION_GUARD_MAX_STALL"):
        monkeypatch.delenv(name, raising=False)


def _git(root: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


@pytest.fixture
def repo(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """A committed git repo with a .ai/ skeleton, ready for a 'turn' to dirty it.

    The subdirectory is keyed on the test id because pytest truncates long parametrized
    names, so two params of one test would otherwise share a tmp_path.
    """
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    safe = "".join(c if c.isalnum() else "_" for c in request.node.name)[-40:]
    root = tmp_path / f"repo_{safe}"
    (root / ".ai" / "cache").mkdir(parents=True)
    (root / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    assert cg.begin_request(root, "s1") is True
    return root


def _stop(root: Path, **payload) -> str | None:
    base = {"agent": "claude", "session_id": "s1"}
    base.update(payload)
    return cg.guard_directive(base, root)


# --------------------------------------------------------------------------- clean baseline

def test_clean_tree_yields(repo: Path) -> None:
    """The guard must not block a turn that left nothing unfinished."""
    assert cg.detect(repo) is None
    assert _stop(repo) is None


def test_ai_directory_churn_is_not_unfinished_work(repo: Path) -> None:
    """CB writes .ai/ on every hook; attributing that to the user's turn blocks forever."""
    (repo / ".ai" / "cache" / "scratch.py").write_text("def (:\n", encoding="utf-8")
    (repo / ".ai" / "notes.md").write_text("TODO: internal\n", encoding="utf-8")
    assert cg.touched_files(repo) == []
    assert _stop(repo) is None


def test_dirty_tree_alone_is_not_a_signal(repo: Path) -> None:
    """Mid-work edits are the normal state (blurivo idles at 913 modified files)."""
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert cg.detect(repo) is None


# ------------------------------------------------------------------------------ each signal

def test_signal_syntax_blocks_with_file_and_line(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n    return 1\n", encoding="utf-8")
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "syntax"
    reason = _stop(repo)
    assert reason is not None
    assert "cb-guard[syntax]" in reason and "app.py:1" in reason
    assert "Do NOT stop" in reason


def test_signal_conflict_detected_in_any_text_file(repo: Path) -> None:
    """No extension whitelist: git writes markers into whatever it was merging."""
    for name in ("conf.conf", "main.dart", "plain.txt"):
        (repo / name).write_text(
            "a\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n", encoding="utf-8"
        )
        signal = cg.detect(repo)
        assert signal is not None, name
        assert signal["kind"] == "conflict", (name, signal)
        assert signal["detail"].startswith(name), (name, signal)
        (repo / name).unlink()


def test_conflict_marker_needs_exact_seven_char_run_at_line_start(repo: Path) -> None:
    """A loose search matches every diff-handling source file, including the guard itself."""
    (repo / "notes.md").write_text(
        "text <<<<<<< inline mention\n====== six only\n", encoding="utf-8"
    )
    assert cg.detect(repo) is None


def test_signal_marker_only_fires_on_lines_this_turn_added(repo: Path) -> None:
    (repo / "notes.md").write_text("# notes\nTODO: wire this up\n", encoding="utf-8")
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "marker"
    assert "TODO" in signal["detail"]


def test_marker_words_in_explanatory_prose_do_not_block(repo: Path) -> None:
    """Documentation about the guard itself must not trigger the guard."""
    (repo / "notes.md").write_text(
        "# notes\nThe detector recognizes TODO/FIXME/XXX/HACK markers.\n",
        encoding="utf-8",
    )
    assert cg.detect(repo) is None


def test_actionable_markdown_marker_blocks(repo: Path) -> None:
    (repo / "notes.md").write_text("# notes\n- TODO: finish docs\n", encoding="utf-8")
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "marker"


def test_committed_marker_does_not_block(repo: Path) -> None:
    """Pre-existing TODOs are the repo's backlog; blocking on them never releases."""
    (repo / "notes.md").write_text("# notes\nTODO: ancient debt\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "backlog")
    assert cg.detect(repo) is None
    assert _stop(repo) is None


def test_pre_existing_marker_in_an_edited_file_does_not_block(repo: Path) -> None:
    """The decisive attribution case: the turn edits a file that ALREADY had a TODO.

    The file is now in `touched_files`, so a whole-file scan would blame this turn for the
    repo's existing backlog and block forever. Only lines the diff ADDED may count.
    """
    (repo / "legacy.py").write_text(
        "# TODO: inherited debt\ndef g():\n    return 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "legacy with debt")
    (repo / "legacy.py").write_text(
        "# TODO: inherited debt\ndef g():\n    return 2\n", encoding="utf-8"
    )
    assert "legacy.py" in cg.touched_files(repo)
    assert cg.detect(repo) is None
    assert _stop(repo) is None


def test_marker_added_beside_a_pre_existing_one_still_blocks(repo: Path) -> None:
    """Attribution must not become a blanket exemption for files that contain any marker."""
    (repo / "legacy.py").write_text("# TODO: inherited\ndef g():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "legacy")
    (repo / "legacy.py").write_text(
        "# TODO: inherited\ndef g():\n    return 1\n# FIXME: added now\n", encoding="utf-8"
    )
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "marker"
    assert "FIXME" in signal["detail"]


def test_marker_in_untracked_file_blocks(repo: Path) -> None:
    """An untracked file has no diff entry, so its whole content counts as added."""
    (repo / "new.py").write_text("# FIXME: unfinished\n", encoding="utf-8")
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "marker"
    assert "FIXME" in signal["detail"]


def test_deletion_only_edit_never_treats_the_whole_file_as_new(repo: Path) -> None:
    """A tracked deletion has no added hunk; an inherited TODO must stay inherited."""
    (repo / "legacy.md").write_text("# TODO: inherited\nremove me\n", encoding="utf-8")
    _git(repo, "add", "legacy.md")
    _git(repo, "commit", "-q", "-m", "legacy marker")
    (repo / "legacy.md").write_text("# TODO: inherited\n", encoding="utf-8")
    assert cg.detect(repo) is None


def test_marker_diff_is_scoped_to_bounded_candidate_paths(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "notes.md").write_text("# notes\nTODO: current\n", encoding="utf-8")
    original = cg._git
    calls: list[tuple[str, ...]] = []

    def counted(root: Path, *args: str):
        calls.append(args)
        return original(root, *args)

    monkeypatch.setattr(cg, "_git", counted)
    assert cg.detect(repo)["kind"] == "marker"
    diff = next(args for args in calls if "diff" in args)
    assert "notes.md" in diff
    assert cg._EXCLUDE_PATHSPEC[0] not in diff[diff.index("--") + 1 :]


def test_signal_acceptance_reads_the_eval_ledger(repo: Path) -> None:
    from ai_core import eval_loop

    eval_loop.record_case(
        repo, kind="acceptance", command="make test", outcome="fail", duration_ms=5
    )
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "acceptance"
    assert "re-run the acceptance" in signal["action"]


def test_passing_acceptance_clears_the_signal(repo: Path) -> None:
    from ai_core import eval_loop

    eval_loop.record_case(
        repo, kind="acceptance", command="make test", outcome="fail", duration_ms=5
    )
    eval_loop.record_case(
        repo, kind="acceptance", command="make test", outcome="pass", duration_ms=5
    )
    assert cg.detect(repo) is None


def test_non_acceptance_failure_is_ignored(repo: Path) -> None:
    """Only recorded acceptance rows gate completion; other eval kinds are informational."""
    from ai_core import eval_loop

    eval_loop.record_case(
        repo, kind="retrieval", command="probe", outcome="fail", duration_ms=5
    )
    assert cg.detect(repo) is None


def test_signal_plan_delegates_to_plan_state(repo: Path) -> None:
    from ai_core import plan_state

    plan_state.init_plan(repo, plan_id="p1", title="t", steps=["first", "second"])
    signal = cg.detect(repo)
    assert signal is not None and signal["kind"] == "plan"
    assert signal["plan_id"] == "p1"


# -------------------------------------------------------------------------------- precedence

def test_plan_outranks_every_tree_signal(repo: Path) -> None:
    """Precedence exists so the reason names ONE concrete next action, not a list."""
    from ai_core import plan_state

    plan_state.init_plan(repo, plan_id="p1", title="t", steps=["first"])
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    (repo / "notes.md").write_text("# notes\nTODO: later\n", encoding="utf-8")
    assert cg.detect(repo)["kind"] == "plan"


def test_conflict_outranks_syntax_and_marker(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    (repo / "notes.md").write_text(
        "# notes\nTODO: later\n<<<<<<< HEAD\n", encoding="utf-8"
    )
    assert cg.detect(repo)["kind"] == "conflict"


def test_syntax_outranks_marker(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n    # TODO: later\n", encoding="utf-8")
    assert cg.detect(repo)["kind"] == "syntax"


def test_marker_outranks_acceptance(repo: Path) -> None:
    from ai_core import eval_loop

    eval_loop.record_case(
        repo, kind="acceptance", command="make test", outcome="fail", duration_ms=5
    )
    (repo / "notes.md").write_text("# notes\nTODO: later\n", encoding="utf-8")
    assert cg.detect(repo)["kind"] == "marker"


# ----------------------------------------------------------------------- request attribution

def test_request_baseline_ignores_old_dirty_syntax_until_it_changes(repo: Path) -> None:
    (repo / "app.py").write_text("def old(:\n", encoding="utf-8")
    assert cg.begin_request(repo, "s1") is True
    assert _stop(repo) is None
    (repo / "app.py").write_text("def changed(:\n", encoding="utf-8")
    assert "cb-guard[syntax]" in str(_stop(repo))


def test_request_baseline_ignores_old_conflict_and_marker(repo: Path) -> None:
    (repo / "notes.md").write_text(
        "# notes\n# TODO: old\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> old\n",
        encoding="utf-8",
    )
    assert cg.begin_request(repo, "s1") is True
    assert _stop(repo) is None
    (repo / "notes.md").write_text(
        "# notes\n# TODO: old\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> old\n# FIXME: new\n",
        encoding="utf-8",
    )
    assert "cb-guard[marker]" in str(_stop(repo))


def test_request_baseline_scopes_acceptance_to_current_request(repo: Path) -> None:
    from ai_core import eval_loop

    eval_loop.record_case(
        repo, kind="acceptance", command="old test", outcome="fail", duration_ms=5
    )
    assert cg.begin_request(repo, "s1") is True
    assert _stop(repo) is None
    eval_loop.record_case(
        repo, kind="acceptance", command="current test", outcome="fail", duration_ms=5
    )
    assert "cb-guard[acceptance]" in str(_stop(repo))


def test_request_baseline_scopes_plan_to_current_request(repo: Path) -> None:
    from ai_core import plan_state

    plan_state.init_plan(repo, plan_id="old", steps=["a", "b"])
    assert cg.begin_request(repo, "s1") is True
    assert _stop(repo) is None
    plan_state.mark_step(repo, plan_id="old", index=1)
    assert "cb-guard[plan]" in str(_stop(repo))


def test_mutation_requires_a_relevant_successful_check(repo: Path) -> None:
    assert cg.begin_request(repo, "s1") is True
    assert cg.observe_tool_event(
        repo,
        {
            "agent": "codex",
            "session_id": "s1",
            "tool_use_id": "edit-1",
            "tool_name": "functions.apply_patch",
            "tool_input": {"patch": "*** Update File: app.py\n"},
        },
    ) is True
    reason = _stop(repo)
    assert reason is not None and "cb-guard[verification]" in reason

    assert cg.observe_tool_event(
        repo,
        {
            "agent": "codex",
            "session_id": "s1",
            "tool_use_id": "verify-1",
            "tool_name": "functions.exec_command",
            "tool_input": {"command": ".ai/runtime/.venv/bin/python -m pytest -q"},
            "tool_response": {"exit_code": 0},
        },
    ) is True
    assert _stop(repo) is None


def test_failed_or_weak_check_does_not_clear_code_mutation(repo: Path) -> None:
    assert cg.begin_request(repo, "s1") is True
    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "edit-2",
            "tool_name": "Write",
            "tool_input": {"file_path": "app.py"},
        },
    )
    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "verify-failed",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exitCode": 1},
        },
    )
    assert "cb-guard[verification]" in str(_stop(repo))

    assert cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "verify-background",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q", "run_in_background": True},
            "tool_response": {"exitCode": 0, "status": "running"},
        },
    ) is False

    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "verify-weak",
            "tool_name": "Bash",
            "tool_input": {"command": "git diff --check"},
            "tool_response": {"exitCode": 0},
        },
    )
    assert "cb-guard[verification]" in str(_stop(repo))


def test_docs_mutation_accepts_diff_check_but_new_edit_rearms(repo: Path) -> None:
    assert cg.begin_request(repo, "s1") is True
    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "docs-edit-1",
            "tool_name": "write_to_file",
            "tool_input": {"file_path": "README.md"},
        },
    )
    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "docs-check-1",
            "tool_name": "exec_command",
            "tool_input": {"command": "git -C . diff --check"},
            "tool_response": {"status": "completed", "exit_code": 0},
        },
    )
    assert _stop(repo) is None

    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "docs-edit-2",
            "tool_name": "Edit",
            "tool_input": {"file_path": "README.md"},
        },
    )
    assert "cb-guard[verification]" in str(_stop(repo))


def test_echoing_a_test_name_is_not_verification(repo: Path) -> None:
    assert cg.begin_request(repo, "s1") is True
    cg.observe_tool_event(
        repo,
        {"session_id": "s1", "tool_use_id": "edit-echo", "tool_name": "Write", "tool_input": {"file_path": "app.py"}},
    )
    cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "echo-check",
            "tool_name": "Bash",
            "tool_input": {"command": "echo pytest -q"},
            "tool_response": {"exit_code": 0},
        },
    )
    assert "cb-guard[verification]" in str(_stop(repo))


def test_verification_is_bound_to_edited_content_hash(repo: Path) -> None:
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "bound-edit",
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo / "app.py")},
        },
    )
    assert cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_use_id": "bound-check",
            "tool_name": "Bash",
            "tool_input": {"command": "python -m compileall app.py"},
            "tool_response": {"exit_code": 0},
        },
    )
    assert _stop(repo) is None
    # A change outside the observed tool stream invalidates the earlier proof.
    (repo / "app.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    assert "cb-guard[verification]" in str(_stop(repo))


def test_unbindable_ledger_fails_open(repo: Path) -> None:
    assert cg.observe_tool_event(
        repo,
        {
            "session_id": "s1",
            "tool_name": "Write",  # no host call id: not trustworthy enough to block
            "tool_input": {"file_path": "app.py"},
        },
    )
    assert _stop(repo) is None
    assert "verification was not certified" in cg.consume_degraded_notice(repo, "s1")


def test_corrupt_or_stale_request_evidence_fails_open(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    state_path = cg.state_path(repo)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["baselines"]["s1"]["ts"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _stop(repo) is None
    assert "baseline missing, corrupt, or expired" in cg.consume_degraded_notice(repo, "s1")
    state_path.write_text("{broken", encoding="utf-8")
    assert _stop(repo) is None


def test_marker_scan_overflow_fails_open(repo: Path) -> None:
    for i in range(cg.MAX_FILES_SCANNED + 1):
        text = "# TODO: current\n" if i == 0 else "changed\n"
        (repo / f"overflow-{i:03d}.md").write_text(text, encoding="utf-8")
    assert cg.detect(repo, cg._request_baseline(repo, "s1"), sid="s1") is None
    assert _stop(repo) is None
    assert "marker scan exceeded" in cg.consume_degraded_notice(repo, "s1")


def test_missing_baseline_is_nonblocking_and_warns_claude(repo: Path) -> None:
    from ai_core import hooks

    request = {"agent": "claude", "session_id": "unsupported-host"}
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    response = hooks.handle_hook(repo, "Stop", request)
    assert response.get("decision") != "block"
    assert "verification was not certified" in str(response.get("completion_guard_notice"))
    wire = hooks.hook_wire_output(response, request)
    assert wire["continue"] is True
    assert "completion guard degraded" in wire["systemMessage"]


def test_detect_uses_one_git_process_before_syntax_hit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    original = cg._git
    calls: list[tuple[str, ...]] = []

    def counted(root: Path, *args: str):
        calls.append(args)
        return original(root, *args)

    monkeypatch.setattr(cg, "_git", counted)
    assert cg.detect(repo)["kind"] == "syntax"
    assert len(calls) == 1
    assert calls[0][:2] == ("status", "--porcelain=v1")


# ------------------------------------------------------------------------------ safety rails

def test_stop_hook_active_cannot_bypass_but_stall_cap_still_yields(repo: Path) -> None:
    """Claude exposes this diagnostic after a nudge; evidence, not the flag, controls exit."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None
    assert _stop(repo, stop_hook_active=True) is not None
    assert _stop(repo, stop_hook_active=True) is None


@pytest.mark.parametrize("key", ["context_pressure", "compact_pending", "near_compaction"])
def test_context_pressure_yields(repo: Path, key: str) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo, **{key: True}) is None


def test_antigravity_normal_model_stop_is_guarded(repo: Path) -> None:
    """Current Antigravity can re-enter the loop; its wire polarity is handled separately."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    reason = _stop(
        repo,
        agent="antigravity",
        conversationId="agy-1",
        terminationReason="model_stop",
        fullyIdle=True,
    )
    assert reason is not None and "cb-guard[syntax]" in reason


@pytest.mark.parametrize("reason", ["error", "max_steps_exceeded", "cancelled"])
def test_antigravity_terminal_stop_yields(repo: Path, reason: str) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(
        repo,
        conversationId="agy-2",
        terminationReason=reason,
        fullyIdle=True,
    ) is None


def test_antigravity_non_idle_or_error_stop_yields(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo, conversationId="agy-3", fullyIdle=False) is None
    assert _stop(repo, conversationId="agy-4", fullyIdle=True, error="transport") is None


def test_trailing_question_is_not_a_guard_bypass(repo: Path) -> None:
    """The user's exact complaint was agents asking unnecessary questions instead of working."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo, last_assistant_message="Should I keep going?") is not None


@pytest.mark.parametrize("flag", ["user_input_required", "awaiting_user", "approval_required"])
def test_host_authenticated_user_input_flag_yields(repo: Path, flag: str) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo, **{flag: True}) is None


def test_off_repo_yields(tmp_path: Path) -> None:
    """No git tree means no evidence to reason about, so the guard must never block."""
    bare = tmp_path / "plain"
    (bare / ".ai").mkdir(parents=True)
    (bare / "a.py").write_text("def f(:\n", encoding="utf-8")
    assert cg.detect(bare) is None
    assert _stop(bare) is None


def test_kill_switch_disables(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None
    for value in ("0", "false", "no"):
        monkeypatch.setenv("AI_COMPLETION_GUARD", value)
        assert _stop(repo) is None, value


def test_enabled_by_default_with_no_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-ON is the whole fix: the old guard's opt-in env never reached consumers."""
    monkeypatch.delenv("AI_COMPLETION_GUARD", raising=False)
    monkeypatch.delenv("AI_LOOP_CONTINUATION", raising=False)
    assert cg._enabled() is True
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None


def test_doctor_surfaces_an_engaged_kill_switch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_core.doctor import check_completion_guard

    monkeypatch.setenv("AI_COMPLETION_GUARD", "0")
    check = check_completion_guard(repo)
    assert check.ok is False
    assert "disabled via AI_COMPLETION_GUARD" in check.detail


def test_malformed_payload_yields(repo: Path) -> None:
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert cg.guard_directive("not-a-dict", repo) is None  # type: ignore[arg-type]
    assert cg.guard_directive(None, repo) is None  # type: ignore[arg-type]


def test_detect_failure_yields_instead_of_raising(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: a guard bug must end the turn, never wedge it."""
    def _boom(_root):
        raise RuntimeError("signal probe exploded")

    monkeypatch.setattr(cg, "detect", _boom)
    assert _stop(repo) is None


# ------------------------------------------------------------------- no-progress escalation

def test_stall_escalation_gives_up_after_max_repeats(repo: Path) -> None:
    """Same signal + unchanged tree must stop being re-prompted, or a stuck model burns
    the entire budget on no progress."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None  # 1st block
    assert _stop(repo) is not None  # 2nd block
    assert _stop(repo) is None      # gave up: no progress across repeats


def test_stall_counter_resets_when_the_tree_changes(repo: Path) -> None:
    """Real progress must re-arm the guard, otherwise one stall disables it for the session."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None
    assert _stop(repo) is not None
    assert _stop(repo) is None
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "other.py").write_text("def g(:\n", encoding="utf-8")
    assert _stop(repo) is not None


def test_same_diff_stat_but_changed_evidence_rearms(repo: Path) -> None:
    """Content progress can keep identical line/byte stats; fingerprint exact evidence."""
    (repo / "app.py").write_text("def a(:\n    return 1\n", encoding="utf-8")
    assert _stop(repo) is not None
    assert _stop(repo) is not None
    assert _stop(repo) is None
    # Same path, same line count and same byte count, but different broken content.
    (repo / "app.py").write_text("def b(:\n    return 2\n", encoding="utf-8")
    assert _stop(repo) is not None


def test_stall_is_tracked_per_session(repo: Path) -> None:
    assert cg.begin_request(repo, "a")
    assert cg.begin_request(repo, "b")
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert cg.guard_directive({"agent": "claude", "session_id": "a"}, repo) is not None
    assert cg.guard_directive({"agent": "claude", "session_id": "a"}, repo) is not None
    assert cg.guard_directive({"agent": "claude", "session_id": "a"}, repo) is None
    assert cg.guard_directive({"agent": "claude", "session_id": "b"}, repo) is not None


def test_user_prompt_rearms_guard_in_same_session(repo: Path) -> None:
    from ai_core import hooks

    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None
    assert _stop(repo) is not None
    assert _stop(repo) is None
    hooks.handle_hook(
        repo,
        "UserPromptSubmit",
        {"agent": "claude", "session_id": "s1", "prompt": "continue", "dry": True},
    )
    # The old error is now request baseline, so it is not blamed on the new prompt. Actual
    # progress that still leaves syntax broken changes the evidence and re-arms the guard.
    assert _stop(repo) is None
    (repo / "app.py").write_text("def g(:\n", encoding="utf-8")
    assert _stop(repo) is not None


def test_stall_limit_is_configurable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_COMPLETION_GUARD_MAX_STALL", "1")
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    assert _stop(repo) is not None
    assert _stop(repo) is None


def test_stall_sidecar_is_capped(repo: Path) -> None:
    """The sidecar must not grow without bound — it is written on every block."""
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    for i in range(50):
        cg.guard_directive({"agent": "claude", "session_id": f"s{i}"}, repo)
    state = json.loads(cg.state_path(repo).read_text(encoding="utf-8"))
    assert len(state["sessions"]) <= 32


def test_degraded_notice_state_is_capped_without_counter_file_growth(repo: Path) -> None:
    counter_root = repo / ".ai" / "cache" / "loop_continuation"
    for i in range(50):
        assert cg.guard_directive(
            {"agent": "claude", "session_id": f"unsupported-{i}"}, repo
        ) is None
    state = json.loads(cg.state_path(repo).read_text(encoding="utf-8"))
    assert len(state["degraded"]) <= 32
    assert not counter_root.exists(), "fail-open notices must not create continuation ledgers"


def test_shared_budget_with_loop_continuation(repo: Path) -> None:
    """One 'keep going' ledger: exhausting loop_continuation's cap must silence the guard."""
    from ai_core import loop_continuation

    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    path = loop_continuation._counter_path(repo, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"count": loop_continuation.MAX_CONTINUATIONS, "first_ts": 1.0}),
        encoding="utf-8",
    )
    assert _stop(repo) is None


# ---------------------------------------------------------------------------- determinism

def test_detect_is_deterministic(repo: Path) -> None:
    (repo / "notes.md").write_text("# notes\nTODO: a\n", encoding="utf-8")
    (repo / "b.md").write_text("TODO: b\n", encoding="utf-8")
    first = cg.detect(repo)
    for _ in range(5):
        assert cg.detect(repo) == first


def test_files_scanned_are_bounded(repo: Path) -> None:
    for i in range(cg.MAX_FILES_SCANNED + 20):
        (repo / f"f{i:03d}.md").write_text("x\n", encoding="utf-8")
    assert len(cg.touched_files(repo)) <= cg.MAX_FILES_SCANNED


def test_status_parser_handles_spaces_and_staged_renames(repo: Path) -> None:
    spaced = repo / "file with space.py"
    spaced.write_text("def ok():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "file with space.py")
    _git(repo, "commit", "-q", "-m", "space")
    _git(repo, "mv", "file with space.py", "renamed file.py")
    files = cg.touched_files(repo)
    assert "renamed file.py" in files
    assert "file with space.py" not in files


def test_oversized_file_is_skipped(repo: Path) -> None:
    """A huge generated file must not be read into memory on the Stop hot path."""
    big = repo / "big.md"
    big.write_text("TODO x\n" * ((cg.MAX_FILE_BYTES // 7) + 10), encoding="utf-8")
    assert big.stat().st_size > cg.MAX_FILE_BYTES
    assert cg._read_text(big) is None


# ------------------------------------------------------------------------------ hook wiring


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_claude_compatible_stop_polarity_golden(agent: str) -> None:
    from ai_core import hooks

    request = {"agent": agent, "session_id": "golden"}
    blocked = {"hook": "Stop", "decision": "block", "reason": "unfinished"}
    allowed = {"hook": "Stop"}
    assert hooks.hook_wire_output(blocked, request) == {
        "decision": "block",
        "reason": "unfinished",
    }
    assert hooks.hook_wire_output(allowed, request) == {"continue": True}


def test_antigravity_invocation_and_stop_polarity_golden() -> None:
    from ai_core import hooks

    request = {"conversationId": "agy-golden", "workspacePaths": ["/tmp/project"]}
    assert hooks.hook_wire_output({"hook": "PreInvocation"}, request) == {"injectSteps": []}
    assert hooks.hook_wire_output(
        {"hook": "Stop", "decision": "block", "reason": "unfinished"}, request
    ) == {"decision": "continue", "reason": "unfinished"}
    assert hooks.hook_wire_output({"hook": "Stop"}, request) == {"decision": "stop"}
    assert hooks.hook_wire_output({"hook": "PostInvocation"}, request) == {}


@pytest.mark.parametrize("hook", ["Stop", "SubagentStop"])
def test_hook_blocks_on_both_stop_like_events(
    repo: Path, hook: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SubagentStop shares Stop's decision contract; a subagent that quits early is the
    same defect as a main agent that does."""
    from ai_core import hooks

    monkeypatch.delenv("AI_COMPLETION_GUARD", raising=False)
    assert hook in hooks._STOP_LIKE_HOOKS
    assert cg.begin_request(repo, f"w-{hook}")
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    response = hooks.handle_hook(repo, hook, {"agent": "claude", "session_id": f"w-{hook}"})
    assert response.get("decision") == "block"
    assert "cb-guard" in str(response.get("reason"))
    wire = hooks.codex_wire_output(response)
    assert wire.get("decision") == "block"
    assert "cb-guard" in str(wire.get("reason"))


@pytest.mark.parametrize("hook", ["Stop", "SubagentStop"])
def test_hook_lets_a_clean_turn_end(repo: Path, hook: str) -> None:
    from ai_core import hooks

    response = hooks.handle_hook(repo, hook, {"agent": "claude", "session_id": f"c-{hook}"})
    assert response.get("decision") != "block"
    assert hooks.codex_wire_output(response) == {"continue": True}


def test_antigravity_wire_inverts_stop_polarity(repo: Path) -> None:
    """P0 regression: `decision:block` ALLOWS an Antigravity stop; `continue` blocks it."""
    from ai_core import hooks

    assert cg.begin_request(repo, "agy-wire")
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    request = {
        "conversationId": "agy-wire",
        "workspacePaths": [str(repo)],
        "terminationReason": "model_stop",
        "fullyIdle": True,
    }
    response = hooks.handle_hook(repo, "Stop", request)
    assert response.get("decision") == "block"
    assert hooks.normalize_agent(request) == "antigravity"
    wire = hooks.hook_wire_output(response, request)
    assert wire == {"decision": "continue", "reason": response["reason"]}


def test_antigravity_clean_stop_emits_required_stop_decision(repo: Path) -> None:
    from ai_core import hooks

    request = {
        "conversationId": "agy-clean",
        "workspacePaths": [str(repo)],
        "terminationReason": "model_stop",
        "fullyIdle": True,
    }
    assert cg.begin_request(repo, "agy-clean")
    response = hooks.handle_hook(repo, "Stop", request)
    assert response.get("decision") != "block"
    assert hooks.hook_wire_output(response, request) == {"decision": "stop"}


def test_antigravity_posttooluse_wire_is_empty_object(repo: Path) -> None:
    from ai_core import hooks

    request = {
        "conversationId": "agy-post",
        "workspacePaths": [str(repo)],
        "stepIdx": 0,
        "toolCall": {"name": "write_to_file", "args": {"file_path": str(repo / "notes.md")}},
    }
    response = hooks.handle_hook(repo, "PostToolUse", request)
    assert hooks.hook_wire_output(response, request) == {}
    state = json.loads(cg.state_path(repo).read_text(encoding="utf-8"))
    assert state["activities"]["agy-post"]["mutation_seq"] == 1
    assert state["activities"]["agy-post"]["mutation_call_id"] == "agy-post:step:0"


def test_antigravity_first_preinvocation_captures_request_baseline(repo: Path) -> None:
    from ai_core import hooks

    (repo / "app.py").write_text("def old(:\n", encoding="utf-8")
    request = {
        "conversationId": "agy-baseline",
        "workspacePaths": [str(repo)],
        "invocationNum": 0,
        "initialNumSteps": 0,
    }
    response = hooks.handle_hook(repo, "PreInvocation", request)
    assert hooks.hook_wire_output(response, request) == {"injectSteps": []}
    clean_stop = {
        "conversationId": "agy-baseline",
        "workspacePaths": [str(repo)],
        "terminationReason": "model_stop",
        "fullyIdle": True,
    }
    response = hooks.handle_hook(repo, "Stop", clean_stop)
    assert hooks.hook_wire_output(response, clean_stop) == {"decision": "stop"}
    (repo / "app.py").write_text("def changed(:\n", encoding="utf-8")
    response = hooks.handle_hook(repo, "Stop", clean_stop)
    assert hooks.hook_wire_output(response, clean_stop)["decision"] == "continue"


def test_security_block_is_never_overridden(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not be able to replace a security decision's reason."""
    from ai_core import hooks

    called: list[int] = []

    def _tracked(*args, **kwargs):
        called.append(1)
        return "cb-guard[syntax]: should not be consulted"

    monkeypatch.setattr(cg, "guard_directive", _tracked)
    (repo / "app.py").write_text("def f(:\n", encoding="utf-8")
    response = hooks.handle_hook(
        repo, "Stop", {"agent": "claude", "session_id": "sec", "tool_input": {}}
    )
    if response.get("decision") == "block" and "cb-guard" not in str(response.get("reason")):
        assert not called, "guard consulted despite an existing block"
