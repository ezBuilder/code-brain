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


def test_hatch_head_allows() -> None:
    result = should_intercept("grep -rn pattern src/ | head -50")
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_hatch_dev_null_allows() -> None:
    result = should_intercept('find . -name "*.tmp" 2>/dev/null')
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_hatch_wc_allows() -> None:
    result = should_intercept("grep -r pattern src/ | wc -l")
    assert result["intercept"] is False
    assert result["reason"] == "hatch_detected"


def test_compound_command_allows() -> None:
    result = should_intercept("cd src && grep -rn pattern .")
    assert result["intercept"] is False
    assert result["reason"] == "compound_command"


def test_unbalanced_quotes_allows() -> None:
    result = should_intercept('grep "broken pattern src/')
    assert result["intercept"] is False
    assert result["reason"] == "shlex_failed"


def test_evaluate_non_bash_tool_allows() -> None:
    result = evaluate("Read", {"file_path": "/tmp/x"})
    assert result["action"] == "allow"
    assert result["reason"] == "non_bash_tool"


def test_evaluate_bash_with_grep_recursive_blocks() -> None:
    result = evaluate("Bash", {"command": "grep -rn x src/"})
    assert result["action"] == "block"
    assert result["binary"] == "grep"
    assert result["suggestion"].startswith(".ai/bin/ai exec run -- ")


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
