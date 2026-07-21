from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_core.memory import read_jsonl_recent_bounded
from ai_core.memory_match import compact, tokenize, weighted_relevance
from ai_core.memory_recall import recall_memory
from ai_core.private_write import read_root_confined_tail_text


def _memory_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".ai" / "memory").mkdir(parents=True)
    return root


def _write_jsonl(root: Path, name: str, records: list[dict]) -> Path:
    path = root / ".ai" / "memory" / name
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def test_tokenizer_aligns_camel_snake_paths_and_unicode() -> None:
    terms = tokenize("src/ai_core/processGroup.py 메모리-회상")
    assert {"src", "ai", "core", "process", "group", "py", "메모리", "회상"} <= set(terms)
    assert tokenize("process_group") == ["process", "group"]
    assert compact("Process-Group") == compact("process_group") == "processgroup"

    relevance, fields = weighted_relevance(
        tokenize("processGroup cleanup"),
        {"text": ("bounded process_group cleanup", 1.0)},
        query_text="processGroup cleanup",
    )
    assert relevance == 1.0
    assert fields == {"text": ["process", "group", "cleanup"]}


def test_recall_exposes_relations_provenance_and_temporal_validity(tmp_path: Path) -> None:
    root = _memory_root(tmp_path)
    _write_jsonl(
        root,
        "decisions.jsonl",
        [
            {
                "id": "dec-11111111",
                "decided_at": "2026-01-01T00:00:00Z",
                "decision": "Use legacy cleanup for workers",
                "source": "operator",
            },
            {
                "id": "dec-22222222",
                "decided_at": "2026-07-01T00:00:00Z",
                "decision": "Use process_group cleanup for workerTree",
                "tags": ["SIGKILL", "runner"],
                "source": "research",
                "observed_versions": {"python": "3.11"},
                "environment": "macOS runner",
                "contradicts": "dec-11111111",
                "derives_from": "dec-11111111",
                "retest_after": "2026-06-01T00:00:00Z",
            },
            {
                "id": "dec-33333333",
                "decided_at": "2025-01-01T00:00:00Z",
                "decision": "Use process group cleanup without bounds",
                "expires_at": "2026-02-01T00:00:00Z",
            },
        ],
    )

    result = recall_memory(
        root,
        query="processGroup cleanup worker tree",
        types=["decision"],
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert result["items"][0]["ref"] == "dec-22222222"
    first = result["items"][0]
    assert first["relations"] == {
        "contradicts": "dec-11111111",
        "derives_from": "dec-11111111",
    }
    assert first["provenance"]["source"] == "research"
    assert first["provenance"]["observed_versions"] == {"python": "3.11"}
    assert first["temporal"]["retest_due"] is True
    assert result["scan"]["expired_filtered"] == 1
    assert all(item["ref"] != "dec-33333333" for item in result["items"])
    observation = result["retrieval_observation"]
    assert observation["operation"] == "memory.recall"
    assert observation["query"]["raw_included"] is False
    assert observation["results"]["returned"] == result["count"]
    assert observation["quality"]["expired_filtered"] == 1

    old = next(item for item in result["items"] if item["ref"] == "dec-11111111")
    assert old["relations"]["contradicted_by"] == ["dec-22222222"]
    assert old["relations"]["derived_by"] == ["dec-22222222"]


def test_recall_suppresses_cross_store_duplicate_text(tmp_path: Path) -> None:
    root = _memory_root(tmp_path)
    shared = "Use bounded process group cleanup"
    _write_jsonl(
        root,
        "decisions.jsonl",
        [{"id": "dec-11111111", "decided_at": "2026-07-01T00:00:00Z", "decision": shared}],
    )
    _write_jsonl(
        root,
        "procedural.jsonl",
        [
            {
                "id": "proc-11111111",
                "ts": "2026-07-01T00:00:00Z",
                "kind": "fix_pattern",
                "trigger": "process_group",
                "procedure": shared,
            }
        ],
    )

    result = recall_memory(
        root,
        query="bounded processGroup cleanup",
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert result["count"] == 1
    assert result["items"][0]["ref"] == "dec-11111111"
    assert result["scan"]["duplicates_suppressed"] == 1


def test_bounded_jsonl_tail_reports_partial_and_keeps_newest_records(
    tmp_path: Path,
) -> None:
    root = _memory_root(tmp_path)
    path = _write_jsonl(
        root,
        "decisions.jsonl",
        [
            {
                "id": f"dec-{index:08x}",
                "decision": f"record {index} {'x' * 120}",
            }
            for index in range(80)
        ],
    )

    result = read_jsonl_recent_bounded(path, max_records=3, max_bytes=1_024)

    assert result["scan"]["partial"] is True
    assert result["scan"]["record_limit_hit"] is True
    assert len(result["items"]) == 3
    assert result["items"][-1]["decision"].startswith("record 79")
    assert result["scan"]["bytes_read"] <= 1_024


def test_tail_reader_drops_partial_first_line_and_pins_initial_size(tmp_path: Path) -> None:
    root = _memory_root(tmp_path)
    path = root / ".ai" / "memory" / "sample.jsonl"
    path.write_text("first-long-line\nsecond\nthird\n", encoding="utf-8")

    text, state, metadata = read_root_confined_tail_text(
        path,
        root=root,
        max_bytes=15,
        require_private=False,
    )

    assert state.st_size == path.stat().st_size
    assert text == "second\nthird\n"
    assert metadata["partial"] is True
    assert metadata["omitted_prefix_bytes"] > 0


def test_recall_reports_store_byte_limit_without_unbounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _memory_root(tmp_path)
    records = [
        {
            "id": f"dec-{index:08x}",
            "decided_at": "2026-07-01T00:00:00Z",
            "decision": f"old unrelated record {index} {'x' * 200}",
        }
        for index in range(100)
    ]
    records.append(
        {
            "id": "dec-deadbeef",
            "decided_at": "2026-07-20T00:00:00Z",
            "decision": "Newest process_group cleanup rule",
        }
    )
    _write_jsonl(root, "decisions.jsonl", records)
    monkeypatch.setenv("AI_MEMORY_RECALL_MAX_BYTES", "16384")
    monkeypatch.setenv("AI_MEMORY_RECALL_MAX_RECORDS", "20")

    result = recall_memory(
        root,
        query="processGroup cleanup",
        types=["decision"],
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert result["items"][0]["ref"] == "dec-deadbeef"
    assert result["scan"]["partial"] is True
    scan = result["scan"]["stores"]["decisions"]
    assert scan["bytes_read"] <= 16_384
    assert scan["record_limit_hit"] is True
    assert result["retrieval_observation"]["outcome"] == "partial"
    assert result["retrieval_observation"]["sources"]["decisions"]["partial"] is True
