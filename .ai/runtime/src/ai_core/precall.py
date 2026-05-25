"""Pure decision logic for whether a Bash command should be intercepted
and routed to Code Brain's sandbox instead.

Stdlib-only (re, shlex). No file I/O. No side effects.
"""

from __future__ import annotations

import shlex
import re
from typing import Any

LONG_OUTPUT_BINARIES = ("grep", "egrep", "fgrep", "rg", "find", "tree", "ack", "ag")
SHELL_TOOL_NAMES = {
    "Bash",
    "Shell",
    "shell",
    "exec_command",
    "functions.exec_command",
    "terminal",
    "run_command",
}

HATCH_TOKENS = (
    "| wc",
    "| wc ",
    "&>/dev/null",
)
USER_RULE_HATCH_TOKENS = (
    *HATCH_TOKENS,
    "| head",
    "| head ",
    "| tail",
    "| tail ",
    "| less",
    "| more",
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
_SHELL_WRAPPERS = ("bash", "sh", "zsh")
_FALLBACK_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _strip_path(arg0: str) -> str:
    """Return the binary basename for the first token of a command."""
    if not arg0:
        return arg0
    # shlex preserves quoting; basename via rsplit on '/'
    return arg0.rsplit("/", 1)[-1]


def _is_shell_tool(tool_name: str) -> bool:
    normalized = _strip_path(str(tool_name or "")).strip()
    return normalized in SHELL_TOOL_NAMES or normalized.endswith(".exec_command")


def _fallback_intercept_unparsed_command(command_str: str) -> dict[str, Any] | None:
    """Best-effort broad-search detection when shell tokenization fails."""
    for raw_segment in _FALLBACK_SEGMENT_SPLIT.split(command_str):
        segment = raw_segment.strip()
        if not segment:
            continue
        rough_tokens = segment.split()
        if not rough_tokens:
            continue
        arg0 = _strip_path(rough_tokens[0].strip("\"'"))
        if arg0 in _SHELL_WRAPPERS:
            inner = " ".join(rough_tokens[1:])
            if "-c" in inner:
                nested = _fallback_intercept_unparsed_command(inner.split("-c", 1)[1])
                if nested is not None:
                    return nested
            continue
        if arg0 in ("rg", "find", "tree", "ack", "ag"):
            return {
                "intercept": True,
                "binary": arg0,
                "reason": f"shlex_failed_broad_search:{arg0}",
                "suggested_command": _build_suggested(command_str),
            }
        if arg0 in ("grep", "egrep", "fgrep") and _is_recursive_grep(rough_tokens):
            return {
                "intercept": True,
                "binary": arg0,
                "reason": f"shlex_failed_broad_search:{arg0}",
                "suggested_command": _build_suggested(command_str),
            }
        if arg0 == "git" and len(rough_tokens) >= 2 and rough_tokens[1] == "grep":
            return {
                "intercept": True,
                "binary": "grep",
                "reason": "shlex_failed_broad_search:git-grep",
                "suggested_command": _build_suggested(command_str),
            }
    return None


def _has_hatch(command_str: str) -> bool:
    if any(token in command_str for token in HATCH_TOKENS):
        return True
    normalized = command_str.replace("> /dev/null", ">/dev/null")
    return normalized.strip().startswith(">/dev/null") or " >/dev/null" in normalized


def _has_user_rule_hatch(command_str: str) -> bool:
    return _has_hatch(command_str) or any(token in command_str for token in USER_RULE_HATCH_TOKENS)


def _has_compound(command_str: str) -> bool:
    return any(sep in command_str for sep in _COMPOUND_SEPARATORS)


def _split_compound_segments(command_str: str) -> list[str]:
    """Split a shell command into coarse segments outside quotes."""
    try:
        lexer = shlex.shlex(command_str, posix=True, punctuation_chars=True)
    except TypeError:
        return []
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _COMPOUND_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    if len(segments) <= 1:
        return []
    return [" ".join(shlex.quote(part) for part in segment) for segment in segments]


def _shell_wrapped_command(tokens: list[str]) -> str | None:
    """Return the command string passed to `sh -c` / `bash -lc`, if obvious."""
    if not tokens:
        return None
    arg0 = _strip_path(tokens[0])
    if arg0 not in _SHELL_WRAPPERS:
        return None
    for idx, tok in enumerate(tokens[1:], start=1):
        if tok == "--":
            continue
        if tok.startswith("-") and "c" in tok and idx + 1 < len(tokens):
            return tokens[idx + 1]
    return None


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

    # Tokenize; if shlex fails (unbalanced quotes, etc.) still catch obvious
    # broad-search invocations so malformed quoting cannot bypass routing.
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        fallback = _fallback_intercept_unparsed_command(command_str)
        if fallback is not None:
            return fallback
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

    # 1. Hatch check (highest priority): allow only count/null-output forms.
    # `| head` / `| tail` are intentionally not hatches; they still bypass
    # indexed search/sandbox routing and only hide part of a broad command.
    if _has_hatch(command_str):
        return {
            "intercept": False,
            "binary": None,
            "reason": "hatch_detected",
            "suggested_command": None,
        }

    # 2. Shell wrappers: catch `bash -lc "rg foo"` / `sh -c "find ."` forms.
    wrapped = _shell_wrapped_command(tokens)
    if wrapped:
        wrapped_decision = should_intercept(wrapped)
        if wrapped_decision["intercept"]:
            return {
                "intercept": True,
                "binary": wrapped_decision["binary"],
                "reason": str(wrapped_decision["reason"]),
                "suggested_command": _build_suggested(command_str),
            }

    # 3. Compound command: inspect each segment and block the whole command if
    # any segment is broad output.
    if _has_compound(command_str):
        for segment in _split_compound_segments(command_str):
            segment_decision = should_intercept(segment)
            if segment_decision["intercept"]:
                return {
                    "intercept": True,
                    "binary": segment_decision["binary"],
                    "reason": str(segment_decision["reason"]),
                    "suggested_command": _build_suggested(command_str),
                }
        return {
            "intercept": False,
            "binary": None,
            "reason": "compound_command",
            "suggested_command": None,
        }

    # 4. Binary detection on first token.
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

    # `git grep` scans the tracked tree by default, so treat it as broad search.
    if arg0 == "git" and len(tokens) >= 2 and tokens[1] == "grep":
        return {
            "intercept": True,
            "binary": "grep",
            "reason": "long_output_binary:git-grep",
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
      3. Hard-coded long-output binary intercept → block (built-in always wins).
      4. Hatch detected → allow (user already capped output; do NOT apply user rules).
      5. Active extra_rules match → block (with rule_id).
      6. Dry-run extra_rules match → observe (do not block; caller increments counter).
      7. Otherwise → allow.
    """
    if not _is_shell_tool(tool_name):
        return {"action": "allow", "reason": "non_bash_tool"}

    if not isinstance(tool_input, dict) or ("command" not in tool_input and "CommandLine" not in tool_input and "commandLine" not in tool_input):
        return {"action": "allow", "reason": "no_command"}

    command = tool_input.get("command") or tool_input.get("CommandLine") or tool_input.get("commandLine") or ""
    if not command:
        return {"action": "allow", "reason": "empty_command"}

    decision = should_intercept(command)
    if decision["intercept"]:
        return {
            "action": "block",
            "reason": decision["reason"],
            "suggestion": decision["suggested_command"],
            "binary": decision["binary"],
        }

    if _has_user_rule_hatch(str(command)):
        return {"action": "allow", "reason": "hatch_detected"}

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
