from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .redact import redact_value

COMPLETION_TARGET = 0.95
DEFAULT_BUDGET = {
    "max_wall_sec": 1800,
    "max_tool_calls": 120,
    "max_retry_count": 2,
}
PROTECTED_PATHS = [
    ".env*",
    "secrets/**",
    ".git/config",
    "~/.codex/**",
    "~/.claude/**",
    ".ai/memory/**",
]
DEPENDENCY_MANIFESTS = [
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "go.mod",
    "Cargo.toml",
]
REQUEST_PATTERNS = (
    "하네스",
    "harness",
    "95%",
    "자율 개선",
    "자율개선",
    "신규 프로젝트",
    "고도화",
    "commercial",
    "production-ready",
)


def analyze(root: Path) -> dict[str, Any]:
    root = Path(root)
    manifests = [rel for rel in DEPENDENCY_MANIFESTS if (root / rel).exists()]
    source_count = _count_files(root, ("src", "app", "lib", "packages", ".ai/runtime/src"), {".py", ".js", ".jsx", ".ts", ".tsx", ".dart", ".go", ".rs"})
    test_count = _count_files(root, ("test", "tests", "__tests__", ".ai/runtime/tests"), {".py", ".js", ".jsx", ".ts", ".tsx", ".dart", ".go", ".rs"})
    dirty_count = len(_git_status(root))
    mode = _mode(manifests=manifests, source_count=source_count, test_count=test_count)
    should_use = mode != "observe"
    payload = {
        "ok": True,
        "should_use": should_use,
        "mode": mode,
        "completion_target": COMPLETION_TARGET,
        "signals": {
            "dependency_manifests": manifests,
            "source_files": source_count,
            "test_files": test_count,
            "dirty_files": dirty_count,
        },
        "policy": {
            "protected_paths": PROTECTED_PATHS,
            "dependency_manifests_need_explicit_scope": True,
            "worker_report_required": True,
            "post_gate_requires_tests_or_reason": True,
        },
        "budget": DEFAULT_BUDGET,
        "loop": [
            "derive scoped task and acceptance criteria from the user goal",
            "own paths before editing; avoid protected paths unless explicitly approved",
            "implement in small iterations",
            "run closest verification first",
            "record pass/fail evidence internally and keep working on remaining gaps",
            "repeat until release/readiness confidence is at least 95%, no required work remains, or a concrete blocker is proven",
        ],
    }
    return redact_value(payload)


def context_line(root: Path) -> str:
    payload = analyze(root)
    signals = payload["signals"]
    return (
        "자율 하니스: "
        f"mode={payload['mode']}, target={int(COMPLETION_TARGET * 100)}%, "
        f"src={signals['source_files']}, tests={signals['test_files']}, dirty={signals['dirty_files']}. "
        "For new builds or hardening, self-apply: define acceptance criteria, own paths, iterate with tests/doctor/release-gate, "
        "and continue until target confidence, no required work remains, or a concrete blocker."
    )


def requested(payload: dict[str, Any]) -> bool:
    text = _payload_text(payload).lower()
    return any(pattern.lower() in text for pattern in REQUEST_PATTERNS)


def directive(root: Path, *, explicit: bool = False) -> str:
    payload = analyze(root)
    prefix = "Explicit harness request detected." if explicit else "Autonomous harness available."
    return (
        f"{prefix} Apply the Code Brain autonomous harness now: "
        f"mode={payload['mode']}, target={int(COMPLETION_TARGET * 100)}%. "
        "Do not wait for a separate `ai harness` command. Convert the user goal into scoped tasks and acceptance criteria, "
        "declare owned paths before edits, enforce protected-path and dependency-manifest gates, run the closest verification, "
        "and iterate until target confidence, no required work remains, or a concrete blocker."
    )


def _payload_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    keys = (
        "prompt",
        "message",
        "user_prompt",
        "user_message",
        "input",
        "text",
        "transcript",
    )
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    if not parts:
        try:
            parts.append(str(payload))
        except Exception:
            pass
    return "\n".join(parts)


def _mode(*, manifests: list[str], source_count: int, test_count: int) -> str:
    if not manifests and source_count < 5:
        return "bootstrap"
    if source_count and test_count == 0:
        return "stabilize"
    if source_count >= 5:
        return "hardening"
    return "observe"


def _count_files(root: Path, dirs: tuple[str, ...], suffixes: set[str]) -> int:
    count = 0
    for dirname in dirs:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in suffixes:
                count += 1
                if count >= 999:
                    return count
    return count


def _git_status(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]
