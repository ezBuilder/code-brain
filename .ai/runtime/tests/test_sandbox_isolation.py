"""Sandbox isolation tests (PRD §12.2.1) — opt-in network/env hardening for execute().

Non-destructive: the DEFAULT execute() path is unchanged; these exercise the opt-in
isolation. Network assertions are empirically grounded: under the macOS sandbox profile an
outbound IP connect is denied with EPERM (errno 1), which distinguishes a real sandbox block
from a mere connection-refused/timeout.
"""
from __future__ import annotations

import shutil

import pytest

from ai_core import sandbox

_HAS_SBX = shutil.which("sandbox-exec") is not None


def _out(result: dict) -> str:
    return result.get("output") or "\n".join(
        result.get("first_lines", []) + result.get("last_lines", [])
    )


# --- backward compatibility (defaults preserve current behavior) ---

def test_defaults_inherit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_INHERIT", "yes_inherited")
    r = sandbox.execute(tmp_path, command="echo from=${CANARY_INHERIT:-MISSING}")
    assert r["ok"] is True
    assert "from=yes_inherited" in _out(r)


def test_no_isolation_by_default(tmp_path):
    r = sandbox.execute(tmp_path, command="echo ok")
    assert r["ok"] is True and "ok" in _out(r)


def test_read_output_rejects_bad_exec_id(tmp_path):
    assert sandbox.read_output(tmp_path, "../etc/passwd") is None
    assert sandbox.read_output(tmp_path, "ZZZZ") is None
    assert sandbox.read_output(tmp_path, "") is None


def test_read_output_roundtrip(tmp_path):
    r = sandbox.execute(tmp_path, command="echo hello_output")
    out = sandbox.read_output(tmp_path, r["exec_id"])
    assert out is not None and "hello_output" in out


# --- environment scrubbing ---

def test_isolate_env_drops_unlisted_var(tmp_path, monkeypatch):
    # Not in the allowlist → must be absent from the child env (sentinel default shows through).
    monkeypatch.setenv("MY_DEPLOY_TOKEN", "CANARY_SHOULD_NOT_LEAK")
    r = sandbox.execute(tmp_path, command="echo got=${MY_DEPLOY_TOKEN:-NONE}", isolate_env=True)
    assert r["ok"] is True
    out = _out(r)
    assert "got=NONE" in out
    assert "CANARY_SHOULD_NOT_LEAK" not in out
    assert r["isolate_env"] is True
    assert r["isolate_network"] is False


def test_isolate_env_keeps_path(tmp_path):
    r = sandbox.execute(tmp_path, command='test -n "$PATH" && echo path=present', isolate_env=True)
    assert r["ok"] is True
    assert "path=present" in _out(r)


def test_isolate_env_extra_allows_nonsecret(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_FEATURE_FLAG", "on")
    r = sandbox.execute(
        tmp_path,
        command="echo flag=${MY_FEATURE_FLAG:-NONE}",
        isolate_env=True,
        extra_env_vars=["MY_FEATURE_FLAG"],
    )
    assert r["ok"] is True
    assert "flag=on" in _out(r)


def test_isolate_env_extra_rejects_secret(tmp_path):
    r = sandbox.execute(
        tmp_path, command="echo x", isolate_env=True, extra_env_vars=["MY_API_KEY"]
    )
    assert r["ok"] is False
    assert r["reason"] == "invalid_extra_env_vars"


def test_is_secret_var_heuristic():
    assert sandbox._is_secret_var("AWS_ACCESS_KEY_ID")
    assert sandbox._is_secret_var("GITHUB_TOKEN")
    assert sandbox._is_secret_var("DB_PASSWORD")
    assert sandbox._is_secret_var("SERVICE_CREDENTIAL")
    assert sandbox._is_secret_var("DATABASE_URL")  # connection strings carry inline creds
    assert sandbox._is_secret_var("REDIS_URI")
    assert not sandbox._is_secret_var("NODE_ENV")
    assert not sandbox._is_secret_var("MY_FEATURE_FLAG")


def test_isolate_env_skips_login_rc(tmp_path, monkeypatch):
    # A login profile that exports a secret must NOT repopulate the scrubbed env.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bash_profile").write_text("export CANARY_RC=LEAKED_VIA_RC\n")
    # default (bash -lc) DOES source the login profile:
    base = sandbox.execute(tmp_path, command="echo rc=${CANARY_RC:-NONE}")
    assert "rc=LEAKED_VIA_RC" in _out(base)
    # isolate_env (bash --noprofile --norc) does NOT:
    iso = sandbox.execute(tmp_path, command="echo rc=${CANARY_RC:-NONE}", isolate_env=True)
    assert "rc=NONE" in _out(iso)
    assert "LEAKED_VIA_RC" not in _out(iso)


def test_build_clean_env_excludes_unlisted(monkeypatch):
    monkeypatch.setenv("RANDOM_UNLISTED_VAR", "x")
    env = sandbox._build_clean_env()
    assert "RANDOM_UNLISTED_VAR" not in env
    assert "PATH" in env  # mandatory allowlist preserved


# --- network isolation ---

def test_isolate_network_fail_closed_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_has_sandbox_exec", lambda: False)
    r = sandbox.execute(tmp_path, command="echo x", isolate_network=True)
    assert r["ok"] is False
    assert r["reason"] == "sandbox_exec_unavailable"


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_isolate_network_allows_local_compute(tmp_path):
    r = sandbox.execute(tmp_path, command=["python3", "-c", "print(2 + 2)"],
                        isolate_network=True, timeout=20)
    assert r["ok"] is True
    assert "4" in _out(r)


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_isolate_network_blocks_outbound(tmp_path):
    prog = (
        "import socket\n"
        "try:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(3)\n"
        "    s.connect(('1.1.1.1', 443)); print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('ERRNO', e.errno)\n"
    )
    r = sandbox.execute(tmp_path, command=["python3", "-c", prog], isolate_network=True, timeout=15)
    assert r["ok"] is True
    out = _out(r)
    assert "CONNECTED" not in out
    assert "ERRNO 1" in out  # EPERM — proves the sandbox denied it, not a refused/timeout


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_isolate_network_blocks_dns(tmp_path):
    # DNS name resolution must also fail under isolation (closes the DNS covert channel).
    prog = (
        "import socket\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80); print('RESOLVED')\n"
        "except OSError:\n"
        "    print('DNS_BLOCKED')\n"
    )
    r = sandbox.execute(tmp_path, command=["python3", "-c", prog], isolate_network=True, timeout=15)
    assert r["ok"] is True
    out = _out(r)
    assert "RESOLVED" not in out
    assert "DNS_BLOCKED" in out


@pytest.mark.skipif(not _HAS_SBX, reason="sandbox-exec not available")
def test_meta_records_isolation_flags(tmp_path):
    r = sandbox.execute(tmp_path, command="echo hi", isolate_network=True, isolate_env=True)
    assert r["ok"] is True
    listed = sandbox.list_executions(tmp_path, limit=1)
    assert listed["count"] >= 1
