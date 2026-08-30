"""Pure decision logic for whether a Bash command should be intercepted
and routed to Code Brain's sandbox instead.

Stdlib-only (re, shlex). No file I/O. No side effects.
"""

from __future__ import annotations

import re
import shlex
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

_BOUNDED_PIPELINE_BINARIES = frozenset({"head", "tail", "wc"})
_HATCH_CONTROL_TOKENS = frozenset(
    {"&&", "||", ";", "&", "(", ")", "{", "}", "`", "$", "$("}
)
_PIPELINE_MAX_LINES = 200
_PIPELINE_MAX_BYTES = 16 * 1024

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
_SHELL_PUNCTUATION = "();<>|&`{}$"
_NESTED_COMMAND_BOUNDARIES = frozenset(
    {"(", "$(", "`", "{", ";", "&&", "||", "|", "!", "if", "elif", "while", "until", "then", "do"}
)
_NESTED_COMMAND_END = frozenset({")", "}", "`", ";", "&&", "||", "|", "then", "do"})
_FALLBACK_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_RG_VALUE_OPTIONS = frozenset(
    {
        "-A", "--after-context", "-B", "--before-context", "-C", "--context",
        "-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob",
        "-m", "--max-count", "--max-columns", "--path-separator", "--pre",
        "--pre-glob", "-r", "--replace", "--sort", "--sortr", "-t", "--type",
        "-T", "--type-not", "--encoding", "--engine",
    }
)
_RG_PATTERN_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file"})
_RG_FLAG_OPTIONS = frozenset(
    {
        "--binary", "--byte-offset", "--case-sensitive", "--column", "--crlf",
        "--fixed-strings", "--follow", "--hidden", "--ignore-case", "--invert-match",
        "--line-number", "--line-regexp", "--max-columns-preview", "--multiline",
        "--multiline-dotall", "--no-config", "--no-filename", "--no-heading",
        "--no-ignore", "--no-ignore-dot", "--no-ignore-exclude", "--no-ignore-files",
        "--no-ignore-global", "--no-ignore-parent", "--no-ignore-vcs", "--no-line-number",
        "--no-messages", "--no-require-git", "--no-unicode", "--only-matching",
        "--pcre2", "--quiet", "--smart-case", "--text", "--trim", "--unicode",
        "--with-filename", "--word-regexp", "-F", "-H", "-L", "-N", "-P", "-S",
        "-U", "-i", "-n", "-o", "-q", "-s", "-v", "-w", "-x",
    }
)
_RG_COMBINABLE_SHORT_FLAGS = frozenset("FHLNPSUinoqsvwx")
_FILELIKE_BASENAMES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        "CMakeLists.txt",
        "Dockerfile",
        "Gemfile",
        "LICENSE",
        "Makefile",
        "Procfile",
        "Rakefile",
    }
)
_FILELIKE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cfg", ".clj", ".cljs", ".cljc", ".cmake", ".conf",
        ".cpp", ".cs", ".css", ".csv", ".cxx", ".dart", ".edn", ".erl",
        ".ex", ".exs", ".fish", ".fs", ".fsi", ".fsx", ".go", ".gql",
        ".gradle", ".graphql", ".h", ".hpp", ".hrl", ".hs", ".htm", ".html",
        ".ini", ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".kts",
        ".less", ".lhs", ".lock", ".lua", ".md", ".mdx", ".mjs", ".ml",
        ".mli", ".php", ".properties", ".proto", ".ps1", ".py", ".pyi",
        ".r", ".rb", ".rs", ".rst", ".sass", ".scala", ".sc", ".scss",
        ".sh", ".sql", ".svelte", ".swift", ".toml", ".ts", ".tsx", ".tsv",
        ".txt", ".vue", ".xml", ".yaml", ".yml", ".zsh",
    }
)


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


def _shell_tokens(command_str: str) -> list[str] | None:
    try:
        # Include command-substitution and brace punctuation so nested broad
        # searches cannot hide behind ``$(...)``, backticks, or ``{ ...; }``.
        lexer = shlex.shlex(command_str, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return None


def _split_unquoted_newline_segments(command_str: str) -> list[str]:
    """Split shell command siblings on real newlines, preserving quoted/escaped ones."""

    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command_str):
        char = command_str[index]
        if char == "\\" and quote != "'" and index + 1 < len(command_str):
            current.extend((char, command_str[index + 1]))
            index += 2
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "\n":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments if len(segments) > 1 else []


def _bounded_count(value: str, *, maximum: int) -> bool:
    return value.isdigit() and int(value) <= maximum


def _bounded_consumer_args(binary: str, args: list[str]) -> bool:
    if binary == "wc":
        return all(arg.startswith("-") and not arg.startswith("--files0-from") for arg in args)

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-q", "--quiet", "--silent", "-v", "--verbose", "-z", "--zero-terminated"}:
            index += 1
            continue
        if re.fullmatch(r"-\d+", arg):
            if not _bounded_count(arg[1:], maximum=_PIPELINE_MAX_LINES):
                return False
            index += 1
            continue
        if arg in {"-n", "--lines", "-c", "--bytes"}:
            if index + 1 >= len(args):
                return False
            maximum = _PIPELINE_MAX_BYTES if arg in {"-c", "--bytes"} else _PIPELINE_MAX_LINES
            if not _bounded_count(args[index + 1], maximum=maximum):
                return False
            index += 2
            continue
        if arg.startswith("--lines="):
            if not _bounded_count(arg.split("=", 1)[1], maximum=_PIPELINE_MAX_LINES):
                return False
            index += 1
            continue
        if arg.startswith("--bytes="):
            if not _bounded_count(arg.split("=", 1)[1], maximum=_PIPELINE_MAX_BYTES):
                return False
            index += 1
            continue
        return False
    return True


def _has_bounded_pipeline(command_str: str) -> bool:
    """Return whether one shell pipeline ends in a genuinely bounded consumer.

    Tokenizing avoids treating a quoted search pattern such as ``"foo | head"`` as
    an output cap. Sibling commands, pass-through pagers, follow/from-start tail modes,
    huge limits, and stages after the cap all fail closed.
    """
    tokens = _shell_tokens(command_str)
    if not tokens or any(token in _HATCH_CONTROL_TOKENS for token in tokens):
        return False
    pipe_indexes = [index for index, token in enumerate(tokens) if token == "|"]
    if not pipe_indexes:
        return False
    final_stage = tokens[pipe_indexes[-1] + 1 :]
    if not final_stage:
        return False
    consumer = _strip_path(final_stage[0])
    return consumer in _BOUNDED_PIPELINE_BINARIES and _bounded_consumer_args(
        consumer,
        final_stage[1:],
    )


def _has_stdout_null_sink(command_str: str) -> bool:
    tokens = _shell_tokens(command_str)
    if not tokens or any(token in _HATCH_CONTROL_TOKENS for token in tokens):
        return False
    if len(tokens) < 2 or tokens[-1] != "/dev/null" or tokens[-2] not in {">", "&>"}:
        return False
    return len(tokens) < 3 or tokens[-3] != "2"


def _has_hatch(command_str: str) -> bool:
    return _has_bounded_pipeline(command_str) or _has_stdout_null_sink(command_str)


def _has_user_rule_hatch(command_str: str) -> bool:
    return _has_hatch(command_str)


def _has_compound(command_str: str) -> bool:
    try:
        lexer = shlex.shlex(command_str, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return any(token in _COMPOUND_SEPARATORS for token in lexer)
    except (TypeError, ValueError):
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


def _looks_like_file_target(value: str) -> bool:
    if value.endswith("/"):
        return False
    value = value.rstrip("/")
    if not value or value in {".", ".."} or any(char in value for char in "*?[]{}"):
        return False
    basename = value.rsplit("/", 1)[-1]
    if basename in _FILELIKE_BASENAMES:
        return True
    separator, _, suffix = basename.rpartition(".")
    return bool(separator) and f".{suffix.casefold()}" in _FILELIKE_SUFFIXES


def _rg_has_explicit_file_target(tokens: list[str]) -> bool:
    """Recognize the narrow ``rg PATTERN FILE`` form without filesystem I/O."""
    pattern_seen = False
    targets: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            remainder = tokens[index + 1 :]
            if not pattern_seen and remainder:
                pattern_seen = True
                remainder = remainder[1:]
            targets.extend(remainder)
            break
        option_name = token.split("=", 1)[0]
        if option_name in _RG_VALUE_OPTIONS:
            if option_name in _RG_PATTERN_OPTIONS:
                pattern_seen = True
            if "=" not in token:
                index += 1
            index += 1
            continue
        if token in _RG_FLAG_OPTIONS:
            index += 1
            continue
        if (
            token.startswith("-")
            and not token.startswith("--")
            and len(token) > 2
            and all(flag in _RG_COMBINABLE_SHORT_FLAGS for flag in token[1:])
        ):
            index += 1
            continue
        # Unknown options fail closed. Otherwise an option that consumes a value
        # (for example ``--threads 4``) can shift the positional parse and make a
        # broad ``rg PATTERN`` invocation look like ``rg PATTERN FILE``.
        if token.startswith("-"):
            return False
        if not pattern_seen:
            pattern_seen = True
        else:
            targets.append(token)
        index += 1
    return pattern_seen and bool(targets) and all(_looks_like_file_target(target) for target in targets)


def _nested_search_decision(command_str: str) -> dict[str, Any] | None:
    """Fail closed when a broad search starts inside shell control syntax.

    The top-level parser already handles ordinary commands and pipelines. This
    bounded token scan covers command groups/substitutions such as ``(rg ...)``,
    ``$(rg ...)``, backticks, braces, and shell condition bodies without trying
    to become a complete shell parser. Quoted prose remains one non-binary token.
    """
    tokens = _shell_tokens(command_str)
    if not tokens:
        return None
    for index, token in enumerate(tokens):
        if index == 0 or tokens[index - 1] not in _NESTED_COMMAND_BOUNDARIES:
            continue
        binary = _strip_path(token)
        is_git_grep = binary == "git" and index + 1 < len(tokens) and tokens[index + 1] == "grep"
        if binary not in LONG_OUTPUT_BINARIES and binary not in _SHELL_WRAPPERS and not is_git_grep:
            continue
        end = index + 1
        while end < len(tokens) and tokens[end] not in _NESTED_COMMAND_END:
            end += 1
        segment = shlex.join(tokens[index:end])
        if not segment or segment == command_str:
            continue
        decision = should_intercept(segment)
        if decision.get("intercept"):
            return {
                "intercept": True,
                "binary": decision.get("binary"),
                "reason": f"nested_shell:{decision.get('reason')}",
                "suggested_command": _build_suggested(command_str),
            }
    return None


def _build_suggested(command_str: str) -> str:
    # Preserve pipes/compound commands as one shell payload. Without this wrapper the
    # caller's shell consumes ``&&``/``|`` before Code Brain starts, and PreToolUse may
    # intercept the suggested command again.
    return f".ai/bin/ai exec run -- bash -lc {shlex.quote(command_str)}"


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

    newline_segments = _split_unquoted_newline_segments(command_str)
    if newline_segments:
        for segment in newline_segments:
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

    nested = _nested_search_decision(command_str)
    if nested is not None:
        return nested

    # 1. Hatch check (highest priority): an explicit downstream cap/null sink keeps
    # model-visible output bounded. Modern tool hosts also enforce their own output
    # caps, so routing an already-bounded native search only adds latency.
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
        if _rg_has_explicit_file_target(tokens):
            return {
                "intercept": False,
                "binary": None,
                "reason": "rg_explicit_file_target",
                "suggested_command": None,
            }
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
      3. Built-in broad-search decision → block, or allow when output/file scope is bounded.
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
