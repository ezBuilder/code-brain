from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))


def run_ai(*args: str, stdin: str | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")
    return subprocess.run(
        [PYTHON, "-m", "ai_core.cli", *args],
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_hashline_read_and_verify(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    sample = tmp_path / "sample.txt"
    sample.write_text("alpha\nbeta\n", encoding="utf-8")
    result = run_ai("code", "read-hashline", str(sample), "--json", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["hash_format"] == "line+sha12|content"
    first = payload["content"].splitlines()[0]
    prefix, content = first.split("|", 1)
    line_s, hash_s = prefix.split("+", 1)
    verify = run_ai(
        "code",
        "verify-hashline",
        str(sample),
        "--json",
        stdin=json.dumps([{"line": int(line_s), "hash": hash_s, "content": content}]),
        cwd=tmp_path,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert json.loads(verify.stdout)["ok"] is True
    sample.write_text("changed\nbeta\n", encoding="utf-8")
    stale = run_ai(
        "code",
        "verify-hashline",
        str(sample),
        "--json",
        stdin=json.dumps([{"line": int(line_s), "hash": hash_s, "content": content}]),
        cwd=tmp_path,
    )
    assert stale.returncode != 0
    assert json.loads(stale.stdout)["ok"] is False


def test_hashline_refuses_credential_like_path(tmp_path: Path) -> None:
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x\n", encoding="utf-8")
    result = run_ai("code", "read-hashline", ".env", "--json", cwd=tmp_path)
    assert result.returncode != 0
    assert "credential-like" in result.stdout


def test_stream_guard_scan_blocks_secret_path() -> None:
    result = run_ai("guard", "scan", "--text", "cat .env", "--scope", "tool", "--json")
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["matches"][0]["id"] == "credential_path"


def test_stream_guard_pretooluse_blocks_read_env() -> None:
    result = run_ai(
        "hook",
        "PreToolUse",
        "--json",
        stdin=json.dumps(
            {
                "agent": "codex",
                "dry": True,
                "tool_name": "Read",
                "tool_input": {"file_path": ".env"},
            }
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert payload["stream_guard"]["matches"][0]["id"] == "credential_path"
