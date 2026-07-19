from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
