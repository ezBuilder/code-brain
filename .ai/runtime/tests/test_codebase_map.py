from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.codebase_map import build_codebase_map  # noqa: E402
from ai_core.hooks import handle_hook  # noqa: E402


def run_ai(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    api = repo / "api"
    web = repo / "web"
    api.mkdir(parents=True)
    web.mkdir()
    (api / "AGENTS.md").write_text("api rules\n", encoding="utf-8")
    (api / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    (api / "service.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (web / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "test": "vitest", "build": "vite build"}}),
        encoding="utf-8",
    )
    (web / "app.tsx").write_text("export const App = () => null\n", encoding="utf-8")
    (repo / "Makefile").write_text("test:\n\ttrue\nrelease-gate:\n\ttrue\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    (repo / ".ai").mkdir()
    (repo / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return repo


def test_codebase_map_detects_local_instructions_and_scoped_commands(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    payload = build_codebase_map(repo)

    assert payload["ok"] is True
    by_path = {entry["path"]: entry for entry in payload["entries"]}
    assert "api" in by_path
    assert "python" in by_path["api"]["languages"]
    assert "api/AGENTS.md" in by_path["api"]["instructions"]
    assert "cd api && pytest" in by_path["api"]["commands"]
    assert "cd web && npm run test" in by_path["web"]["commands"]
    assert "cd . && make test" in payload["root_commands"]
    assert "Codebase map:" in payload["additionalContext"]


def test_code_map_cli_outputs_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = run_ai(repo, "code", "map", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert any(entry["path"] == "web" for entry in payload["entries"])


def test_codebase_map_fast_mode_ignores_untracked_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "map-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Map Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    extra = repo / "scratch"
    extra.mkdir()
    (extra / "tool.py").write_text("def temp():\n    pass\n", encoding="utf-8")

    fast = build_codebase_map(repo, include_untracked=False)
    full = build_codebase_map(repo, include_untracked=True)

    assert not any(entry["path"] == "scratch" for entry in fast["entries"])
    assert any(entry["path"] == "scratch" for entry in full["entries"])


def test_session_start_context_includes_codebase_map(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    payload = handle_hook(repo, "SessionStart", {"agent": "codex", "dry": True})

    assert payload["ok"] is True
    context = payload["additionalContext"]
    assert "Codebase map:" in context
    assert "web; lang=typescript" in context
