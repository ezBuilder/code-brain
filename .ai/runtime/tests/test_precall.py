"""Unit tests for ai_core.precall."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.precall import evaluate, should_intercept  # noqa: E402


def test_empty_command_allows() -> None:
    result = should_intercept("")
    assert result["intercept"] is False
    assert result["reason"] == "empty_command"
    assert result["binary"] is None
    assert result["suggested_command"] is None


def test_grep_recursive_intercepts() -> None:
    result = should_intercept("grep -rn pattern src/")
    assert result["intercept"] is True
    assert result["binary"] == "grep"
    assert result["suggested_command"] == ".ai/bin/ai exec run -- bash -lc 'grep -rn pattern src/'"


def test_grep_single_file_allows() -> None:
    result = should_intercept("grep pattern file.txt")
    assert result["intercept"] is False
    assert result["binary"] is None


def test_rg_always_intercepts() -> None:
    result = should_intercept("rg pattern")
    assert result["intercept"] is True
    assert result["binary"] == "rg"


def test_rg_explicit_file_target_allows_native_exact_lookup() -> None:
    result = should_intercept("rg -n '^def should_intercept' .ai/runtime/src/ai_core/precall.py")
    assert result["intercept"] is False
    assert result["reason"] == "rg_explicit_file_target"


def test_rg_glob_value_is_not_misclassified_as_file_target() -> None:
    result = should_intercept("rg -g '*.py' pattern")
    assert result["intercept"] is True


def test_rg_explicit_file_plus_directory_still_intercepts() -> None:
    result = should_intercept("rg pattern file.py src/")
    assert result["intercept"] is True


def test_rg_wildcard_target_still_intercepts() -> None:
    result = should_intercept("rg pattern 'src/**/*.py'")
    assert result["intercept"] is True


def test_rg_dotted_directory_target_still_intercepts() -> None:
    for command in (
        "rg TODO .github/",
        "rg secret .ai/",
        "rg x src.old/",
        "rg TODO .github",
        "rg secret .ai",
        "rg x .cache",
        "rg x src.old",
    ):
        assert should_intercept(command)["intercept"] is True, command


def test_rg_unknown_value_option_cannot_shift_pattern_into_file_target() -> None:
    for command in (
        "rg --threads 4 needle.py",
        "rg --color always needle.py",
        "rg --future-option value needle.py",
    ):
        assert should_intercept(command)["intercept"] is True, command


def test_rg_known_combined_flags_keep_exact_file_lookup_native() -> None:
    result = should_intercept("rg -nFi needle src/module.py")
    assert result["intercept"] is False
    assert result["reason"] == "rg_explicit_file_target"


def test_find_intercepts() -> None:
    result = should_intercept('find . -name "*.py"')
    assert result["intercept"] is True
    assert result["binary"] == "find"


def test_tree_intercepts() -> None:
    result = should_intercept("tree -L 3")
    assert result["intercept"] is True
    assert result["binary"] == "tree"


def test_ack_intercepts() -> None:
    result = should_intercept("ack pattern")
    assert result["intercept"] is True
    assert result["binary"] == "ack"


def test_head_pipe_bounds_output_and_allows() -> None:
    result = should_intercept("grep -rn pattern src/ | head -50")
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_bounded_pipe_does_not_hide_unbounded_sibling_command() -> None:
    commands = (
        "grep -rn x src | head -5 && grep -rn secret /",
        "rg a file.py | head -2 ; find / -name '*.pem'",
        "echo ok | wc -l && rg pattern /",
        "echo ok >/dev/null && rg pattern /",
    )
    for command in commands:
        assert should_intercept(command)["intercept"] is True, command


def test_bounded_pipe_does_not_hide_newline_sibling_command() -> None:
    commands = (
        "rg pattern /\nprintf ok | head -1",
        "printf ok | head -1\nfind / -name '*.pem'",
        "rg a file.py | head -2\ngrep -rn secret /",
    )
    for command in commands:
        assert should_intercept(command)["intercept"] is True, command


def test_quoted_or_escaped_newline_is_not_a_sibling_boundary() -> None:
    quoted = should_intercept("rg 'first\nsecond' file.py | head -2")
    escaped = should_intercept("rg pattern file.py \\\n| head -2")

    assert quoted["intercept"] is False
    assert escaped["intercept"] is False


def test_nested_shell_forms_do_not_bypass_broad_search_routing() -> None:
    commands = (
        "(rg pattern /)",
        "$(rg pattern /)",
        "`rg pattern /`",
        "{ rg pattern /; }",
        "echo $(grep -rn pattern /)",
        "if find / -name '*.pem'; then echo found; fi",
    )
    for command in commands:
        result = should_intercept(command)
        assert result["intercept"] is True, command
        assert str(result["reason"]).startswith("nested_shell:"), command


def test_only_final_genuinely_bounded_pipeline_stage_is_a_hatch() -> None:
    blocked = (
        "rg pattern / | less",
        "rg pattern / | more",
        "rg pattern / | tail -n +1",
        "rg pattern / | head -1000000",
        "rg pattern / | head -20 | cat",
    )
    for command in blocked:
        assert should_intercept(command)["intercept"] is True, command

    for command in ("rg pattern / | head -20", "rg pattern / | tail -n 20"):
        assert should_intercept(command)["intercept"] is False, command


def test_quoted_head_text_is_not_an_output_hatch() -> None:
    result = should_intercept("rg 'pattern | head'")
    assert result["intercept"] is True
    assert result["binary"] == "rg"


def test_stderr_dev_null_still_intercepts() -> None:
    result = should_intercept('find . -name "*.tmp" 2>/dev/null')
    assert result["intercept"] is True
    assert result["binary"] == "find"


def test_stdout_dev_null_allows() -> None:
    result = should_intercept('find . -name "*.tmp" >/dev/null')
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_hatch_wc_allows() -> None:
    result = should_intercept("grep -r pattern src/ | wc -l")
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_compound_command_intercepts_broad_segment() -> None:
    result = should_intercept("cd src && grep -rn pattern .")
    assert result["intercept"] is True
    assert result["binary"] == "grep"
    suggestion = str(result["suggested_command"])
    assert "bash -lc" in suggestion
    assert should_intercept(suggestion)["intercept"] is False


def test_shell_wrapper_allows_bounded_inner_command() -> None:
    result = should_intercept('bash -lc "rg pattern | head -20"')
    assert result["intercept"] is False


def test_git_grep_intercepts() -> None:
    result = should_intercept("git grep pattern")
    assert result["intercept"] is True
    assert result["binary"] == "grep"
    assert result["reason"] == "long_output_binary:git-grep"


def test_unbalanced_non_search_allows() -> None:
    result = should_intercept('echo "broken')
    assert result["intercept"] is False
    assert result["reason"] == "shlex_failed"


def test_unbalanced_recursive_grep_blocks() -> None:
    result = should_intercept('grep -rn "broken pattern src/')
    assert result["intercept"] is True
    assert result["binary"] == "grep"
    assert result["reason"] == "shlex_failed_broad_search:grep"


def test_evaluate_non_bash_tool_allows() -> None:
    result = evaluate("Read", {"file_path": "/tmp/x"})
    assert result["action"] == "allow"
    assert result["reason"] == "non_bash_tool"


def test_evaluate_bash_with_grep_recursive_blocks() -> None:
    result = evaluate("Bash", {"command": "grep -rn x src/"})
    assert result["action"] == "block"
    assert result["binary"] == "grep"
    assert result["suggestion"].startswith(".ai/bin/ai exec run -- ")


def test_evaluate_codex_exec_command_blocks() -> None:
    result = evaluate("functions.exec_command", {"command": "rg x"})
    assert result["action"] == "block"
    assert result["binary"] == "rg"


def test_evaluate_bash_no_command_allows() -> None:
    result = evaluate("Bash", {})
    assert result["action"] == "allow"
    assert result["reason"] == "no_command"


def test_evaluate_long_command_path_resolves_binary() -> None:
    result = evaluate("Bash", {"command": "/usr/bin/grep -r foo bar/"})
    assert result["action"] == "block"
    assert result["binary"] == "grep"
    assert result["suggestion"].startswith(".ai/bin/ai exec run -- ")


def test_egrep_recursive_intercepts() -> None:
    result = should_intercept("egrep -R pattern src/")
    assert result["intercept"] is True
    assert result["binary"] == "egrep"


def test_ag_intercepts() -> None:
    result = should_intercept("ag pattern")
    assert result["intercept"] is True
    assert result["binary"] == "ag"


def test_evaluate_non_dict_tool_input_allows() -> None:
    result = evaluate("Bash", None)
    assert result["action"] == "allow"
    assert result["reason"] == "no_command"


def test_evaluate_antigravity_run_command_blocks() -> None:
    # run_command with CommandLine parameter should be correctly evaluated as a shell tool
    result = evaluate("run_command", {"CommandLine": "rg pattern"})
    assert result["action"] == "block"
    assert result["binary"] == "rg"
    assert result["suggestion"].startswith(".ai/bin/ai exec run -- ")


def test_evaluate_antigravity_run_command_allows_safe() -> None:
    result = evaluate("run_command", {"CommandLine": "echo hello"})
    assert result["action"] == "allow"
    assert result["reason"] == "unmatched"
