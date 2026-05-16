"""Pure decision logic for whether a Bash command should be intercepted
and routed to Code Brain's sandbox instead.

Stdlib-only (re, shlex). No file I/O. No side effects.
"""

from __future__ import annotations

import shlex
from typing import Any

LONG_OUTPUT_BINARIES = ("grep", "egrep", "fgrep", "rg", "find", "tree", "ack", "ag")

HATCH_TOKENS = (
    "| head",
    "| tail",
    "| wc",
    "| less",
    "| more",
    "| head ",
    "| tail ",
    "| wc ",
    ">/dev/null",
    "> /dev/null",
    "&>/dev/null",
    "2>/dev/null",
)

RECURSIVE_GREP_FLAGS = (
    "-r",
    "-R",
    "--recursive",
    "-rn",
    "-rl",
    "-rL",
    "-Rn",
    "-RH",
    "-rIn",
    "-rni",
)

# Compound separators that signal a multi-step pipeline we won't unwind.
_COMPOUND_SEPARATORS = ("&&", "||", ";", "|")


def _strip_path(arg0: str) -> str:
    """Return the binary basename for the first token of a command."""
    if not arg0:
        return arg0
    # shlex preserves quoting; basename via rsplit on '/'
    return arg0.rsplit("/", 1)[-1]


def _has_hatch(command_str: str) -> bool:
    return any(token in command_str for token in HATCH_TOKENS)


def _has_compound(command_str: str) -> bool:
    return any(sep in command_str for sep in _COMPOUND_SEPARATORS)


def _is_recursive_grep(tokens: list[str]) -> bool:
    """True if any arg in tokens[1:] indicates recursive grep."""
    for tok in tokens[1:]:
        if tok in RECURSIVE_GREP_FLAGS:
            return True
        if tok == "--recursive":
            return True
        # Combined short flags like -rn, -rl, -Rn covered by RECURSIVE_GREP_FLAGS.
        # Also catch generic combined forms: a leading single dash followed by
        # letters that include 'r' or 'R' (but not long options like --color).
        if (
            len(tok) >= 2
            and tok.startswith("-")
            and not tok.startswith("--")
            and ("r" in tok[1:] or "R" in tok[1:])
        ):
            return True
    return False


def _build_suggested(command_str: str) -> str:
    return f".ai/bin/ai exec run -- {command_str}"


def should_intercept(command_str: str | None) -> dict[str, Any]:
    """Decide whether ``command_str`` should be intercepted.

    Returns a dict with keys: intercept, binary, reason, suggested_command.
    """
    if not command_str:
        return {
            "intercept": False,
            "binary": None,
            "reason": "empty_command",
            "suggested_command": None,
        }

    # Tokenize; if shlex fails (unbalanced quotes, etc.) bail conservatively.
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return {
            "intercept": False,
            "binary": None,
            "reason": "shlex_failed",
            "suggested_command": None,
        }

    if not tokens:
        return {
            "intercept": False,
            "binary": None,
            "reason": "empty_command",
            "suggested_command": None,
        }

    # 1. Hatch check (highest priority): if user already capped output, allow.
    if _has_hatch(command_str):
        return {
            "intercept": False,
            "binary": None,
            "reason": "hatch_detected",
            "suggested_command": None,
        }

    # 2. Compound command: conservative — don't intercept.
    if _has_compound(command_str):
        return {
            "intercept": False,
            "binary": None,
            "reason": "compound_command",
            "suggested_command": None,
        }

    # 3. Binary detection on first token.
    arg0 = _strip_path(tokens[0])

    if arg0 == "rg":
        return {
            "intercept": True,
            "binary": "rg",
            "reason": "long_output_binary:rg",
            "suggested_command": _build_suggested(command_str),
        }

    if arg0 in ("grep", "egrep", "fgrep"):
        if _is_recursive_grep(tokens):
            return {
                "intercept": True,
                "binary": arg0,
                "reason": f"long_output_binary:{arg0}",
                "suggested_command": _build_suggested(command_str),
            }
        return {
            "intercept": False,
            "binary": None,
            "reason": "grep_non_recursive",
            "suggested_command": None,
        }

    if arg0 == "find":
        return {
            "intercept": True,
            "binary": "find",
            "reason": "long_output_binary:find",
            "suggested_command": _build_suggested(command_str),
        }

    if arg0 == "tree":
        return {
            "intercept": True,
            "binary": "tree",
            "reason": "long_output_binary:tree",
            "suggested_command": _build_suggested(command_str),
        }

    if arg0 in ("ack", "ag"):
        return {
            "intercept": True,
            "binary": arg0,
            "reason": f"long_output_binary:{arg0}",
            "suggested_command": _build_suggested(command_str),
        }

    # `git grep` with recursive flag — same as recursive grep.
    if arg0 == "git" and len(tokens) >= 2 and tokens[1] == "grep":
        # Treat tokens[1:] as the inner grep command for flag detection.
        inner = tokens[1:]
        if _is_recursive_grep(inner):
            return {
                "intercept": True,
                "binary": "grep",
                "reason": "long_output_binary:grep",
                "suggested_command": _build_suggested(command_str),
            }

    return {
        "intercept": False,
        "binary": None,
        "reason": "unmatched",
        "suggested_command": None,
    }


def _match_extra_rules(
    command: str,
    rules: list[dict[str, Any]] | None,
    *,
    statuses: tuple[str, ...],
) -> dict[str, Any] | None:
    """Return the first matching rule with status in `statuses`, or None.

    Rules are pre-compiled by the caller (each entry must already have a `_compiled`
    re.Pattern). We never compile here to keep this pure-function and cheap.
    """
    if not rules:
        return None
    for rule in rules:
        if rule.get("status") not in statuses:
            continue
        compiled = rule.get("_compiled")
        if compiled is None:
            continue
        if compiled.search(command):
            return rule
    return None


def evaluate(
    tool_name: str,
    tool_input: Any,
    *,
    extra_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate a Claude Code tool call and decide allow/block/observe.

    Evaluation order (deterministic):
      1. Non-Bash tool → allow.
      2. Empty command → allow.
      3. Hatch detected → allow (user already capped output; do NOT apply user rules).
      4. Hard-coded long-output binary intercept → block (built-in always wins).
      5. Active extra_rules match → block (with rule_id).
      6. Dry-run extra_rules match → observe (do not block; caller increments counter).
      7. Otherwise → allow.
    """
    if tool_name != "Bash":
        return {"action": "allow", "reason": "non_bash_tool"}

    if not isinstance(tool_input, dict) or "command" not in tool_input:
        return {"action": "allow", "reason": "no_command"}

    command = tool_input.get("command", "")
    if not command:
        return {"action": "allow", "reason": "empty_command"}

    if _has_hatch(str(command)):
        return {"action": "allow", "reason": "hatch_detected"}

    decision = should_intercept(command)
    if decision["intercept"]:
        return {
            "action": "block",
            "reason": decision["reason"],
            "suggestion": decision["suggested_command"],
            "binary": decision["binary"],
        }

    active = _match_extra_rules(str(command), extra_rules, statuses=("active",))
    if active is not None:
        return {
            "action": "block",
            "reason": f"user_rule:{active.get('kind') or 'custom'}",
            "suggestion": str(active.get("suggestion") or "ai exec run -- <command>"),
            "binary": None,
            "rule_id": active.get("id"),
        }

    dry = _match_extra_rules(str(command), extra_rules, statuses=("dry_run",))
    if dry is not None:
        return {
            "action": "observe",
            "reason": f"user_rule_dry_run:{dry.get('kind') or 'custom'}",
            "rule_id": dry.get("id"),
        }

    return {"action": "allow", "reason": decision["reason"]}
