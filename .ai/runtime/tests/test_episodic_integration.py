from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_core import episodic_runtime, hooks, mcp_server, memory, memory_tier


ROOT = Path(__file__).resolve().parents[3]


def _seed_audit(root: Path, count: int = 120) -> None:
    path = root / ".ai" / "memory" / "audit" / "2026.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / ".ai" / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    rows: list[str] = []
    previous_sha: str | None = None
    for index in range(count):
        row = {
            "ts": f"2026-01-01T00:00:{index % 60:02d}Z",
            "event_id": f"evt-{index:032x}",
            "action": "test.event",
            "category": "test",
            "payload": {"index": index},
            "prev_sha": previous_sha,
        }
        line = json.dumps(row, sort_keys=True, separators=(",", ":"))
        rows.append(line)
        previous_sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
    path.write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_cli_build_status_context_and_drilldown(tmp_path: Path) -> None:
    _seed_audit(tmp_path)
    env = os.environ.copy()
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT / ".ai" / "runtime" / "src")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ai_core.cli", *args],
            cwd=tmp_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    built = run("memory", "episodic", "build", "--json")
    status = run("memory", "episodic", "status", "--json")
    context = run("memory", "episodic", "context", "--byte-budget", "2000", "--json")
    drill = run("memory", "episodic", "drill-down", "--start", "10", "--end", "12", "--json")

    assert built.returncode == status.returncode == context.returncode == drill.returncode == 0
    assert json.loads(status.stdout)["ready"] is True
    assert json.loads(context.stdout)["authoritative"] is False
    assert json.loads(drill.stdout)["events"][0]["source"]["line"] == 11


def test_mcp_tools_are_read_only_and_resolve_raw_rows(tmp_path: Path) -> None:
    _seed_audit(tmp_path)
    episodic_runtime.build_audit_index(tmp_path)

    context = mcp_server._dispatch_tool(
        tmp_path, "episodic_context", {"byte_budget": 2_000, "raw_tail": 5}
    )
    drill = mcp_server._dispatch_tool(
        tmp_path, "episodic_drilldown", {"start": 3, "end": 5, "limit": 10}
    )

    assert context["ok"] is True and context["authoritative"] is False
    assert drill["authoritative"] is True
    assert [event["index"] for event in drill["events"]] == [3, 4]
    assert {"episodic_context", "episodic_drilldown"} <= mcp_server._READ_ONLY_TOOLS


def test_session_start_reads_only_bounded_prebuilt_cache(tmp_path: Path, monkeypatch) -> None:
    _seed_audit(tmp_path)
    episodic_runtime.build_audit_index(tmp_path)
    monkeypatch.setattr(
        episodic_runtime,
        "load_audit_corpus",
        lambda _root: (_ for _ in ()).throw(AssertionError("hot path scanned raw audit")),
    )

    sections = hooks._build_auxiliary_sections("SessionStart", {}, tmp_path)

    life = next(section for section in sections if section.startswith("cb-life:"))
    assert len(life.encode("utf-8")) <= episodic_runtime.HOOK_CONTEXT_MAX_BYTES
    assert hooks._is_protected_section(life) is True


def test_page_out_builds_index_offline_and_status_detects_tamper(
    tmp_path: Path, monkeypatch
) -> None:
    _seed_audit(tmp_path)
    monkeypatch.setenv("AI_MEMORY_CONFLICT_SCAN", "0")

    result = memory_tier.page_out(tmp_path)
    assert result["episodic_memory"]["ok"] is True
    assert episodic_runtime.read_hook_context(tmp_path).startswith("cb-life:")

    path = memory.audit_path(tmp_path)
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"test.event", b"test.tampr", 1))

    status = episodic_runtime.status(tmp_path)
    assert status["ok"] is False
    assert status["integrity_ok"] is False


def test_episodic_index_reads_lossless_audit_segments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    written = [
        memory.append_audit(
            tmp_path,
            action="test.segmented",
            category="test",
            payload={"index": index, "padding": "x" * 80},
        )
        for index in range(40)
    ]

    built = episodic_runtime.build_audit_index(tmp_path)
    corpus = episodic_runtime.load_audit_corpus(tmp_path)
    persisted_ids = {
        event.event_id for event in corpus.events if event.raw.get("action") == "test.segmented"
    }

    assert built["ok"] is True
    assert persisted_ids == {record["event_id"] for record in written}
    assert len(memory.all_audit_files(tmp_path)) > 1


@pytest.mark.parametrize("missing_position", [0, 1], ids=["first", "middle"])
def test_episodic_loader_rejects_deleted_raw_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_position: int
) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    for index in range(60):
        memory.append_audit(
            tmp_path,
            action="test.segment.loss",
            category="test",
            payload={"index": index, "padding": "x" * 80},
        )
    segments = [
        path
        for path in memory.all_audit_files(tmp_path)
        if (memory._audit_file_sort_key(path.name) or (0, 1, 0))[1] == 0
    ]
    assert len(segments) >= 3
    segments[missing_position].unlink()

    with pytest.raises(episodic_runtime.EpisodicRuntimeError, match="segment sequence"):
        episodic_runtime.load_audit_corpus(tmp_path)


def test_legacy_ids_survive_current_file_segmentation(tmp_path: Path, monkeypatch) -> None:
    current = memory.audit_path(tmp_path)
    current.parent.mkdir(parents=True)
    rows = [
        {
            "ts": f"2026-01-01T00:00:{index:02d}Z",
            "action": "legacy.event",
            "category": "test",
            "payload": {"index": index},
        }
        for index in range(20)
    ]
    current.write_text(
        "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    current.chmod(0o600)
    before = episodic_runtime.load_audit_corpus(tmp_path)
    legacy_ids = [event.event_id for event in before.events]
    assert episodic_runtime.build_audit_index(tmp_path)["ok"] is True

    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", current.stat().st_size)
    memory.append_audit(tmp_path, action="after.segment", category="test", payload={})

    after = episodic_runtime.load_audit_corpus(tmp_path)
    retained = [event.event_id for event in after.events if event.raw.get("action") == "legacy.event"]
    rebuilt = episodic_runtime.build_audit_index(tmp_path)

    assert retained == legacy_ids
    assert rebuilt["ok"] is True
    drill = episodic_runtime.drilldown_payload(tmp_path, event_id=legacy_ids[0])
    assert drill["events"][0]["source"]["path"].endswith(".jsonl")
    assert ".000001." in drill["events"][0]["source"]["path"]


def test_episodic_loader_rejects_duplicate_segment_sequence(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    for action in ("branch-a", "branch-b"):
        content = json.dumps({"action": action}, separators=(",", ":")) + "\n"
        digest = hashlib.sha256(content.encode()).hexdigest()[:12]
        (audit_dir / f"2026.000001.{digest}.jsonl").write_text(content, encoding="utf-8")

    with pytest.raises(episodic_runtime.EpisodicRuntimeError, match="duplicate audit segment"):
        episodic_runtime.load_audit_corpus(tmp_path)
