from __future__ import annotations

import json
from copy import deepcopy

from ai_core.doctor import check_autonomous_round_completeness
from ai_core.evidence import _candidate_record, validate_autonomous_round_record


def _complete_round() -> dict[str, object]:
    return {
        "round_id": "round-20260820-001",
        "start": {
            "sha": "3f6e9cb3878367eedb75f022d14e75e57a16cfa8",
            "branch": "develop",
            "dirty_paths": [],
        },
        "research": {
            "question": "Does the round completeness contract reject missing evidence?",
            "sources": [
                {
                    "source": "docs/WORLD_CLASS_AUTONOMOUS_UPGRADE.md",
                    "freshness": "2026-08-20",
                    "local_repro": "make test",
                }
            ],
        },
        "task": {
            "task_id": "M1-2",
            "owned_paths": [".ai/runtime/src/ai_core/evidence.py"],
            "protected_paths": ["pyproject.toml"],
            "changed_paths": [".ai/runtime/src/ai_core/evidence.py"],
            "acceptance": [
                {
                    "command": "make test",
                    "exit_code": 0,
                    "observed": "targeted regression suite passed",
                    "artifact_path": ".ai/outputs/autonomous-round-round-20260820-001.json",
                }
            ],
        },
        "reviewer": {
            "verdict": "accept",
            "evidence": [{"type": "command", "ref": "task.acceptance[0]"}],
        },
        "end": {"status": "completed", "next_trigger": "next backlog item"},
    }


def test_complete_round_is_green() -> None:
    result = validate_autonomous_round_record(_complete_round())
    assert result == {"ok": True, "round_id": "round-20260820-001", "issues": []}


def test_missing_required_links_are_red_without_reflecting_values() -> None:
    payload = deepcopy(_complete_round())
    payload["task"]["acceptance"][0].pop("artifact_path")  # type: ignore[index]
    payload["reviewer"].pop("verdict")  # type: ignore[union-attr]
    payload["task"]["protected_paths"] = ["/tmp/operator-secret"]  # type: ignore[index]

    result = validate_autonomous_round_record(payload)

    assert result["ok"] is False
    assert "task.acceptance[0].artifact_path: required_text" in result["issues"]
    assert "reviewer.verdict: required_text" in result["issues"]
    assert "task.protected_paths[0]: invalid_path" in result["issues"]
    assert "operator-secret" not in json.dumps(result)


def test_generic_candidate_evidence_remains_compatible() -> None:
    record = _candidate_record(
        query="round completeness",
        result={"path": "src/example.py", "snippet": "generic search evidence"},
        source="search",
        rank=1,
        observed_at="2026-08-20T00:00:00Z",
    )

    assert record is not None
    assert record["status"] == "candidate"
    assert record["path"] == "src/example.py"
    assert "round_id" not in record


def test_doctor_accepts_complete_typed_report(tmp_path) -> None:
    outputs = tmp_path / ".ai" / "outputs"
    outputs.mkdir(parents=True)
    report = outputs / "autonomous-round-round-20260820-001.json"
    report.write_text(json.dumps(_complete_round()), encoding="utf-8")

    check = check_autonomous_round_completeness(tmp_path)

    assert check.ok is True
    assert check.detail == "reports=1 complete"


def test_doctor_rejects_incomplete_typed_report(tmp_path) -> None:
    outputs = tmp_path / ".ai" / "outputs"
    outputs.mkdir(parents=True)
    payload = _complete_round()
    payload["end"].pop("next_trigger")  # type: ignore[union-attr]
    report = outputs / "autonomous-round-round-20260820-001.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    check = check_autonomous_round_completeness(tmp_path)

    assert check.ok is False
    assert "end.next_trigger: required_text" in check.detail