#!/usr/bin/env python3
from __future__ import annotations  # PEP 604 unions (str | None) on Python 3.9

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "hook-policy.json"


def load_policy() -> dict:
    return json.loads(POLICY.read_text())


def any_match(patterns: list[str], value: str) -> str | None:
    for pattern in patterns:
        if re.search(pattern, value, re.IGNORECASE):
            return pattern
    return None


def check_command(policy: dict, command: str) -> dict:
    hard = any_match(policy["hard_deny"]["commands"], command)
    if hard:
        return {"decision": "deny", "reason": f"hard-deny command pattern: {hard}"}
    approval = any_match(policy["approval_required"]["commands"], command)
    if approval:
        return {"decision": "ask", "reason": f"approval-required command pattern: {approval}"}
    return {"decision": "allow", "reason": "no policy match"}


def check_path(policy: dict, path: str) -> dict:
    hard = any_match(policy["hard_deny"]["paths"], path.replace("\\", "/"))
    if hard:
        return {"decision": "deny", "reason": f"hard-deny path pattern: {hard}"}
    return {"decision": "allow", "reason": "no policy match"}


def check_prompt(policy: dict, prompt: str) -> dict:
    lowered = prompt.lower()
    approvals = [
        keyword for keyword in policy["approval_required"]["request_keywords"]
        if keyword in lowered
    ]
    research = [
        keyword for keyword in policy["workflow_nudges"]["research_first_keywords"]
        if keyword in lowered
    ]
    verification = [
        keyword for keyword in policy["workflow_nudges"]["verification_keywords"]
        if keyword in lowered
    ]
    return {
        "decision": "nudge" if approvals or research or verification else "allow",
        "approval_keywords": approvals,
        "research_keywords": research,
        "verification_keywords": verification,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--prompt")
    args = parser.parse_args()

    policy = load_policy()
    results = []
    if args.command is not None:
        results.append({"type": "command", "input": args.command, **check_command(policy, args.command)})
    for path in args.path:
        results.append({"type": "path", "input": path, **check_path(policy, path)})
    if args.prompt is not None:
        results.append({"type": "prompt", "input": args.prompt, **check_prompt(policy, args.prompt)})

    print(json.dumps({"results": results}, ensure_ascii=False))
    if any(result["decision"] == "deny" for result in results):
        return 10
    if any(result["decision"] == "ask" for result in results):
        return 20
    return 0


if __name__ == "__main__":
    sys.exit(main())
