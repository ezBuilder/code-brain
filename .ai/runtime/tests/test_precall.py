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
    assert result["suggested_command"] == ".ai/bin/ai exec run -- grep -rn pattern src/"


def test_grep_single_file_allows() -> None:
    result = should_intercept("grep pattern file.txt")
    assert result["intercept"] is False
    assert result["binary"] is None


def test_rg_always_intercepts() -> None:
    result = should_intercept("rg pattern")
    assert result["intercept"] is True
    assert result["binary"] == "rg"


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


def test_head_pipe_still_intercepts() -> None:
    result = should_intercept("grep -rn pattern src/ | head -50")
    assert result["intercept"] is True
    assert result["binary"] == "grep"


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


def test_shell_wrapper_intercepts_inner_command() -> None:
    result = should_intercept('bash -lc "rg pattern | head -20"')
    assert result["intercept"] is True
    assert result["binary"] == "rg"


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
