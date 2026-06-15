from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .redact import redact_text, redact_value
import subprocess

MAX_ENTRIES = 40
CONTEXT_ENTRY_LIMIT = 8
COMMAND_LIMIT = 8

LANG_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".dart": "dart",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c/c++",
    ".cpp": "c++",
    ".hpp": "c++",
}


def build_codebase_map(root: Path, *, max_entries: int = MAX_ENTRIES, include_untracked: bool = True) -> dict[str, Any]:
    """Build a live, small map that tells an agent where to start.

    This is intentionally filesystem-backed and cheap. It complements the FTS
    index: before searching for a vague task, an agent can see top-level areas,
    nearby instruction files, and the closest test/build commands.
    """
    root = Path(root)
    entries = _entries(root, max_entries=max_entries, include_untracked=include_untracked)
    payload = {
        "ok": True,
        "root": root.name,
        "entry_count": len(entries),
        "entries": entries,
        "root_instructions": _root_instructions(root),
        "root_commands": _commands_for_dir(root, label="."),
        "additionalContext": render_context(entries, root_commands=_commands_for_dir(root, label=".")),
    }
    return redact_value(payload)


def render_context(entries: list[dict[str, Any]], *, root_commands: list[str] | None = None) -> str:
    lines = ["코드베이스 지도: start in the narrowest matching path; read local AGENTS/CLAUDE before editing."]
    for entry in entries[:CONTEXT_ENTRY_LIMIT]:
        langs = ",".join(entry.get("languages") or [])
        bits = [str(entry.get("path") or "")]
        if langs:
            bits.append(f"lang={langs}")
        purpose = str(entry.get("purpose") or "")
        if purpose:
            bits.append(purpose)
        commands = entry.get("commands") or []
        if commands:
            bits.append("cmd=" + " | ".join(commands[:2]))
        instructions = entry.get("instructions") or []
        if instructions:
            bits.append("ctx=" + ",".join(instructions[:2]))
        lines.append("  - " + "; ".join(bits))
    root_commands = root_commands or []
    if root_commands:
        lines.append("루트 명령: " + " | ".join(root_commands[:3]))
    return "\n".join(lines)


def _entries(root: Path, *, max_entries: int, include_untracked: bool) -> list[dict[str, Any]]:
    all_files = [path for path in _working_tree_files(root, include_untracked=include_untracked) if _is_visible_project_file(root, path)]
    top_map: dict[str, list[Path]] = {}
    for path in all_files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not rel.parts:
            continue
        top = _top_bucket(rel)
        if top in {".git", ".codebrain"}:
            continue
        top_map.setdefault(top, []).append(path)

    entries: list[dict[str, Any]] = []
    for top, files in sorted(top_map.items()):
        top_path = root if top == "." else root / top
        text_files = [p for p in files if _looks_textish(p)]
        commands = _commands_for_dir(top_path if top_path.is_dir() else root, label=top if top_path.is_dir() else ".")
        instructions = _instruction_files(root, top_path if top_path.is_dir() else root)
        entries.append(
            {
                "path": top,
                "kind": "dir" if top_path.is_dir() else "file",
                "files": len(files),
                "text_files": len(text_files),
                "languages": _languages(files),
                "purpose": _purpose(top, files),
                "instructions": instructions,
                "commands": commands,
            }
        )
    entries.sort(key=lambda item: (-int(item["text_files"]), str(item["path"])))
    return entries[:max(1, min(max_entries, MAX_ENTRIES))]


def _is_visible_project_file(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".dart_tool", ".gradle", "build", "dist", "coverage", "Pods", "DerivedData"} for part in rel.parts):
        return False
    if rel.parts[:2] in {(".ai", "cache"), (".ai", "memory"), (".ai", "skills"), (".ai", "precall_rules"), (".ai", "agents_catalog")}:
        return False
    return path.is_file()


def _working_tree_files(root: Path, *, include_untracked: bool) -> list[Path]:
    try:
        args = ["git", "ls-files", "-z", "--cached"]
        if include_untracked:
            args.extend(["--others", "--exclude-standard"])
        result = subprocess.run(
            args,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(path for path in root.rglob("*") if path.is_file())
    rels = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return sorted(path for rel in rels if (path := root / rel).is_file())


def _top_bucket(rel: Path) -> str:
    if len(rel.parts) == 1:
        return "."
    if rel.parts[0] == ".ai":
        if len(rel.parts) >= 2 and rel.parts[1] == "runtime":
            if len(rel.parts) >= 3 and rel.parts[2] in {"src", "tests"}:
                return f".ai/runtime/{rel.parts[2]}"
            return ".ai/runtime"
        return ".ai"
    return rel.parts[0]


def _looks_textish(path: Path) -> bool:
    return path.suffix in LANG_BY_SUFFIX or path.suffix in {".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".txt"}


def _languages(files: list[Path]) -> list[str]:
    counts: dict[str, int] = {}
    for path in files:
        lang = LANG_BY_SUFFIX.get(path.suffix)
        if not lang:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    return [lang for lang, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:4]]


def _purpose(top: str, files: list[Path]) -> str:
    lowered = top.casefold()
    if lowered == ".":
        return "repo root"
    if lowered in {"src", "lib", "app"}:
        return "main source"
    if lowered in {"test", "tests", "__tests__"}:
        return "tests"
    if lowered in {"docs", "doc"}:
        return "docs"
    if lowered in {"scripts", "tools"}:
        return "operator scripts"
    if lowered in {".github"}:
        return "ci workflows"
    names = {path.name for path in files}
    if "package.json" in names:
        return "node package"
    if "pyproject.toml" in names:
        return "python package"
    if "pubspec.yaml" in names:
        return "flutter/dart package"
    return ""


def _root_instructions(root: Path) -> list[str]:
    return [name for name in ("AGENTS.md", "CLAUDE.md", ".ai/AGENTS.md") if (root / name).exists()]


def _instruction_files(root: Path, directory: Path) -> list[str]:
    out = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = directory / name
        if path.exists():
            try:
                out.append(path.relative_to(root).as_posix())
            except ValueError:
                out.append(path.name)
    return out


def _commands_for_dir(directory: Path, *, label: str | None = None) -> list[str]:
    commands: list[str] = []
    display = label or directory.name or "."
    if (directory / "package.json").exists():
        commands.extend(_package_scripts(directory / "package.json", label=display))
    if (directory / "pyproject.toml").exists():
        commands.append(f"cd {display} && pytest")
    if (directory / "pubspec.yaml").exists():
        commands.extend([f"cd {display} && flutter analyze", f"cd {display} && flutter test"])
    if (directory / "Cargo.toml").exists():
        commands.append(f"cd {display} && cargo test")
    if (directory / "go.mod").exists():
        commands.append(f"cd {display} && go test ./...")
    makefile = directory / "Makefile"
    if makefile.exists():
        commands.extend(_make_targets(makefile, display))
    return _dedupe(commands)[:COMMAND_LIMIT]


def _package_scripts(path: Path, *, label: str) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return []
    preferred = ["test", "lint", "typecheck", "build"]
    return [f"cd {label} && npm run {name}" for name in preferred if name in scripts]


def _make_targets(path: Path, dirname: str) -> list[str]:
    try:
        text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    targets = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s|$)", line)
        if not match:
            continue
        name = match.group(1)
        if name in {"test", "lint", "check", "build", "doctor", "release-gate"}:
            targets.append(f"cd {dirname} && make {name}")
    return targets


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
