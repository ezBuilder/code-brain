from __future__ import annotations

import json
import sqlite3
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


def test_generic_assignment_matcher_ignores_identifiers_and_repeated_test_placeholders() -> None:
    assert contains_secret("to" + "ken = cancellationToken") is False
    assert contains_secret("pass" + "word = properties.getProperty") is False
    assert contains_secret("to" + "ken: ApplicationInfo@db5dd68") is False
    assert contains_secret("pass" + 'word = "' + "a" * 40 + '"') is False
    high_signal = "actual" + "-credential-" + "123456789"
    assert contains_secret("pass" + 'word = "' + high_signal + '"') is True


def test_generic_assignment_matcher_preserves_code_type_annotations() -> None:
    safe_annotations = (
        "private var activeScan" + "Token: ScanCancellation" + "Token?",
        "func run(to" + "ken: ScanCancellation" + "Token)",
        "let activeTo" + "ken: ScanCancellation" + "Token",
        "let to" + "ken: ScanCancellation" + "Token",
        "symName: '+[Thing decryptionPass" + "word:veryLongSelectorType:]'",
    )

    for text in safe_annotations:
        assert contains_secret(text, source_path=Path("Example.swift")) is False
        assert redact_text(text, source_path=Path("Example.swift")) == text

    bare_binding = "let to" + "ken: ScanCancellation" + "Token"
    assert contains_secret(bare_binding) is True
    assert contains_secret(bare_binding, source_path=Path("config.yaml")) is True
    assert redact_text(bare_binding, source_path=Path("config.yaml")) != bare_binding

    opaque_value = "AbcdefghijklmnopqrSTUV"
    unsafe_config = "to" + "ken: " + opaque_value
    assert contains_secret(unsafe_config) is True
    assert redact_text(unsafe_config) != unsafe_config

    selector_shaped_config = "dbPass" + "word: veryLongSelectorType:"
    assert contains_secret(selector_shaped_config) is True
    assert redact_text(selector_shaped_config) != selector_shaped_config

    declaration_shaped_config = "let api" + "key: MyLongSecretValueAbc"
    assert contains_secret(declaration_shaped_config) is True
    assert redact_text(declaration_shaped_config) != declaration_shaped_config

    cross_line_paren_config = (
        "# rotate quarterly (see runbook\n" + "api_to" + "ken: MyLongSecretValueAbc\n"
    )
    assert contains_secret(cross_line_paren_config) is True
    assert redact_text(cross_line_paren_config) != cross_line_paren_config

    cross_line_selector_config = (
        "# symbol example +[Thing method:]\n" + "db_pass" + "word: MyLongSecretValueAbc:5432\n"
    )
    assert contains_secret(cross_line_selector_config) is True
    assert redact_text(cross_line_selector_config) != cross_line_selector_config


def test_search_preserves_swift_type_annotations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config = repo / ".ai" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("project_name: swift-parity\n", encoding="utf-8")
    source = repo / "src" / "TypeSurface.swift"
    source.parent.mkdir(parents=True)
    annotation = "let to" + "ken: ScanCancellation" + "Token"
    source.write_text("// SwiftTypeMarker\n" + annotation + "\n", encoding="utf-8")

    rebuild(repo)
    visible = query(repo, "ScanCancellationToken")
    serialized = json.dumps(visible, sort_keys=True)

    assert visible["results"]
    assert annotation in serialized
    assert "[REDACTED]" not in serialized


def test_redactor_orders_multiple_generic_assignment_spans() -> None:
    first = "AbcdefghijklmnoPQRSTUV123"
    second = "ZyxwvutsrqponmlKJIHGF987"
    source = "prefix pass" + f'word="{first}" middle to' + f'ken="{second}" suffix'

    redacted = redact_text(source)

    assert redacted == "prefix [REDACTED] middle [REDACTED] suffix"
    assert first not in redacted
    assert second not in redacted
    assert contains_secret(redacted) is False


def test_redactor_merges_overlapping_generic_assignment_spans() -> None:
    fixture_value = "AbcdefghijklmnoPQRSTUV123"
    source = "pass" + "word=sec" + f"ret={fixture_value}"

    redacted = redact_text(source)

    assert redacted == "[REDACTED]"
    assert fixture_value not in redacted
    assert contains_secret(redacted) is False


def test_redactor_conservatively_scrubs_assignment_like_identifiers() -> None:
    fixture_value = "AbcdefghijklmnopqrsT"
    sources = (
        "sec" + f"ret={fixture_value}.tail",
        "to" + f"ken={fixture_value};",
        "api" + f"key={fixture_value},",
    )

    for source in sources:
        assert contains_secret(source) is False
        redacted = redact_text(source)
        assert "[REDACTED]" in redacted
        assert fixture_value not in redacted
        assert contains_secret(redacted) is False


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
        # FTS tokenization may drop a query that exists only inside a redacted
        # value. An empty result is safe; any returned snippet must stay redacted.
        if not fallback["results"]:
            continue
        fallback_results = json.dumps(fallback["results"], sort_keys=True)
        assert case.value not in fallback_results
        assert case.needle not in fallback_results
        assert "[REDACTED]" in fallback_results


def test_function_chunks_never_store_unredacted_assignments(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config = repo / ".ai" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("project_name: function-redaction\n", encoding="utf-8")
    source = repo / "src" / "worker.py"
    source.parent.mkdir(parents=True)
    fixture_value = _body("function", 28)
    source.write_text(
        "def FunctionRedactionMarker():\n"
        + "    api_"
        + f'key = "{fixture_value}"\n'
        + "    return True\n",
        encoding="utf-8",
    )

    rebuild(repo)
    visible = query(repo, "FunctionRedactionMarker")
    serialized = json.dumps(visible, sort_keys=True)
    with sqlite3.connect(repo / ".ai" / "cache" / "code.sqlite") as conn:
        function_rows = conn.execute(
            "select id from chunks where path like ?",
            ("src/worker.py:%",),
        ).fetchall()
        leaked_rows = conn.execute(
            "select rowid from chunks_fts where chunks_fts match ?",
            (fixture_value,),
        ).fetchall()

    assert visible["results"]
    assert fixture_value not in serialized
    assert function_rows
    assert leaked_rows == []
