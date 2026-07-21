from __future__ import annotations

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import doctor, loss_accounting, memory, obs, retention, sandbox


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".ai").mkdir(parents=True)
    return root


def test_empty_summary_is_bounded_and_healthy(tmp_path: Path) -> None:
    root = _root(tmp_path)

    payload = loss_accounting.summary(root)

    assert payload["ok"] is True
    assert payload["bounded"] is True
    assert payload["observed"] is False
    assert payload["reason"] == "no_loss_events"
    assert payload["totals"]["events"] == 0
    assert not loss_accounting.accounting_path(root).exists()


def test_applied_events_accumulate_exact_totals_and_private_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = loss_accounting.loss_event(
        domain="test_retention",
        operation="first",
        applied=True,
        files_before=5,
        files_after=3,
        bytes_before=1000,
        bytes_after=400,
        records_before=20,
        records_after=7,
        reasons={"age_limit": 2},
        examples=("old-a", "old-b"),
    )
    second = loss_accounting.loss_event(
        domain="test_retention",
        operation="second",
        applied=True,
        files_before=3,
        files_after=2,
        bytes_before=400,
        bytes_after=250,
        records_before=7,
        records_after=5,
        reasons={"byte_limit": 1},
    )

    assert loss_accounting.finalize_event(root, first)["accounting"]["recorded"] is True
    assert loss_accounting.finalize_event(root, second)["accounting"]["recorded"] is True
    payload = loss_accounting.summary(root)

    assert payload["totals"] == {
        "events": 2,
        "applied_events": 2,
        "removed_files": 3,
        "removed_bytes": 750,
        "removed_records": 15,
        "error_events": 0,
    }
    domain = payload["domains"]["test_retention"]
    assert domain["removed_files"] == 3
    assert domain["removed_bytes"] == 750
    assert domain["removed_records"] == 15
    assert domain["reasons"] == {"age_limit": 2, "byte_limit": 1}
    path = loss_accounting.accounting_path(root)
    assert path.stat().st_size <= loss_accounting.MAX_BYTES
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dry_run_does_not_mutate_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    event = loss_accounting.loss_event(
        domain="dry_run_test",
        operation="preview",
        applied=False,
        dry_run=True,
        files_before=4,
        files_after=1,
        bytes_before=400,
        bytes_after=100,
        reasons={"file_limit": 3},
    )

    result = loss_accounting.finalize_event(root, event)

    assert result["files"]["removed"] == 3
    assert result["bytes"]["removed"] == 300
    assert result["accounting"] == {"ok": True, "recorded": False, "reason": "dry_run"}
    assert not loss_accounting.accounting_path(root).exists()


def test_error_count_is_not_limited_to_examples(tmp_path: Path) -> None:
    root = _root(tmp_path)
    errors = [f"failure-{index}" for index in range(25)]
    event = loss_accounting.loss_event(
        domain="error_test",
        operation="failed-prune",
        applied=False,
        errors=errors,
    )

    assert event["error_count"] == 25
    assert len(event["errors"]) == loss_accounting.MAX_EXAMPLES
    result = loss_accounting.finalize_event(root, event)
    assert result["accounting"]["recorded"] is True
    totals = loss_accounting.summary(root)["totals"]
    assert totals["events"] == 1
    assert totals["applied_events"] == 0
    assert totals["error_events"] == 1


def test_snapshot_last_event_is_fixed_size(tmp_path: Path) -> None:
    root = _root(tmp_path)
    event = loss_accounting.loss_event(
        domain="bounded_last_event",
        operation="operation-" + "o" * 1000,
        applied=False,
        errors=(f"error-{index}-" + "e" * 1000 for index in range(25)),
        examples=(f"example-{index}-" + "x" * 1000 for index in range(25)),
    )

    result = loss_accounting.finalize_event(root, event)
    payload = loss_accounting.summary(root)
    last = payload["domains"]["bounded_last_event"]["last_event"]

    assert result["accounting"]["recorded"] is True
    assert len(last["errors"]) == loss_accounting.SNAPSHOT_LAST_ERRORS
    assert len(last["examples"]) == loss_accounting.SNAPSHOT_LAST_EXAMPLES
    assert len(last["operation"]) <= 120
    assert loss_accounting.accounting_path(root).stat().st_size < loss_accounting.MAX_BYTES


def test_concurrent_events_do_not_lose_updates(tmp_path: Path) -> None:
    root = _root(tmp_path)

    def write(index: int) -> bool:
        event = loss_accounting.loss_event(
            domain="concurrent_test",
            operation=f"event-{index}",
            applied=True,
            bytes_before=100,
            bytes_after=99,
            reasons={"bounded_test": 1},
        )
        return loss_accounting.finalize_event(root, event)["accounting"]["recorded"] is True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(40)))

    payload = loss_accounting.summary(root)
    assert all(results)
    assert payload["totals"]["events"] == 40
    assert payload["totals"]["applied_events"] == 40
    assert payload["totals"]["removed_bytes"] == 40
    assert payload["domains"]["concurrent_test"]["reasons"] == {"bounded_test": 40}


def test_many_domains_with_long_events_remain_under_snapshot_cap(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for index in range(loss_accounting.MAX_DOMAINS):
        event = loss_accounting.loss_event(
            domain=f"domain_{index}",
            operation="operation-" + "o" * 1000,
            applied=False,
            errors=("error-" + "e" * 1000 for _ in range(20)),
            examples=("example-" + "x" * 1000 for _ in range(20)),
        )
        assert loss_accounting.finalize_event(root, event)["accounting"]["recorded"] is True

    path = loss_accounting.accounting_path(root)
    payload = loss_accounting.summary(root)
    assert payload["ok"] is True
    assert len(payload["domains"]) == loss_accounting.MAX_DOMAINS
    assert path.stat().st_size < loss_accounting.MAX_BYTES


def test_corrupt_snapshot_fails_doctor(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = loss_accounting.accounting_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("{not-json\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)

    payload = loss_accounting.summary(root)
    check = doctor.check_loss_accounting(root)

    assert payload["ok"] is False
    assert payload["reason"] == "invalid_json"
    assert check.ok is False


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink trust boundary")
def test_symlink_snapshot_is_rejected_without_touching_target(tmp_path: Path) -> None:
    root = _root(tmp_path)
    external = tmp_path / "external.json"
    external.write_text('{"keep": true}\n', encoding="utf-8")
    path = loss_accounting.accounting_path(root)
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    payload = loss_accounting.summary(root)

    assert payload["ok"] is False
    assert external.read_text(encoding="utf-8") == '{"keep": true}\n'


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_snapshot_is_rejected_without_touching_target(tmp_path: Path) -> None:
    root = _root(tmp_path)
    event = loss_accounting.loss_event(
        domain="hardlink_test",
        operation="seed",
        applied=True,
        bytes_before=2,
        bytes_after=1,
    )
    assert loss_accounting.finalize_event(root, event)["accounting"]["recorded"] is True
    path = loss_accounting.accounting_path(root)
    content = path.read_bytes()
    path.unlink()
    external = tmp_path / "external-loss.json"
    external.write_bytes(content)
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, path)

    payload = loss_accounting.summary(root)

    assert payload["ok"] is False
    assert "hard" in str(payload["reason"]).lower() or "link" in str(payload["reason"]).lower()
    assert external.read_bytes() == content


def test_oversized_snapshot_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = loss_accounting.accounting_path(root)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b"x" * (loss_accounting.MAX_BYTES + 1))
    if os.name != "nt":
        path.chmod(0o600)

    payload = loss_accounting.summary(root)
    check = doctor.check_loss_accounting(root)

    assert payload["ok"] is False
    assert check.ok is False


def test_retention_prune_reports_actual_files_bytes_and_reasons(tmp_path: Path) -> None:
    root = _root(tmp_path)
    directory = root / ".ai" / "cache" / "diagnostics"
    directory.mkdir(parents=True)
    for index, size in enumerate((100, 200, 300)):
        path = directory / f"diagnostics-{index}.zip"
        path.write_bytes(b"x" * size)
        os.utime(path, (time.time() + index, time.time() + index))

    result = retention.prune_directory(
        root,
        directory,
        prefixes=("diagnostics-",),
        suffixes=(".zip",),
        keep_days=3650,
        max_files=1,
        max_bytes=10_000,
        accounting_domain="diagnostics_retention",
    )

    assert result["ok"] is True
    assert result["removed_count"] == 2
    assert result["removed_bytes"] == 300
    loss = result["loss"]
    assert loss["files"] == {"before": 3, "after": 1, "removed": 2}
    assert loss["bytes"] == {"before": 600, "after": 300, "removed": 300}
    assert loss["reasons"]["file_limit"] == 2
    assert loss["accounting"]["recorded"] is True
    totals = loss_accounting.summary(root)["domains"]["diagnostics_retention"]
    assert totals["removed_files"] == 2
    assert totals["removed_bytes"] == 300


def test_jsonl_rotation_reports_removed_records_and_bytes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / ".ai" / "memory" / "events.jsonl"
    for index in range(20):
        memory.append_jsonl(path, {"id": index, "payload": "x" * 40})
    before = path.stat().st_size

    result = memory.rotate_jsonl_tail(path, max_bytes=300, keep_lines=3)

    assert result["ok"] is True
    assert result["rotated"] is True
    assert result["loss"]["records"]["removed"] == 17
    assert result["loss"]["bytes"]["before"] == before
    assert result["loss"]["bytes"]["after"] == path.stat().st_size
    assert result["loss"]["accounting"]["recorded"] is True
    summary = loss_accounting.summary(root)
    assert summary["domains"]["jsonl_rotation"]["removed_records"] == 17


def test_sandbox_prune_accounts_for_meta_and_output_bytes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = sandbox.execute(root, command=["echo", "old-output"])
    assert result["ok"] is True
    exec_id = result["exec_id"]
    directory = root / ".ai" / "cache" / "sandbox"
    meta = directory / f"{exec_id}.meta.json"
    output = directory / f"{exec_id}.txt"
    payload = json.loads(meta.read_text(encoding="utf-8"))
    payload["created_at"] = "2020-01-01T00:00:00Z"
    meta.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - 3600
    os.utime(meta, (old, old))
    os.utime(output, (old, old))
    before_bytes = meta.stat().st_size + output.stat().st_size

    pruned = sandbox.prune(root, older_than_seconds=10)

    assert pruned["ok"] is True
    assert pruned["removed_count"] == 1
    assert pruned["loss"]["files"]["removed"] == 2
    assert pruned["loss"]["bytes"]["removed"] == before_bytes
    assert pruned["loss"]["accounting"]["recorded"] is True
    assert loss_accounting.summary(root)["domains"]["sandbox_prune"]["removed_bytes"] == before_bytes


def test_log_payload_truncation_records_removed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(obs, "LOG_PAYLOAD_MAX_BYTES", 400)

    result = obs.write_log(root, "info", "large-payload", {"blob": "z" * 5000})

    assert result["record"]["payload"]["truncated"] is True
    assert result["payload_loss"] is not None
    assert result["payload_loss"]["bytes"]["removed"] > 0
    domain = loss_accounting.summary(root)["domains"]["payload_truncation"]
    assert domain["events"] == 1
    assert domain["removed_bytes"] == result["payload_loss"]["bytes"]["removed"]
