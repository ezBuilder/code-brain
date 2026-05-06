from __future__ import annotations

from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".ai" / "config.yaml").is_file():
            return candidate
    raise RuntimeError("could not find repo root containing .ai/config.yaml")


def ai_dir(root: Path) -> Path:
    return root / ".ai"

