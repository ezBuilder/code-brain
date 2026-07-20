from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import process_janitor  # noqa: E402
from ai_core.process_janitor import cleanup_children, register_child, registry_path  # noqa: E402


def test_register_child_writes_redacted_shape(tmp_path: Path) -> None:
    register_child(tmp_path, pid=12345, kind="test", command=["ai", "index", "rebuild"])

    rows = [json.loads(line) for line in registry_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["pid"] == 12345
    assert rows[0]["kind"] == "test"
    assert rows[0]["command"] == ["ai", "index", "rebuild"]
    assert "identity" in rows[0]


def test_register_child_redacts_secret_command_arguments(tmp_path: Path) -> None:
    secret = "ghp_" + "a" * 36

    register_child(
        tmp_path,
        pid=12345,
        kind="test",
        command=["worker", f"--token={secret}"],
    )

    raw = registry_path(tmp_path).read_text(encoding="utf-8")
    row = json.loads(raw)
    assert secret not in raw
    assert "[REDACTED]" in row["command"][1]


def test_cleanup_children_terminates_stale_registered_child(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        register_child(tmp_path, pid=proc.pid, kind="sleep", command=["python", "sleep"])
        path = registry_path(tmp_path)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["created_at"] = time.time() - 3600
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

        result = cleanup_children(tmp_path, ttl_seconds=1)

        assert result["killed"] == 1
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_cleanup_never_terminates_reused_pid(tmp_path: Path, monkeypatch) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        json.dumps(
            {
                "pid": 424242,
                "kind": "old-child",
                "command": ["worker"],
                "created_at": time.time() - 3600,
                "identity": "old-process-identity",
            }
        )
        + "\n",
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "new-process-identity")
    monkeypatch.setattr(
        process_janitor,
        "_terminate",
        lambda _pid: (_ for _ in ()).throw(AssertionError("reused PID must not be terminated")),
    )

    result = cleanup_children(tmp_path, ttl_seconds=1)

    assert result["killed"] == 0
    assert result["reused"] == 1
    assert result["alive"] == 0


def test_cleanup_never_terminates_legacy_record_without_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        json.dumps(
            {
                "pid": 424242,
                "kind": "legacy-child",
                "command": ["worker"],
                "created_at": time.time() - 3600,
            }
        )
        + "\n",
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "current-identity")
    monkeypatch.setattr(
        process_janitor,
        "_terminate",
        lambda _pid: (_ for _ in ()).throw(AssertionError("unverified PID must not be terminated")),
    )

    result = cleanup_children(tmp_path, ttl_seconds=1)

    assert result["killed"] == 0
    assert result["unverified"] == 1
    assert result["alive"] == 1


def test_concurrent_register_and_cleanup_never_lose_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pid = 424242
    identity = "stable-process-identity"
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: identity)
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)

    def register(index: int) -> None:
        register_child(
            tmp_path,
            pid=pid,
            kind=f"worker-{index}",
            command=["worker", str(index)],
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(register, index) for index in range(30)]
        futures.append(pool.submit(cleanup_children, tmp_path, ttl_seconds=999999))
        for future in futures:
            future.result()

    rows = [
        json.loads(line)
        for line in registry_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 30
    assert {row["kind"] for row in rows} == {f"worker-{index}" for index in range(30)}


def test_pidfd_termination_targets_verified_process_instance(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(process_janitor.os, "pidfd_open", lambda pid, flags: calls.append(("open", pid, flags)) or 99, raising=False)
    monkeypatch.setattr(
        process_janitor.signal,
        "pidfd_send_signal",
        lambda fd, sig: calls.append(("signal", fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(process_janitor.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "expected")
    monkeypatch.setattr(
        process_janitor,
        "_terminate",
        lambda _pid: (_ for _ in ()).throw(AssertionError("pidfd path must not use numeric kill")),
    )

    assert process_janitor._terminate_if_identity_matches(123, "expected") is True
    assert calls == [
        ("open", 123, 0),
        ("signal", 99, process_janitor.signal.SIGTERM),
        ("close", 99),
    ]


def test_pidfd_termination_refuses_identity_mismatch(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(process_janitor.os, "pidfd_open", lambda _pid, _flags: 77, raising=False)
    monkeypatch.setattr(
        process_janitor.signal,
        "pidfd_send_signal",
        lambda fd, sig: calls.append(("signal", fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(process_janitor.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "different")

    assert process_janitor._terminate_if_identity_matches(123, "expected") is False
    assert calls == [("close", 77)]


def test_cleanup_skips_malformed_registry_rows_without_crashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        "not-json\n"
        + json.dumps(["not", "a", "record"])
        + "\n"
        + json.dumps({"pid": "not-an-int"})
        + "\n"
        + json.dumps(
            {
                "pid": 424242,
                "identity": "stable",
                "created_at": "not-a-time",
            }
        )
        + "\n",
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "stable")

    result = cleanup_children(tmp_path, ttl_seconds=1)

    assert result["ok"] is True
    assert result["malformed"] == 4
    assert result["killed"] == 0


def test_register_child_caps_registry_to_recent_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(process_janitor, "_process_identity", lambda pid: f"identity-{pid}")

    for index in range(150):
        register_child(
            tmp_path,
            pid=10000 + index,
            kind=f"worker-{index}",
            command=["worker", str(index)],
        )

    rows = [
        json.loads(line)
        for line in registry_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == process_janitor.REGISTRY_MAX_RECORDS
    assert rows[0]["kind"] == "worker-50"
    assert rows[-1]["kind"] == "worker-149"


def test_register_child_replaces_oversized_or_malformed_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        "x" * (process_janitor.REGISTRY_MAX_BYTES + 1),
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "identity")

    register_child(tmp_path, pid=12345, kind="fresh", command=["worker"])

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "fresh"


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_register_child_rejects_external_parent_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)

    register_child(root, pid=12345, kind="blocked", command=["worker"])

    assert not (external / "cache").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_register_child_replaces_linked_registry_without_external_write(
    tmp_path: Path,
    monkeypatch,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    path = registry_path(root)
    path.parent.mkdir(parents=True)
    external = tmp_path / f"external-registry-{link_kind}.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        path.symlink_to(external)
    else:
        os.link(external, path)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "identity")

    register_child(root, pid=12345, kind="fresh", command=["worker"])

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["fresh"]
    assert not path.is_symlink()
    assert path.stat().st_nlink == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix mode semantics")
def test_register_child_replaces_public_registry_with_private_bounded_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"pid":1,"created_at":1}\n', encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "identity")

    register_child(tmp_path, pid=12345, kind="fresh", command=["worker"])

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["fresh"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_cleanup_rejects_linked_registry_without_rewrite(
    tmp_path: Path,
    link_kind: str,
) -> None:
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    external = tmp_path / f"external-cleanup-{link_kind}.jsonl"
    external.write_text('{"pid":123,"created_at":1}\n', encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        path.symlink_to(external)
    else:
        os.link(external, path)

    result = cleanup_children(tmp_path, ttl_seconds=1)

    assert result == {"ok": False, "reason": "registry_unreadable"}
    assert external.read_text(encoding="utf-8") == '{"pid":123,"created_at":1}\n'


def test_cleanup_normalizes_far_future_timestamp_then_expires_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        json.dumps(
            {
                "pid": 424242,
                "kind": "future-child",
                "command": ["worker"],
                "created_at": 87_400.0,
                "identity": "stable",
            }
        )
        + "\n",
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "stable")
    termination_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        process_janitor,
        "_terminate_if_identity_matches",
        lambda pid, identity: termination_calls.append((pid, identity)) or True,
    )
    monkeypatch.setattr(process_janitor.time, "time", lambda: 1000.0)

    first = cleanup_children(tmp_path, ttl_seconds=10)

    assert first["clock_skew"] == 1
    assert first["alive"] == 1
    assert first["killed"] == 0
    assert termination_calls == []
    normalized = json.loads(path.read_text(encoding="utf-8"))
    assert normalized["created_at"] == 1000.0

    monkeypatch.setattr(process_janitor.time, "time", lambda: 1011.0)
    second = cleanup_children(tmp_path, ttl_seconds=10)

    assert second["clock_skew"] == 0
    assert second["killed"] == 1
    assert termination_calls == [(424242, "stable")]


def test_cleanup_rejects_nonfinite_timestamps(tmp_path: Path, monkeypatch) -> None:
    path = registry_path(tmp_path)
    process_janitor.atomic_write_private_text(
        path,
        "".join(
            json.dumps(
                {
                    "pid": pid,
                    "kind": "invalid-time",
                    "command": ["worker"],
                    "created_at": created_at,
                    "identity": "stable",
                }
            )
            + "\n"
            for pid, created_at in [(111, float("nan")), (222, float("inf"))]
        ),
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "stable")

    result = cleanup_children(tmp_path, ttl_seconds=10)

    assert result["malformed"] == 2
    assert result["alive"] == 0
    assert path.read_text(encoding="utf-8") == ""


def test_cleanup_rewrites_kept_record_to_bounded_redacted_shape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = registry_path(tmp_path)
    secret = "ghp_" + "z" * 36
    process_janitor.atomic_write_private_text(
        path,
        json.dumps(
            {
                "pid": 424242,
                "kind": "k" * 500,
                "command": [f"--token={secret}"] * 30,
                "created_at": 1000.0,
                "identity": "i" * 500,
                "extra": "discarded",
            }
        )
        + "\n",
        root=tmp_path,
    )
    monkeypatch.setattr(process_janitor.time, "time", lambda: 1001.0)
    monkeypatch.setattr(process_janitor, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_janitor, "_process_identity", lambda _pid: "i" * 500)

    result = cleanup_children(tmp_path, ttl_seconds=100)

    assert result["alive"] == 1
    raw = path.read_text(encoding="utf-8")
    row = json.loads(raw)
    assert set(row) == {"pid", "kind", "command", "created_at", "identity"}
    assert len(row["kind"]) == 64
    assert len(row["command"]) == 12
    assert len(row["identity"]) == 128
    assert secret not in raw
    assert "[REDACTED]" in raw
