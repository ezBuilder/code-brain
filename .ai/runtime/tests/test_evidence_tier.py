"""G12: LIGHT/HEAVY evidence-tier triage in the harness directive (reuses loopd classifiers)."""
from __future__ import annotations

from pathlib import Path

from ai_core import autonomous_harness as ah


def test_evidence_tier_heavy_for_complex() -> None:
    assert ah.evidence_tier({"prompt": "refactor the auth module and prove no regression"}) == "heavy"


def test_evidence_tier_heavy_for_high_risk() -> None:
    assert ah.evidence_tier({"prompt": "deploy to production and rotate the secret token"}) == "heavy"


def test_evidence_tier_light_for_trivial() -> None:
    assert ah.evidence_tier({"prompt": "fix a typo in the readme"}) == "light"


def test_evidence_tier_safe_on_garbage() -> None:
    assert ah.evidence_tier(None) == "light"
    assert ah.evidence_tier({}) == "light"


def test_directive_appends_tier_close(tmp_path: Path) -> None:
    (tmp_path / ".ai" / "memory").mkdir(parents=True, exist_ok=True)
    heavy = ah.directive(tmp_path, explicit=True, request={"prompt": "refactor security-critical module"})
    assert "HEAVY work" in heavy and "reproducible evidence" in heavy
    light = ah.directive(tmp_path, explicit=True, request={"prompt": "fix typo"})
    assert "LIGHT work" in light
    # backward-compatible: no request → no tier close (unchanged behavior)
    assert "work" not in ah.directive(tmp_path, explicit=True).split("iterate until done/blocker.")[-1]
