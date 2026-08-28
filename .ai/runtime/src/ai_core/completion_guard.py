"""Hard premature-stop guard: refuse a turn end that the WORKING TREE says is unfinished.

Why this module exists even though `loop_continuation` already blocks Stop: that driver is
gated entirely on `plan_state.active_summary(root)` returning a plan with unchecked steps.
Measured across the 11 installed projects, essentially nobody keeps such a plan — blurivo 1
of 140 plans, navio 0 of 32, actraflow 0 of 1, fluxwright 0, vera-harness 0. So the guard's
trigger condition is almost never satisfied and the model's premature "done" sails through.

The fix is NOT to trust a different self-report. It is to read signals the model cannot
assert its way past, in the spirit of Evidence-Carrying Termination (arXiv:2608.23623, 22
Aug 2026: an agent may return COMPLETE only when a certificate binds each claim to trace
evidence; ECT drove premature unsupported terminations from 40/66 to 0/66). CB's cheap,
offline analogue: derive completion from the tree and the durable stores, never from prose.

SIGNAL PRECEDENCE (first hit wins, so the reason names one concrete next action)
  1. plan          — an active plan has unchecked steps (delegated to plan_state)
  2. conflict      — an unresolved merge conflict marker sits in a tracked file
  3. syntax        — a file the turn touched no longer parses
  4. marker        — a TODO/FIXME/XXX/HACK marker the turn ITSELF introduced
  5. verification  — the turn mutated files but has not run a relevant successful check since
  6. acceptance    — a recorded acceptance command last exited non-zero

Deliberately NOT signals: a dirty worktree (normal mid-work state and blurivo idles at 913
modified files), an uncommitted change (committing is the user's call), open memory todos
(they are a deferred-work backlog by definition — blocking on them would never release).

SAFETY. Blocking a Stop is a loop, so every rail is on the yield side:
  * ON by default (`AI_COMPLETION_GUARD=0` disables). See `_enabled` for why opt-in was the
    root cause of the old guard being dead in every installed project.
  * Never overrides a security block; the caller only consults this when decision != block.
  * Claude's `stop_hook_active` is diagnostic, not a bypass. We process the evidence
    fingerprint instead: unchanged evidence yields after two nudges, changed evidence may
    continue, and Claude itself enforces an eight-consecutive-continuation ceiling.
  * Context pressure → yield (never fight a pending compaction).
  * Antigravity system/error/max-step stops and non-idle stops → yield. A normal
    `model_stop` is continuable: Antigravity 2.0's Stop contract maps CB's internal
    `decision:block` to the host's inverted `decision:"continue"` wire value.
  * Bounded: per-session count + wall clock, shared with loop_continuation.
  * NO-PROGRESS ESCALATION: if the same signal fingerprint repeats with an unchanged tree
    for `MAX_STALL_REPEATS` blocks, yield. Re-prompting a stuck model is a token fire.
  * A host-authenticated `user_input_required` / `awaiting_user` / `approval_required`
    signal → yield. Punctuation is not evidence: yielding on a trailing `?` let a model
    bypass the guard merely by asking an unnecessary question.

stdlib + git only. No LLM, no network. Fail-soft: any error yields (turn ends).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .private_write import atomic_write_private_text, private_file_lock

# Repeats of the SAME fingerprint against an UNCHANGED tree before the guard gives up. Two
# strikes is deliberate: one re-prompt is a nudge, a third would be nagging a stuck model.
MAX_STALL_REPEATS = 2
# Cap on files inspected per signal so a huge turn cannot blow the Stop budget.
MAX_FILES_SCANNED = 40
MAX_FILE_BYTES = 512_000
MAX_BASELINE_SIGNAL_KEYS = 256
MAX_ACTIVITY_PATHS = 32
MAX_ACTIVITY_HASH_BYTES = 2_000_000
REQUEST_EVIDENCE_TTL_SECONDS = 1800
_GIT_TIMEOUT = 5
STATE_PARTS = (".ai", "cache", "completion_guard.json")

# Conflict markers must be anchored at line start with the exact 7-char run git writes;
# a loose search matches this module's own docstring and every diff-handling source file.
_CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")
# Action-form markers only. A bare word inside prose (for example documentation explaining
# "TODO/FIXME/XXX/HACK") is not unfinished work. Common code comments, line-leading markers,
# and Markdown list items remain covered.
_MARKER_RE = re.compile(
    r"(?:^\s*|#\s*|//\s*|/\*\s*|<!--\s*|--\s*|[-*]\s+)"
    r"(TODO|FIXME|XXX|HACK)\b(?=\s|:|\()"
)
# Suffixes worth parsing for a syntax check. Only Python is checked: it is the one language
# whose parser ships in the stdlib, and a wrong "broken syntax" claim is worse than none.
_PY_SUFFIXES = (".py",)
_TEXT_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".dart", ".kt", ".kts", ".swift", ".java",
    ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".scala", ".sh",
    ".bash", ".zsh", ".sql", ".yaml", ".yml", ".toml", ".json", ".md", ".txt", ".gradle",
    ".m", ".mm", ".vue", ".svelte", ".tf", ".proto", ".graphql", ".css", ".scss", ".html",
)
# Code Brain writes .ai/ on every hook; attributing that to the user's turn would make the
# guard fire on its own bookkeeping. Same exclusion turn_report needs, same reason.
_EXCLUDE_PATHSPEC = (".", ":(exclude).ai")

_ANTIGRAVITY_PAYLOAD_KEYS = frozenset(
    {"conversationId", "workspacePaths", "terminationReason", "fullyIdle"}
)

_MUTATION_TOOLS = frozenset(
    {
        "apply_patch", "edit", "write", "multiedit", "notebookedit",
        "replace_file_content", "multi_replace_file_content", "write_to_file",
    }
)
_SHELL_TOOLS = frozenset(
    {"bash", "shell", "exec_command", "functions.exec_command", "run_command"}
)
_CODE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".dart", ".kt", ".kts",
        ".swift", ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc",
        ".cpp", ".hpp", ".cs", ".scala", ".sh", ".bash", ".zsh", ".sql",
        ".vue", ".svelte", ".tf", ".proto", ".graphql",
    }
)
_SHELL_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:"
    r"sed\s+-[^\n;&|]*\bi|perl\s+-[^\n;&|]*\bpi|tee(?:\s|$)|touch(?:\s|$)|"
    r"mkdir(?:\s|$)|rm(?:\s|$)|mv(?:\s|$)|cp(?:\s|$)|truncate(?:\s|$)|"
    r"git\s+(?:apply|checkout|restore|reset|clean|mv|rm)(?:\s|$)|"
    r"(?:dart\s+format|gofmt\s+-w|rustfmt)(?:\s|$)|"
    r"(?:cat|printf|echo)\b[^\n;&|]*(?:>>?|\|\s*tee)|"
    r"python(?:3(?:\.\d+)?)?\b[^\n;&|]*(?:write_text|write_bytes)"
    r")",
    re.IGNORECASE,
)
_PATCH_PATH_RE = re.compile(
    r"^(?:\*{3} (?:Update|Add|Delete) File:|\+\+\+ b/|--- a/)\s*(.+?)\s*$",
    re.MULTILINE,
)

# Verification strength: 1 = repository/text sanity, 2 = static/build/doctor, 3 = tests.
# Patterns are anchored to a shell segment's executable so `echo pytest` cannot forge proof.
_VERIFY_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (3, re.compile(r"^(?:\S*/)?(?:python(?:3(?:\.\d+)?)?)\s+-m\s+(?:pytest|unittest)\b", re.I)),
    (3, re.compile(r"^(?:\S*/)?(?:pytest|py\.test)\b", re.I)),
    (3, re.compile(r"^(?:uv|poetry|pipenv)\s+run\b.*\b(?:pytest|unittest)\b", re.I)),
    (3, re.compile(r"^(?:go\s+test|cargo\s+test|swift\s+test|dotnet\s+test|xcodebuild\b.*\btest\b)", re.I)),
    (3, re.compile(r"^(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|(?:\./)?gradlew\b.*\btest\b|mvn\b.*\b(?:test|verify)\b)", re.I)),
    (3, re.compile(r"^(?:make|gmake)\b.*\btest\b", re.I)),
    (2, re.compile(r"^(?:make|gmake)\b.*\b(?:lint|check|doctor|eval|build)\b", re.I)),
    (2, re.compile(r"^(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|check|typecheck|build)\b|cargo\s+(?:check|clippy)\b)", re.I)),
    (3, re.compile(r"^(?:dart|flutter)\s+test\b", re.I)),
    (2, re.compile(r"^(?:dart|flutter)\s+analyze\b|^(?:dotnet|swift)\s+build\b", re.I)),
    (2, re.compile(r"^(?:\S*/)?(?:ruff|mypy|pyright|eslint|tsc|shellcheck)\b", re.I)),
    (2, re.compile(r"^(?:\S*/)?(?:python(?:3(?:\.\d+)?)?)\s+-m\s+(?:compileall|mypy|ruff)\b", re.I)),
    (2, re.compile(r"^(?:bash|zsh|sh)\s+-n\b|^(?:bash\s+)?(?:\./)?scripts/docs-check\.sh\b", re.I)),
    (2, re.compile(r"^(?:\S*/)?(?:ai|ai\.ps1)\s+(?:doctor|context\s+prove)\b|^\.ai/bin/ai\s+(?:doctor|context\s+prove)\b", re.I)),
    (1, re.compile(r"^git(?:\s+-C\s+\S+)?\s+diff\b.*--check\b", re.I)),
)

# Explicit proof policy. A verification command is accepted only when its allowlisted strength
# reaches the edited scope's threshold, the host says that exact call succeeded, and the request
# ledger binds it to the current content of every observed edit target.
_PROOF_POLICY = {
    "docs": {"minimum_level": 1, "description": "repository/docs sanity"},
    "code": {"minimum_level": 2, "description": "static, build, doctor, eval, or test"},
}


def _env_on(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() not in ("", "0", "false", "no")


def _enabled() -> bool:
    """ON by default; `AI_COMPLETION_GUARD=0` is the kill switch.

    Deliberately NOT opt-in, unlike `loop_continuation`. That flag-gated design is exactly
    why premature stops were never caught: `AI_LOOP_CONTINUATION=1` lived only in the source
    kit's `.claude/settings.json`, the installer merged `hooks` but never `env`, and consumer
    settings therefore had the Stop hook registered with `env` absent. Verified on blurivo and
    navio: `env` was `None` in both. A guard whose activation depends on env plumbing across
    three different host config formats (`.claude/settings.json`, `.codex/hooks.json`,
    `.agents/hooks.json`) will be dead in most installs, and it was.

    Default-on is safe because the evidence fingerprint gives up after MAX_STALL_REPEATS
    without progress, the shared per-request counter has a hard cap, and Claude independently
    caps consecutive Stop continuations at eight. Unlike the former `stop_hook_active` bypass,
    real progress can therefore continue past the first nudge without permitting a loop.
    """
    return str(os.environ.get("AI_COMPLETION_GUARD", "1")).strip().lower() not in ("0", "false", "no")


def _int_env(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def state_path(root: Path) -> Path:
    return Path(root).joinpath(*STATE_PARTS)


def _git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    return True, proc.stdout


def _worktree_status(root: Path) -> tuple[bool, list[str], list[str], list[str], bool]:
    """Return (ok, changed, unmerged, untracked, overflow) from one process.

    The previous implementation spawned three processes for changed paths and a fourth for
    conflicts. `-z` makes paths with spaces/newlines unambiguous; rename/copy records carry a
    second NUL-delimited source path which is skipped because the first path is the worktree
    destination. Unmerged files are prioritized before applying the scan bound.
    """
    ok, out = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
    )
    if not ok:
        return False, [], [], [], False
    entries = out.split("\0")
    changed: list[str] = []
    unmerged: list[str] = []
    untracked: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        rel = entry[3:]
        if "R" in status or "C" in status:
            i += 1  # -z rename/copy source path; destination is `rel` above.
        if not rel or rel == ".ai" or rel.startswith(".ai/") or rel in seen:
            continue
        seen.add(rel)
        if status in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            unmerged.append(rel)
        if status == "??":
            untracked.append(rel)
        changed.append(rel)
    # A conflict is higher-value evidence than an arbitrary dirty path. Keep deterministic
    # git ordering within each class while ensuring a huge dirty tree cannot hide conflicts.
    unmerged_set = set(unmerged)
    ordered = unmerged + [rel for rel in changed if rel not in unmerged_set]
    bounded = ordered[:MAX_FILES_SCANNED]
    bounded_set = set(bounded)
    return (
        True,
        bounded,
        [rel for rel in unmerged if rel in bounded_set],
        [rel for rel in untracked if rel in bounded_set],
        len(ordered) > MAX_FILES_SCANNED,
    )


def touched_files(root: Path) -> list[str]:
    """Paths changed vs HEAD (staged/unstaged/untracked), `.ai/` excluded and bounded."""
    _ok, files, _unmerged, _untracked, _overflow = _worktree_status(root)
    return files


def _read_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _added_lines_by_path(
    root: Path,
    files: list[str],
    untracked: list[str] | None = None,
) -> dict[str, list[str]]:
    """Map path → lines this turn ADDED, so a pre-existing TODO is never blamed on this turn.

    ONE path-scoped `git diff` for at most MAX_FILES_SCANNED candidates, not one per file and
    never the whole dirty repository. Untracked files have no diff entry, so only their whole
    content counts as added. A tracked deletion-only file is deliberately absent instead of
    being mistaken for a new file (the old fallback caused pre-existing TODO false positives).
    """
    out_map: dict[str, list[str]] = {}
    untracked_set = set(untracked or ())
    tracked = [rel for rel in files if rel not in untracked_set]
    if tracked:
        ok, out = _git(
            root,
            "-c", "core.quotePath=false",
            "diff", "--no-ext-diff", "--no-textconv", "--unified=0",
            "--src-prefix=a/", "--dst-prefix=b/", "HEAD", "--", *tracked,
        )
    else:
        ok, out = True, ""
    if ok:
        current = ""
        for line in out.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:].strip()
                continue
            if line.startswith("+++ ") or line.startswith("--- "):
                continue
            if current and line.startswith("+"):
                out_map.setdefault(current, []).append(line[1:])
    wanted = set(files)
    for rel in files:
        if rel not in untracked_set:
            continue
        text = _read_text(Path(root) / rel)
        if text is not None:
            out_map[rel] = text.splitlines()
    return {k: v for k, v in out_map.items() if k in wanted}


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _signal_key(kind: str, rel: str, value: str) -> str:
    return _digest_text(f"{kind}\0{rel}\0{value}")


def _signal_conflict(
    root: Path,
    files: list[str],
    unmerged: list[str],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """An unresolved conflict marker in a file this turn touched.

    Two sources, cheapest first. `git diff --diff-filter=U` is authoritative for a live
    merge/rebase. But markers also survive a `git add` of a half-resolved file, which clears
    the U flag, so touched files are scanned too. NO extension whitelist here: git writes
    markers into whatever text file it was merging, and an unresolved conflict is never a
    false positive worth suppressing. `_read_text` already skips binaries (it decodes
    strictly) and oversized files.
    """
    baseline = baseline if isinstance(baseline, dict) else {}
    baseline_keys = {str(v) for v in baseline.get("conflicts", []) if isinstance(v, str)}
    baseline_unmerged = baseline.get("unmerged")
    baseline_unmerged = baseline_unmerged if isinstance(baseline_unmerged, dict) else {}
    candidates: list[str] = []
    seen: set[str] = set()
    for rel in list(unmerged) + list(files):
        if rel not in seen:
            seen.add(rel)
            candidates.append(rel)
    for rel in candidates[:MAX_FILES_SCANNED]:
        text = _read_text(Path(root) / rel)
        if text is not None:
            for i, line in enumerate(text.splitlines(), start=1):
                if not _CONFLICT_RE.match(line):
                    continue
                if _signal_key("conflict", rel, line.strip()) in baseline_keys:
                    continue
                return {"kind": "conflict", "path": rel, "detail": f"{rel}:{i}",
                        "action": f"resolve the merge conflict at {rel}:{i}"}
        if rel in unmerged:
            current = _digest_text(text) if text is not None else "unreadable"
            if str(baseline_unmerged.get(rel) or "") == current:
                continue
            return {"kind": "conflict", "path": rel, "detail": rel,
                    "action": f"resolve the unmerged index entry at {rel}"}
    return None


def _signal_syntax(
    root: Path,
    files: list[str],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    baseline = baseline if isinstance(baseline, dict) else {}
    baseline_syntax = baseline.get("syntax")
    baseline_syntax = baseline_syntax if isinstance(baseline_syntax, dict) else {}
    for rel in files:
        if not rel.endswith(_PY_SUFFIXES):
            continue
        text = _read_text(Path(root) / rel)
        if text is None:
            continue
        try:
            ast.parse(text)
        except SyntaxError as exc:
            if str(baseline_syntax.get(rel) or "") == _digest_text(text):
                continue
            line = exc.lineno or 0
            return {"kind": "syntax", "path": rel, "detail": f"{rel}:{line}",
                    "action": f"fix the syntax error you introduced at {rel}:{line}"}
        except (ValueError, RecursionError, MemoryError):
            continue
    return None


def _signal_marker(
    root: Path,
    files: list[str],
    untracked: list[str] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A TODO/FIXME the TURN introduced. Pre-existing markers are the repo's backlog, not this
    turn's unfinished work, so only added lines are considered."""
    candidates = [rel for rel in files if rel.endswith(_TEXT_SUFFIXES)]
    if not candidates:
        return None
    baseline = baseline if isinstance(baseline, dict) else {}
    baseline_keys = {str(v) for v in baseline.get("markers", []) if isinstance(v, str)}
    added = _added_lines_by_path(root, candidates, untracked)
    for rel in candidates:  # iterate `files` order so the result is deterministic
        for line in added.get(rel, ()):
            m = _MARKER_RE.search(line)
            if m:
                if _signal_key("marker", rel, f"{m.group(1)}\0{line.strip()}") in baseline_keys:
                    continue
                return {"kind": "marker", "path": rel, "detail": f"{rel} +{m.group(1)}",
                        "action": f"finish or remove the {m.group(1)} you just added in {rel}"}
    return None


def _session_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("session_id")
        or payload.get("sid")
        or payload.get("conversationId")
        or "default"
    )


def _tool_name(payload: dict[str, Any]) -> str:
    raw = payload.get("tool_name") or payload.get("tool") or ""
    call = payload.get("toolCall")
    if not raw and isinstance(call, dict):
        raw = call.get("name") or call.get("toolName") or ""
    return str(raw).strip().lower().replace("-", "_")


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "input"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    call = payload.get("toolCall")
    if isinstance(call, dict):
        for key in ("args", "arguments", "input"):
            value = call.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _tool_command(payload: dict[str, Any]) -> str:
    inputs = _tool_input(payload)
    return str(
        inputs.get("command")
        or inputs.get("CommandLine")
        or inputs.get("commandLine")
        or ""
    )


def _candidate_paths(payload: dict[str, Any], command: str = "") -> list[str]:
    inputs = _tool_input(payload)
    values: list[str] = []
    for key in ("file_path", "path", "notebook_path", "target_file", "filename"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    for key in ("patch", "diff", "content"):
        value = inputs.get(key)
        if isinstance(value, str) and ("*** " in value or "+++ b/" in value):
            values.extend(match.group(1).strip() for match in _PATCH_PATH_RE.finditer(value))
    if command:
        for match in re.finditer(r"(?:>>?|\btee(?:\s+-a)?)\s+([^\s;&|]+)", command):
            target = match.group(1).strip("'\"")
            if target and target not in {"/dev/null", "NUL"}:
                values.append(target)
    return values[:32]


def _tool_call_id(payload: dict[str, Any]) -> str:
    """Host-issued call identity, normalized across Claude/Codex and Antigravity.

    Claude exposes ``tool_use_id``. Antigravity does not expose a UUID, but its documented
    ``conversationId`` + ``stepIdx`` pair uniquely identifies a completed tool step.
    """
    for key in ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId", "call_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)[:160]
    call = payload.get("toolCall")
    if isinstance(call, dict):
        for key in ("id", "tool_use_id", "toolUseId", "callId"):
            value = call.get(key)
            if value not in (None, ""):
                return str(value)[:160]
    if payload.get("stepIdx") is not None and payload.get("conversationId"):
        return f"{str(payload['conversationId'])[:96]}:step:{str(payload['stepIdx'])[:24]}"
    return ""


def _tool_input_digest(payload: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            _tool_input(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _normalized_paths(root: Path, raw_paths: list[str]) -> tuple[list[str], bool]:
    root = Path(root).resolve()
    normalized: set[str] = set()
    complete = True
    for raw in raw_paths:
        try:
            candidate = Path(str(raw).strip().strip("'\""))
            absolute = candidate if candidate.is_absolute() else root / candidate
            rel = absolute.resolve(strict=False).relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            complete = False
            continue
        if not rel or rel == ".":
            complete = False
            continue
        normalized.add(rel)
        if len(normalized) > MAX_ACTIVITY_PATHS:
            complete = False
            break
    return sorted(normalized)[:MAX_ACTIVITY_PATHS], complete


def _content_binding(root: Path, paths: list[str]) -> tuple[str, bool]:
    """Hash current edit-target contents without git/network; fail-open when not fully bindable."""
    if not paths:
        return "", False
    root = Path(root).resolve()
    total = 0
    digest = hashlib.sha256()
    for rel in sorted(set(paths)):
        try:
            path = (root / rel).resolve(strict=False)
            path.relative_to(root)
            digest.update(rel.encode("utf-8", errors="surrogateescape") + b"\0")
            if not path.exists():
                digest.update(b"missing\0")
                continue
            if path.is_symlink() or not path.is_file():
                return "", False
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_ACTIVITY_HASH_BYTES:
                return "", False
            data = path.read_bytes()
        except (OSError, RuntimeError, ValueError):
            return "", False
        digest.update(str(len(data)).encode("ascii") + b":" + hashlib.sha256(data).digest())
    return digest.hexdigest()[:32], True


def _tool_exit_code(payload: dict[str, Any], *, event_succeeded: bool) -> tuple[int | None, str]:
    """Return a host-backed exit code and provenance, never infer from model-authored prose."""
    if not event_succeeded or str(payload.get("error") or "").strip():
        return 1, "hook-failure-event"
    candidates: list[Any] = [
        payload.get("tool_response"), payload.get("tool_output"), payload.get("toolResult"),
        payload.get("result"),
    ]
    call = payload.get("toolCall")
    if isinstance(call, dict):
        candidates.extend([call.get("result"), call.get("output")])
    for value in candidates:
        if not isinstance(value, dict):
            continue
        for key in ("exit_code", "exitCode", "returncode", "code"):
            if key in value:
                try:
                    return int(value[key]), f"result.{key}"
                except (TypeError, ValueError):
                    return None, "invalid-explicit-exit"
    # Both documented hosts fire PostToolUse only after completion/success. Antigravity exposes
    # no numeric exit field, so an empty `error` is its authoritative zero-exit contract.
    return 0, "successful-post-tool-event"


def _scope_for_paths(paths: list[str]) -> str:
    if not paths:
        return "code"  # unknown mutator: require the stronger proof, never silently under-check
    for raw in paths:
        suffix = Path(raw).suffix.lower()
        if suffix in _CODE_SUFFIXES or not suffix:
            return "code"
    return "docs"


def _mutation_scope(payload: dict[str, Any]) -> str:
    name = _tool_name(payload)
    short = name.rsplit(".", 1)[-1]
    command = _tool_command(payload)
    if short in _MUTATION_TOOLS:
        return _scope_for_paths(_candidate_paths(payload, command))
    if name in _SHELL_TOOLS or short in _SHELL_TOOLS:
        if _SHELL_MUTATION_RE.search(command):
            return _scope_for_paths(_candidate_paths(payload, command))
    return ""


def _strip_shell_prefix(segment: str) -> str:
    text = segment.strip().lstrip("(").strip()
    # Common harmless wrappers and environment assignments. Keep this intentionally narrow:
    # proof recognition must fail closed rather than accepting prose that mentions a test.
    text = re.sub(r"^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S+)\s+)+", "", text)
    text = re.sub(r"^(?:time\s+|command\s+|timeout\s+\S+\s+)", "", text)
    return text.strip()


def _verification_level(payload: dict[str, Any]) -> int:
    name = _tool_name(payload)
    command = _tool_command(payload)
    if name not in _SHELL_TOOLS and name.rsplit(".", 1)[-1] not in _SHELL_TOOLS:
        return 0
    if _tool_input(payload).get("run_in_background") is True:
        return 0
    level = 0
    for raw_segment in re.split(r"&&|\|\||;|\n", command):
        segment = _strip_shell_prefix(raw_segment)
        for strength, pattern in _VERIFY_PATTERNS:
            if pattern.search(segment):
                level = max(level, strength)
    return level


def _tool_succeeded(payload: dict[str, Any], *, event_succeeded: bool) -> bool:
    exit_code, _exit_source = _tool_exit_code(payload, event_succeeded=event_succeeded)
    if exit_code != 0:
        return False
    candidates: list[Any] = [
        payload.get("tool_response"), payload.get("tool_output"), payload.get("toolResult"),
        payload.get("result"),
    ]
    call = payload.get("toolCall")
    if isinstance(call, dict):
        candidates.extend([call.get("result"), call.get("output")])
    for value in candidates:
        if isinstance(value, dict):
            if value.get("isError") is True or value.get("success") is False:
                return False
            for key in ("exit_code", "exitCode", "returncode", "code"):
                if key in value:
                    try:
                        if int(value[key]) != 0:
                            return False
                    except (TypeError, ValueError):
                        pass
            status = str(value.get("status") or "").strip().lower()
            if status in {
                "failed", "failure", "error", "cancelled", "canceled", "running", "pending",
                "timeout", "timed_out", "timed-out",
            }:
                return False
        elif isinstance(value, str) and re.search(
            r"(?:exit(?:ed)?(?:\s+with)?(?:\s+code)?|exit_code)\s*[:=]?\s*[1-9]\d*\b",
            value,
            re.IGNORECASE,
        ):
            return False
    return True


def observe_tool_event(
    root: Path,
    payload: dict[str, Any],
    *,
    event_succeeded: bool = True,
) -> bool:
    """Record request-scoped mutation/verification order from a post-tool hook.

    This is deliberately metadata-only: no git, no parser and no network on PostToolUse. A
    successful relevant check must occur after the latest mutation; otherwise Stop gets one
    bounded continuation directive. Unknown mutators require static/build/test proof.
    """
    if not isinstance(payload, dict):
        return False
    command = _tool_command(payload)
    scope = _mutation_scope(payload)
    verification = _verification_level(payload)
    success = _tool_succeeded(payload, event_succeeded=event_succeeded)
    if not scope and not verification:
        return False
    sid = _session_id(payload)
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            activities = state.get("activities")
            activities = activities if isinstance(activities, dict) else {}
            row = activities.get(sid) if isinstance(activities.get(sid), dict) else {}
            seq = int(row.get("seq") or 0) + 1
            row = dict(row)
            row.update({"seq": seq, "ts": time.time()})
            name = _tool_name(payload)[:80]
            call_id = _tool_call_id(payload)
            input_digest = _tool_input_digest(payload)
            if scope:
                event_paths, paths_complete = _normalized_paths(
                    Path(root), _candidate_paths(payload, command)
                )
                prior_paths = [
                    str(value) for value in row.get("mutation_paths", []) if isinstance(value, str)
                ]
                merged_paths = sorted(set(prior_paths).union(event_paths))
                if len(merged_paths) > MAX_ACTIVITY_PATHS:
                    paths_complete = False
                    merged_paths = merged_paths[:MAX_ACTIVITY_PATHS]
                binding, binding_complete = _content_binding(Path(root), merged_paths)
                ledger_complete = bool(row.get("ledger_complete", True))
                ledger_complete = bool(
                    ledger_complete and call_id and input_digest and paths_complete and binding_complete
                )
                row.update(
                    {
                        "mutation_seq": seq,
                        "mutation_scope": scope,
                        "mutation_tool": name or "unknown",
                        "mutation_call_id": call_id,
                        "mutation_input_digest": input_digest,
                        "mutation_paths": merged_paths,
                        "mutation_binding": binding if binding_complete else "",
                        "ledger_complete": ledger_complete,
                    }
                )
            if verification and success:
                exit_code, exit_source = _tool_exit_code(
                    payload, event_succeeded=event_succeeded
                )
                mutation_paths = [
                    str(value) for value in row.get("mutation_paths", []) if isinstance(value, str)
                ]
                current_binding, current_complete = _content_binding(Path(root), mutation_paths)
                if (
                    call_id
                    and input_digest
                    and exit_code == 0
                    and bool(row.get("ledger_complete"))
                    and current_complete
                    and current_binding == str(row.get("mutation_binding") or "")
                ):
                    row.update(
                        {
                            "verification_seq": seq,
                            "verification_level": verification,
                            "verification_tool": name or "shell",
                            "verification_call_id": call_id,
                            "verification_command_digest": input_digest,
                            "verification_exit_code": exit_code,
                            "verification_exit_source": exit_source,
                            "verification_mutation_binding": current_binding,
                        }
                    )
            activities[sid] = row
            if len(activities) > 32:
                for key in sorted(
                    activities,
                    key=lambda k: float((activities[k] or {}).get("ts") or 0.0),
                )[:-32]:
                    activities.pop(key, None)
            state["activities"] = activities
            return _write_state(root, state)
    except (OSError, TypeError, ValueError):
        return False


def _signal_verification(
    root: Path,
    sid: str,
    diagnostics: list[str] | None = None,
) -> dict[str, Any] | None:
    state = _read_state(root)
    activities = state.get("activities")
    if not isinstance(activities, dict):
        return None
    row = activities.get(str(sid or "default"))
    if not isinstance(row, dict):
        return None
    if time.time() - float(row.get("ts") or 0.0) > REQUEST_EVIDENCE_TTL_SECONDS:
        if diagnostics is not None:
            diagnostics.append("verification ledger TTL expired")
        return None
    mutation_seq = int(row.get("mutation_seq") or 0)
    if mutation_seq <= 0:
        return None
    # A partial/unbindable ledger is not trustworthy enough to block a Stop. Other independent
    # tree signals may still fire, but verification evidence itself always fails open.
    if row.get("ledger_complete") is not True:
        if diagnostics is not None:
            diagnostics.append("verification ledger could not bind call/path/content evidence")
        return None
    verification_seq = int(row.get("verification_seq") or 0)
    scope = str(row.get("mutation_scope") or "code")
    policy = _PROOF_POLICY.get(scope)
    if not isinstance(policy, dict):
        return None
    required = int(policy["minimum_level"])
    level = int(row.get("verification_level") or 0) if verification_seq >= mutation_seq else 0
    paths = [str(value) for value in row.get("mutation_paths", []) if isinstance(value, str)]
    current_binding, current_complete = _content_binding(Path(root), paths)
    if not current_complete:
        if diagnostics is not None:
            diagnostics.append("verification content scope could not be read completely")
        return None
    mutation_binding = str(row.get("mutation_binding") or "")
    verified_binding = str(row.get("verification_mutation_binding") or "")
    proof_bound = bool(
        current_complete
        and mutation_binding
        and current_binding == mutation_binding == verified_binding
        and str(row.get("mutation_call_id") or "")
        and str(row.get("mutation_input_digest") or "")
        and str(row.get("verification_call_id") or "")
        and str(row.get("verification_command_digest") or "")
        and int(row.get("verification_exit_code") or 0) == 0
    )
    if verification_seq >= mutation_seq and level >= required and proof_bound:
        return None
    tool = str(row.get("mutation_tool") or "edit")
    action = (
        "run the closest relevant test, lint, build, or doctor check after the last edit"
        if required >= 2
        else "run a docs/config check or at least `git diff --check` after the last edit"
    )
    return {
        "kind": "verification",
        "path": "",
        "detail": f"{tool} seq={mutation_seq}",
        "mutation_seq": mutation_seq,
        "required_level": required,
        "action": action,
    }


def _signal_acceptance(
    root: Path,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Last recorded acceptance batch failed → machine verification is still outstanding.

    Reads the eval ledger `acceptance.run_acceptance` writes via `eval_loop.record_case`.
    Uses `eval_loop._iter_cases` + `_is_pass` rather than inventing a helper, so the
    pass/fail predicate stays the same one the eval subsystem uses.
    """
    try:
        from . import eval_loop

        if isinstance(baseline, dict) and "acceptance_offset" in baseline:
            path = root.joinpath(*eval_loop.CASES_PATH)
            offset = max(0, int(baseline.get("acceptance_offset") or 0))
            try:
                size = path.stat().st_size
            except OSError:
                return None
            if size < offset or size - offset > MAX_FILE_BYTES:
                return None
            with path.open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                return None
            cases = []
            for line in raw.decode("utf-8", errors="strict").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict) and str(row.get("kind")) == "acceptance":
                    cases.append(row)
        else:
            cases = [c for c in eval_loop._iter_cases(root)
                     if isinstance(c, dict) and str(c.get("kind")) == "acceptance"]
    except Exception:
        return None
    if not cases:
        return None
    row = cases[-1]  # append-only ledger, so the last acceptance row is the current verdict
    try:
        if eval_loop._is_pass(str(row.get("outcome", ""))):
            return None
    except Exception:
        return None
    return {"kind": "acceptance", "path": "", "detail": str(row.get("command") or "acceptance"),
            "action": "re-run the acceptance commands until they pass"}


def _signal_plan(
    root: Path,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        from . import plan_state
        active = plan_state.active_summary(root)
    except Exception:
        return None
    if not active or int(active.get("remaining") or 0) <= 0:
        return None
    nxt = active.get("next_label") or "the next unchecked step"
    signal = {"kind": "plan", "path": "", "plan_id": active.get("plan_id"),
              "detail": f"{active.get('completed')}/{active.get('total')}",
              "action": f"continue with the next step: {nxt}"}
    if isinstance(baseline, dict):
        prior = str(baseline.get("plan_fingerprint") or "")
        if prior and prior == _fingerprint(root, signal):
            return None
    return signal


def detect(
    root: Path,
    baseline: dict[str, Any] | None = None,
    *,
    sid: str = "",
    diagnostics: list[str] | None = None,
) -> dict[str, Any] | None:
    """First unfinished-work signal in precedence order, or None when the tree looks done."""
    root = Path(root)
    signal = _signal_plan(root, baseline)
    if signal:
        return signal
    ok_status, files, unmerged, untracked, overflow = _worktree_status(root)
    if not ok_status:
        if diagnostics is not None:
            diagnostics.append("working-tree status unavailable")
        return None  # off-repo/status failure: never block without trustworthy tree evidence
    signal = _signal_conflict(root, files, unmerged, baseline)
    if signal:
        return signal
    signal = _signal_syntax(root, files, baseline)
    if signal:
        return signal
    # Added-line attribution is incomplete once the changed-path cap is exceeded. Never turn a
    # partial marker scan into blocking evidence; yield rather than guessing which request owns it.
    if not overflow:
        signal = _signal_marker(root, files, untracked, baseline)
        if signal:
            return signal
    elif diagnostics is not None:
        diagnostics.append(f"marker scan exceeded {MAX_FILES_SCANNED} changed paths")
    if sid:
        signal = _signal_verification(root, sid, diagnostics)
        if signal:
            return signal
    return _signal_acceptance(root, baseline)


def _fingerprint(root: Path, signal: dict[str, Any]) -> str:
    """Identity of the unfinished signal plus the exact evidence behind it.

    `git diff --stat` is not enough: replacing text with the same line counts leaves the
    stat byte-identical, so real progress was previously misclassified as a stall and the
    guard yielded early. Hash only the evidence file/plan instead. This is both stronger and
    cheaper than hashing an arbitrarily large working-tree diff on the Stop hot path.
    """
    evidence = ""
    rel = str(signal.get("path") or "")
    if rel:
        evidence = _read_text(Path(root) / rel) or ""
    elif signal.get("plan_id"):
        try:
            from .plan_state import plan_path

            evidence = _read_text(plan_path(root, str(signal["plan_id"]))) or ""
        except (OSError, ValueError):
            evidence = ""
    basis = json.dumps(signal, ensure_ascii=False, sort_keys=True) + "|" + evidence
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _read_state(root: Path) -> dict[str, Any]:
    try:
        raw = _read_text(state_path(root))
        if raw is None:
            return {}
        obj = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _write_state(root: Path, state: dict[str, Any]) -> bool:
    path = state_path(root)
    try:
        atomic_write_private_text(
            path,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            root=Path(root),
        )
        return True
    except OSError:
        return False


def _capture_request_baseline(root: Path) -> dict[str, Any]:
    """Snapshot only unfinished-work evidence present before a user request starts.

    This is deliberately not a whole-repository snapshot. At most forty dirty files are
    inspected and only compact hashes of already-bad evidence are retained. It prevents an
    old dirty syntax error, conflict, TODO, plan, or acceptance failure from being blamed on
    an unrelated new request while keeping both storage and prompt-start latency bounded.
    """
    root = Path(root)
    baseline: dict[str, Any] = {"ts": time.time()}
    ok_status, files, unmerged, untracked, overflow = _worktree_status(root)
    if not ok_status:
        return baseline

    conflicts: list[str] = []
    syntax: dict[str, str] = {}
    unmerged_state: dict[str, str] = {}
    for rel in files:
        text = _read_text(root / rel)
        if rel in unmerged:
            unmerged_state[rel] = _digest_text(text) if text is not None else "unreadable"
        if text is None:
            continue
        for line in text.splitlines():
            if _CONFLICT_RE.match(line):
                conflicts.append(_signal_key("conflict", rel, line.strip()))
                if len(conflicts) >= MAX_BASELINE_SIGNAL_KEYS:
                    break
        if rel.endswith(_PY_SUFFIXES):
            try:
                ast.parse(text)
            except SyntaxError:
                syntax[rel] = _digest_text(text)
            except (ValueError, RecursionError, MemoryError):
                pass

    markers: list[str] = []
    candidates = [rel for rel in files if rel.endswith(_TEXT_SUFFIXES)]
    if candidates and not overflow:
        added = _added_lines_by_path(root, candidates, untracked)
        for rel in candidates:
            for line in added.get(rel, ()):
                match = _MARKER_RE.search(line)
                if not match:
                    continue
                markers.append(
                    _signal_key("marker", rel, f"{match.group(1)}\0{line.strip()}")
                )
                if len(markers) >= MAX_BASELINE_SIGNAL_KEYS:
                    break
            if len(markers) >= MAX_BASELINE_SIGNAL_KEYS:
                break

    plan = _signal_plan(root)
    if plan:
        baseline["plan_fingerprint"] = _fingerprint(root, plan)
    try:
        from . import eval_loop

        baseline["acceptance_offset"] = root.joinpath(*eval_loop.CASES_PATH).stat().st_size
    except OSError:
        baseline["acceptance_offset"] = 0
    baseline.update(
        {
            "conflicts": conflicts[:MAX_BASELINE_SIGNAL_KEYS],
            "markers": markers[:MAX_BASELINE_SIGNAL_KEYS],
            "syntax": syntax,
            "unmerged": unmerged_state,
            "status_overflow": overflow,
        }
    )
    return baseline


def begin_request(root: Path, sid: str) -> bool:
    """Reset loop evidence and bind a compact baseline to a newly submitted request."""
    safe_sid = str(sid or "default")
    baseline = _capture_request_baseline(Path(root))
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            sessions = state.get("sessions")
            sessions = sessions if isinstance(sessions, dict) else {}
            sessions.pop(safe_sid, None)
            baselines = state.get("baselines")
            baselines = baselines if isinstance(baselines, dict) else {}
            baselines[safe_sid] = baseline
            if len(baselines) > 8:
                for key in sorted(
                    baselines,
                    key=lambda k: float((baselines[k] or {}).get("ts") or 0.0),
                )[:-8]:
                    baselines.pop(key, None)
            state["sessions"] = sessions
            state["baselines"] = baselines
            activities = state.get("activities")
            activities = activities if isinstance(activities, dict) else {}
            activities[safe_sid] = {"seq": 0, "ts": time.time(), "ledger_complete": True}
            if len(activities) > 32:
                for key in sorted(
                    activities,
                    key=lambda k: float((activities[k] or {}).get("ts") or 0.0),
                )[:-32]:
                    activities.pop(key, None)
            state["activities"] = activities
            degraded = state.get("degraded")
            degraded = degraded if isinstance(degraded, dict) else {}
            degraded.pop(safe_sid, None)
            state["degraded"] = degraded
            return _write_state(root, state)
    except OSError:
        return False


def _request_baseline(
    root: Path,
    sid: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    state = _read_state(root)
    baselines = state.get("baselines")
    if not isinstance(baselines, dict):
        return None
    row = baselines.get(str(sid or "default"))
    if not isinstance(row, dict):
        return None
    try:
        age = (time.time() if now is None else float(now)) - float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    if age < -300 or age > REQUEST_EVIDENCE_TTL_SECONDS:
        return None
    return row


def request_plan_signal(root: Path, sid: str) -> dict[str, Any] | None:
    """Active plan summary only when it changed or appeared during this request."""
    try:
        from . import plan_state

        active = plan_state.active_summary(Path(root))
    except Exception:
        return None
    if not active or int(active.get("remaining") or 0) <= 0:
        return None
    signal = {
        "kind": "plan",
        "path": "",
        "plan_id": active.get("plan_id"),
        "detail": f"{active.get('completed')}/{active.get('total')}",
        "action": f"continue with the next step: {active.get('next_label') or 'the next unchecked step'}",
    }
    baseline = _request_baseline(Path(root), sid)
    if baseline is None:
        return None
    prior = str((baseline or {}).get("plan_fingerprint") or "")
    if prior and prior == _fingerprint(Path(root), signal):
        return None
    return active


def _stalled(root: Path, sid: str, fingerprint: str) -> bool:
    """True when this exact signal+tree has already been blocked MAX_STALL_REPEATS times.

    This is the anti-nag rail. Without it a model that cannot fix the signal (or refuses to)
    would be re-prompted until the hard cap, burning the whole budget on no progress.
    """
    limit = _int_env("AI_COMPLETION_GUARD_MAX_STALL", MAX_STALL_REPEATS, minimum=1)
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            sessions = state.get("sessions")
            sessions = sessions if isinstance(sessions, dict) else {}
            row = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
            if str(row.get("fingerprint") or "") == fingerprint:
                count = int(row.get("count") or 0) + 1
            else:
                count = 1
            # Keep only the most recent 32 sessions so this sidecar cannot grow without bound.
            sessions[sid] = {"fingerprint": fingerprint, "count": count, "ts": time.time()}
            if len(sessions) > 32:
                for key in sorted(
                    sessions,
                    key=lambda k: float((sessions[k] or {}).get("ts") or 0.0),
                )[:-32]:
                    sessions.pop(key, None)
            state["sessions"] = sessions
            if not _write_state(root, state):
                return True
            return count > limit
    except OSError:
        return True  # state cannot be made durable → never risk an unbounded loop


def reset_session(root: Path, sid: str) -> bool:
    """Delete this session's stall and request-baseline state."""
    safe_sid = str(sid or "default")
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            sessions = state.get("sessions")
            sessions = sessions if isinstance(sessions, dict) else {}
            sessions.pop(safe_sid, None)
            baselines = state.get("baselines")
            baselines = baselines if isinstance(baselines, dict) else {}
            baselines.pop(safe_sid, None)
            activities = state.get("activities")
            activities = activities if isinstance(activities, dict) else {}
            activities.pop(safe_sid, None)
            state["sessions"] = sessions
            state["baselines"] = baselines
            state["activities"] = activities
            degraded = state.get("degraded")
            degraded = degraded if isinstance(degraded, dict) else {}
            degraded.pop(safe_sid, None)
            state["degraded"] = degraded
            return _write_state(root, state)
    except OSError:
        return False


def _requires_user_input(payload: dict[str, Any]) -> bool:
    """Only trust host/harness flags, never model-authored punctuation.

    A trailing question used to disable the guard. That made the exact user complaint — a
    model asking an unnecessary question instead of finishing — a universal bypass. Genuine
    clarification remains bounded by the no-progress rail even on hosts that expose none of
    these flags: two identical nudges, then the question reaches the user.
    """
    return any(
        payload.get(key) is True
        for key in ("user_input_required", "awaiting_user", "approval_required")
    )


def _is_antigravity(payload: dict[str, Any]) -> bool:
    agent = str(payload.get("agent") or payload.get("agent_type") or "").strip().lower()
    return (
        "antigravity" in agent
        or agent == "agy"
        or bool(_ANTIGRAVITY_PAYLOAD_KEYS.intersection(payload))
    )


def _termination_allows_continuation(payload: dict[str, Any]) -> bool:
    """Yield on Antigravity terminal/system stops; continue only a normal model stop."""
    if not _is_antigravity(payload):
        return True
    if payload.get("fullyIdle") is False or str(payload.get("error") or "").strip():
        return False
    reason = str(payload.get("terminationReason") or "").strip().lower()
    return reason in ("", "model_stop")


def _has_context_pressure(payload: dict[str, Any]) -> bool:
    return any(payload.get(k) for k in ("context_pressure", "compact_pending", "near_compaction"))


def _record_degraded_notice(root: Path, sid: str, detail: str) -> None:
    message = (
        f"Code Brain completion guard degraded: {str(detail).strip()[:300]}. "
        "Stop was allowed; verification was not certified."
    )
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            rows = state.get("degraded")
            rows = rows if isinstance(rows, dict) else {}
            rows[str(sid or "default")] = {
                "message": message,
                "emitted": False,
                "ts": time.time(),
            }
            if len(rows) > 32:
                for key in sorted(
                    rows,
                    key=lambda item: float((rows[item] or {}).get("ts") or 0.0),
                )[:-32]:
                    rows.pop(key, None)
            state["degraded"] = rows
            _write_state(root, state)
    except (OSError, TypeError, ValueError):
        pass


def consume_degraded_notice(root: Path, sid: str) -> str:
    """Consume one fail-open notice; empty means no degraded decision is pending."""
    if not state_path(root).is_file() or state_path(root).is_symlink():
        return ""
    try:
        with private_file_lock(state_path(root).with_suffix(".lock"), root=Path(root)):
            state = _read_state(root)
            rows = state.get("degraded")
            if not isinstance(rows, dict):
                return ""
            row = rows.get(str(sid or "default"))
            if not isinstance(row, dict) or row.get("emitted") is not False:
                return ""
            message = str(row.get("message") or "")[:500]
            if not message:
                return ""
            row = dict(row)
            row["emitted"] = True
            rows[str(sid or "default")] = row
            state["degraded"] = rows
            if not _write_state(root, state):
                return ""
            return message
    except (OSError, TypeError, ValueError):
        return ""


def guard_directive(payload: dict[str, Any], root: Path, *, now: float | None = None) -> str | None:
    """Reason to refuse this turn end, or None to let it end. Never raises.

    The caller sets decision=block + reason=<this> on Stop ONLY when not already blocking for
    security. Returns None the moment any safety rail trips — a false stop is recoverable by
    the user typing again, a false loop is not.
    """
    try:
        if not _enabled() or not isinstance(payload, dict):
            return None
        if _has_context_pressure(payload):
            return None
        if not _termination_allows_continuation(payload):
            return None
        if _requires_user_input(payload):
            return None
        sid = _session_id(payload)
        baseline = _request_baseline(root, sid, now=now)
        if baseline is None:
            _record_degraded_notice(root, sid, "request baseline missing, corrupt, or expired")
            return None  # missing/corrupt/stale request attribution can never justify a loop
        diagnostics: list[str] = []
        signal = detect(root, baseline, sid=sid, diagnostics=diagnostics)
        if not signal:
            if diagnostics:
                _record_degraded_notice(root, sid, "; ".join(dict.fromkeys(diagnostics)))
            return None
        fingerprint = _fingerprint(root, signal)
        if _stalled(root, sid, fingerprint):
            return None  # no progress across repeats → stop nagging, hand back to the user
        # Share loop_continuation's bounded per-session budget: one "keep going" ledger.
        from .loop_continuation import _bump_counter
        if not _bump_counter(root, sid, now=now if now is not None else time.time()):
            return None
        kind = str(signal.get("kind"))
        where = str(signal.get("detail") or "")
        return (
            f"cb-guard[{kind}]: unfinished work detected{f' at {where}' if where else ''}. "
            f"Do NOT stop — {signal.get('action')}. "
            "If it is genuinely blocked, say so explicitly and record the blocker; "
            "if this signal is wrong, state why in one line and stop."
        )
    except Exception:
        return None
