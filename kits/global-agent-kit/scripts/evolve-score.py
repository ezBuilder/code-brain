#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "code-brain-global-kit" / "evolution"
POSITIVE = {"adopt", "pass", "passed", "success", "verified", "repeat", "repeated", "useful", "saved"}
NEGATIVE = {"fail", "failed", "broken", "stale", "noisy", "reject", "regression"}
RISKY = {"secret", "credential", "token", "oauth", "billing", "production", "prod", "deploy", "destructive", "delete", "remote"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score captured evolution events into concise candidate decisions.")
    parser.add_argument("--events", type=Path, default=STATE_DIR / "events.jsonl", help="Path to captured JSONL events")
    parser.add_argument("--candidate", help="Score only one candidate")
    parser.add_argument("--limit", type=int, default=10, help="Maximum candidates to return")
    parser.add_argument("--self-test", action="store_true", help="Run local scoring self-test")
    return parser.parse_args()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9가-힣_-]+", text.lower()))


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                events.append(item)
    return events


def candidate_key(event: dict[str, Any]) -> str:
    candidate = str(event.get("candidate") or "").strip()
    if candidate:
        return candidate[:120]
    signal = str(event.get("signal") or "").strip()
    note = str(event.get("note") or "").strip()
    fallback = " ".join(part for part in (signal, note) if part).strip()
    return fallback[:80] or "uncategorized"


def score_group(name: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    positive = 0
    negative = 0
    risky = 0
    confidence_hints: list[float] = []
    risk_hints: list[float] = []
    token_values: list[int] = []

    for event in events:
        text = " ".join(str(event.get(field) or "") for field in ("event", "source", "candidate", "signal", "note"))
        tags = " ".join(str(tag) for tag in event.get("tags") or [])
        bag = words(f"{text} {tags}")
        positive += len(bag & POSITIVE)
        negative += len(bag & NEGATIVE)
        risky += len(bag & RISKY)
        if isinstance(event.get("confidence_hint"), (int, float)):
            confidence_hints.append(float(event["confidence_hint"]))
        if isinstance(event.get("risk_hint"), (int, float)):
            risk_hints.append(float(event["risk_hint"]))
        if isinstance(event.get("token_estimate"), int) and event["token_estimate"] > 0:
            token_values.append(int(event["token_estimate"]))

    volume = min(len(events), 5) / 5
    signal_balance = (positive - negative) / max(positive + negative, 1)
    hinted_confidence = sum(confidence_hints) / len(confidence_hints) if confidence_hints else 0.5
    confidence = clamp(0.35 + 0.25 * volume + 0.25 * signal_balance + 0.15 * hinted_confidence)

    hinted_risk = sum(risk_hints) / len(risk_hints) if risk_hints else 0.25
    risk = clamp(0.15 + 0.2 * negative + 0.25 * risky + 0.25 * hinted_risk)

    avg_tokens = sum(token_values) / len(token_values) if token_values else 800 + 250 * len(events)
    token_budget = int(min(6000, max(400, math.ceil(avg_tokens / 100) * 100)))

    decision = "promote" if confidence >= 0.65 and risk <= 0.45 else "reject"
    reasons: list[str] = []
    if positive:
        reasons.append(f"positive_signals={positive}")
    if negative:
        reasons.append(f"negative_signals={negative}")
    if risky:
        reasons.append(f"risk_terms={risky}")
    if confidence_hints:
        reasons.append("confidence_hint")
    if risk_hints:
        reasons.append("risk_hint")
    if not reasons:
        reasons.append("insufficient_signal")

    return {
        "candidate": name,
        "events": len(events),
        "token_budget": token_budget,
        "confidence": round(confidence, 2),
        "risk": round(risk, 2),
        "decision": decision,
        "reasons": reasons[:4],
    }


def score(events: list[dict[str, Any]], only_candidate: str | None, limit: int) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = candidate_key(event)
        if only_candidate and key != only_candidate:
            continue
        grouped[key].append(event)

    candidates = [score_group(name, group) for name, group in grouped.items()]
    candidates.sort(key=lambda item: (item["decision"] != "promote", -item["confidence"], item["risk"], item["candidate"]))
    if limit > 0:
        candidates = candidates[:limit]

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "events": len(events),
        "candidates": candidates,
    }


def self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.jsonl"
        rows = [
            {"candidate": "short context scorer", "signal": "verified pass repeated", "token_estimate": 900, "confidence_hint": 0.9, "risk_hint": 0.1},
            {"candidate": "short context scorer", "signal": "success useful", "token_estimate": 700, "confidence_hint": 0.8, "risk_hint": 0.1},
            {"candidate": "prod deploy helper", "signal": "oauth production risky", "confidence_hint": 0.5, "risk_hint": 0.9},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        result = score(load_events(path), None, 10)
        by_name = {item["candidate"]: item for item in result["candidates"]}
        if by_name["short context scorer"]["decision"] != "promote":
            raise SystemExit("expected safe repeated candidate to promote")
        if by_name["prod deploy helper"]["decision"] != "reject":
            raise SystemExit("expected risky candidate to reject")
    print("evolve-score self-test ok")


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return
    events = load_events(args.events)
    print(json.dumps(score(events, args.candidate, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
