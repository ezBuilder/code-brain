from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_core.redact import SECRET_PATTERNS, contains_secret, redact_text
from ai_core.search import query, rebuild


@dataclass(frozen=True)
class SecretCase:
    marker: str
    value: str
    needle: str


def _body(label: str, length: int = 24, *, upper: bool = False) -> str:
    seed = (label + "Q7") if not upper else (label + "Q7").upper()
    return (seed * ((length // len(seed)) + 1))[:length]


def _secret_cases() -> list[SecretCase]:
    aws_body = _body("aws", 16, upper=True)
    github_body = _body("hub", 24)
    fine_grained_body = _body("fine", 24)
    model_body = _body("model", 24)
    slack_body = _body("slack", 24)
    bearer_body = _body("bearer", 24)
    assignment_body = _body("assign", 24)
    private_body = _body("private", 32)
    return [
        SecretCase("ParityAwsMarker", "AK" + "IA" + aws_body, aws_body),
        SecretCase("ParityGithubMarker", "gh" + "p_" + github_body, github_body),
        SecretCase(
            "ParityFineGrainedMarker",
            "github_" + "pat_" + fine_grained_body,
            fine_grained_body,
        ),
        SecretCase("ParityModelMarker", "s" + "k-" + model_body, model_body),
        SecretCase("ParitySlackMarker", "xox" + "b-" + slack_body, slack_body),
        SecretCase(
            "ParityBearerMarker",
            "Author" + "ization: " + "Bear" + "er " + bearer_body,
            bearer_body,
        ),
        SecretCase(
            "ParityAssignmentMarker",
            "api" + "_key=" + assignment_body,
            assignment_body,
        ),
        SecretCase(
            "ParityPrivateKeyMarker",
            "-----BEGIN "
            + "PRIVATE "
            + "KEY-----\n"
            + private_body
            + "\n-----END "
            + "PRIVATE "
            + "KEY-----",
            private_body,
        ),
    ]


def _near_misses() -> list[str]:
    return [
        "AK" + "IA" + _body("aws", 15, upper=True),
        "gh" + "p_" + _body("hub", 19),
        "github_" + "pat_" + _body("fine", 19),
        "s" + "k-" + _body("model", 19),
        "xox" + "b-" + _body("slack", 19),
        "Author" + "ization: Basic " + _body("basic", 24),
        "api" + "_key=" + _body("assign", 19),
        "-----BEGIN " + "PRIVATE " + "KEY-----\n" + _body("private", 32),
    ]


def _canonical_match(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in SECRET_PATTERNS)


def test_optimized_secret_matcher_and_redactor_match_canonical_patterns() -> None:
    positives = [case.value for case in _secret_cases()]
    corpus = positives + _near_misses() + [
        "safe source text",
        "to" + "ken-name-without-assignment",
        "\u017f" + "ecret=" + _body("unicode", 24),
    ]

    for value in corpus:
        expected = _canonical_match(value)
        assert contains_secret(value) is expected
        redacted = redact_text(value)
        if expected:
            assert value not in redacted
            assert "[REDACTED]" in redacted
            assert contains_secret(redacted) is False
        else:
            assert redacted == value


def test_search_index_and_snippets_never_reintroduce_detected_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config = repo / ".ai" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("project_name: parity\n", encoding="utf-8")
    source = repo / "src" / "parity-fixture.txt"
    source.parent.mkdir(parents=True)
    cases = _secret_cases()
    source.write_text(
        "\n".join(f"{case.marker} {case.value}" for case in cases) + "\n",
        encoding="utf-8",
    )

    rebuilt = rebuild(repo)
    assert rebuilt["indexed"] == 2

    for case in cases:
        visible = query(repo, case.marker)
        serialized = json.dumps(visible, sort_keys=True)
        assert visible["results"]
        assert case.value not in serialized
        assert case.needle not in serialized
        assert "[REDACTED]" in serialized

        fallback = query(repo, case.needle)
        if case.marker == "ParityPrivateKeyMarker":
            assert fallback["results"] == []
            continue
        fallback_results = json.dumps(fallback["results"], sort_keys=True)
        assert fallback["results"]
        assert case.value not in fallback_results
        assert case.needle not in fallback_results
        assert "[REDACTED]" in fallback_results
