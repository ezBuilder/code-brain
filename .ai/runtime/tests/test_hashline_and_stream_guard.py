from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.stream_guard import evaluate_hook_payload, scan_text  # noqa: E402


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


def test_stream_guard_blocks_nested_relative_and_key_paths() -> None:
    dot_env = "." + "env"
    id_key = "id_" + "rsa"
    credentials = "credentials" + ".json"
    key_suffix = "." + "p8"
    examples = (
        f"cat config/{dot_env}.production",
        f"cat ./{dot_env}",
        f"dd if={dot_env} of=/tmp/synthetic-copy",
        f"cat AuthKey_SAMPLE{key_suffix}",
        f"cat secrets/{id_key}",
        f"cat app/{credentials}",
    )
    for command in examples:
        scan = scan_text(command, scope="tool")
        assert scan["ok"] is False, command
        assert scan["matches"][0]["id"] == "credential_path", command


def test_stream_guard_allows_prompt_to_discuss_credential_filename() -> None:
    dot_env = "." + "env"
    scan = scan_text(f"Explain why agents must not read {dot_env} files.", scope="prompt")
    assert scan["ok"] is True


def test_stream_guard_allows_patch_fixture_content_but_checks_patch_target() -> None:
    dot_env = "." + "env"
    fixture_patch = (
        "*** Begin Patch\n"
        "*** Update File: tests/test_guard.py\n"
        "@@\n"
        f"+example = 'cat {dot_env}'\n"
        "*** End Patch"
    )
    fixture_scan = evaluate_hook_payload(
        "PreToolUse",
        {"tool_name": "functions.apply_patch", "tool_input": {"patch": fixture_patch}},
    )
    assert fixture_scan["ok"] is True

    sensitive_target_patch = (
        "*** Begin Patch\n"
        f"*** Update File: config/{dot_env}\n"
        "@@\n"
        "+placeholder=true\n"
        "*** End Patch"
    )
    target_scan = evaluate_hook_payload(
        "PreToolUse",
        {"tool_name": "apply_patch", "tool_input": {"patch": sensitive_target_patch}},
    )
    assert target_scan["ok"] is False
    assert target_scan["matches"][0]["id"] == "credential_path"


def test_stream_guard_scans_host_path_aliases_and_nested_edit_paths() -> None:
    dot_env = "." + "env"
    examples = (
        ("Read", {"absolute_path": f"/repo/{dot_env}"}),
        ("Read", {"paths": [f"/repo/{dot_env}"]}),
        ("Edit", {"filePath": f"/repo/{dot_env}"}),
        ("Edit", {"source_path": f"/repo/{dot_env}"}),
        ("MultiEdit", {"edits": [{"TargetFile": f"config/{dot_env}.local"}]}),
        ("Read", f"/repo/{dot_env}"),
        ("Read", {"unrecognized_target": f"/repo/{dot_env}"}),
    )
    for tool_name, tool_input in examples:
        scan = evaluate_hook_payload(
            "PreToolUse",
            {"tool_name": tool_name, "tool_input": tool_input},
        )
        assert scan["ok"] is False, (tool_name, tool_input)
        assert scan["matches"][0]["id"] == "credential_path"


def test_stream_guard_does_not_scan_structured_write_fixture_body_as_a_path() -> None:
    dot_env = "." + "env"
    scan = evaluate_hook_payload(
        "PreToolUse",
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "tests/fixture.txt",
                "content": f"synthetic example: cat {dot_env}",
            },
        },
    )
    assert scan["ok"] is True


def test_stream_guard_conservatively_blocks_credential_fixture_inside_tool_heredoc() -> None:
    dot_env = "." + "env"
    id_key = "id_" + "rsa"
    command = (
        "python3 - <<'PY'\n"
        f"examples = ['cat {dot_env}', 'cat nested/{id_key}']\n"
        "print(len(examples))\n"
        "PY"
    )
    scan = evaluate_hook_payload(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )
    assert scan["ok"] is False


def test_stream_guard_blocks_real_private_key_header_variants_in_write_body() -> None:
    headers = (
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "-----BEGIN " + "DSA PRIVATE KEY-----",
        "-----BEGIN " + "EC PRIVATE KEY-----",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
    )
    for header in headers:
        scan = evaluate_hook_payload(
            "PreToolUse",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "notes.txt",
                    "content": header + "\nSYNTHETIC-FIXTURE\n",
                },
            },
        )
        assert scan["ok"] is False, header
        assert any(match["id"] == "private_key_literal" for match in scan["matches"])


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
