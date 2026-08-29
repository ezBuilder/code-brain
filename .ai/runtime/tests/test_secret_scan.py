"""Tests for ai_core.secret_scan (T47)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.secret_scan import scan_source, SecretFinding  # noqa: E402
from ai_core.ast_verify import verify_source  # noqa: E402


def test_scan_finds_aws_access_key():
    src = 'AWS_KEY = "' + "AKIA" + 'IOSFODNN7EXAMPLE"\n'
    findings = scan_source(src)
    assert any(f.kind == "aws_access_key" for f in findings), findings


def test_scan_finds_aws_secret_key():
    src = 'aws_secret_access_key = "' + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" + '"\n'
    findings = scan_source(src)
    assert any(f.kind == "aws_secret_key" for f in findings), findings


def test_scan_finds_openai_key():
    src = 'OPENAI_API_KEY = "' + "sk-" + 'abcdefghijklmnopqrstuvwxyz1234"\n'
    findings = scan_source(src)
    assert any(f.kind == "openai_api_key" for f in findings), findings


def test_scan_finds_anthropic_key():
    src = 'ANTHROPIC = "' + "sk-ant-" + 'api03-AbCdEfGhIjKlMnOpQrSt1234"\n'
    findings = scan_source(src)
    assert any(f.kind == "anthropic_api_key" for f in findings), findings


def test_scan_finds_github_pat():
    src = 'TOKEN = "' + "ghp_" + 'abcdefghijklmnopqrstuvwxyz0123456789"\n'
    findings = scan_source(src)
    assert any(f.kind == "github_pat" for f in findings), findings


def test_scan_finds_slack_token():
    src = 'SLACK = "' + "xoxb-" + '12345-67890-abcdefghij"\n'
    findings = scan_source(src)
    assert any(f.kind == "slack_token" for f in findings), findings


def test_scan_finds_jwt():
    src = 'JWT = "' + "eyJ" + 'hbGciOiJIUzI1NiJ9.' + "eyJ" + 'zdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4f"\n'
    findings = scan_source(src)
    assert any(f.kind == "jwt" for f in findings), findings


def test_scan_finds_private_key_block():
    src = "KEY = '''-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----'''\n"
    findings = scan_source(src)
    assert any(f.kind == "private_key_block" for f in findings), findings


def test_scan_finds_generic_secret():
    src = 'password = "' + "MyS3cret" + '!Pass123"\n'
    findings = scan_source(src)
    assert any(f.kind == "generic_secret" for f in findings), findings


def test_scan_no_false_positive_on_short_string():
    # Less than 12 chars in the quoted value → no generic_secret match
    src = 'password = "ab"\nfoo = "bar"\n'
    findings = scan_source(src)
    assert not any(f.kind == "generic_secret" for f in findings), findings


def test_scan_empty_source():
    assert scan_source("") == []


def test_findings_mask_raw_value():
    raw = "sk-" + "abcdefghijklmnopqrstuv1234WXYZ"
    src = f'KEY = "{raw}"\n'
    findings = scan_source(src)
    assert findings, "expected at least one finding"
    for f in findings:
        # The raw secret (minus possibly the last 4) must not appear in detail.
        # We only allow the masked tail.
        assert raw not in f.detail
        assert f.detail.startswith("***"), f.detail
        # Last 4 chars exposed
        assert f.detail.endswith(raw[-4:])


def test_finding_dataclass_shape():
    src = 'KEY = "' + "AKIA" + 'IOSFODNN7EXAMPLE"\n'
    findings = scan_source(src)
    assert findings
    f = findings[0]
    assert isinstance(f, SecretFinding)
    assert f.lineno >= 1
    assert f.col_offset >= 0
    assert isinstance(f.detail, str)
    assert isinstance(f.kind, str)


def test_lineno_tracks_position():
    src = '\n\nKEY = "' + "AKIA" + 'IOSFODNN7EXAMPLE"\n'
    findings = scan_source(src)
    aws = [f for f in findings if f.kind == "aws_access_key"]
    assert aws and aws[0].lineno == 3, aws


def test_ast_verify_integrates_secret_findings():
    rep = verify_source("OPENAI_API_KEY = 'sk-' + 'a' * 30\n")
    # Concatenated string at AST level so the literal won't match — make a
    # direct literal instead to exercise the integration path:
    rep2 = verify_source('OPENAI_API_KEY = "' + "sk-" + 'abcdefghijklmnopqrstuvwxyz12"\n')
    assert any(v.kind == "secret" for v in rep2.violations), [
        (v.kind, v.detail) for v in rep2.violations
    ]
    # And the masked detail must not leak the raw value.
    assert all("abcdefghij" not in v.detail for v in rep2.violations if v.kind == "secret")
    # The first form (runtime concat) should NOT match since regex sees literal.
    assert not any(v.kind == "secret" for v in rep.violations)


def test_ast_verify_secret_disable_env(monkeypatch):
    monkeypatch.setenv("AI_AST_VERIFY_SECRETS", "0")
    rep = verify_source('OPENAI_API_KEY = "' + "sk-" + 'abcdefghijklmnopqrstuvwxyz12"\n')
    assert not any(v.kind == "secret" for v in rep.violations)


def test_documentation_placeholders_do_not_trip_the_generic_assignment_rule() -> None:
    """Documenting an env var must not fail a consumer's strict doctor.

    Regression: navio tracked a build guide containing
    `export APPLE_PASSWORD="app-specific-password"`. The value is the literal
    placeholder naming Apple's credential type, but the generic
    `password=<20+ chars>` rule flagged it, so `doctor --strict` failed with a
    finding that could only be silenced by an allowlist entry.
    """
    from ai_core.redact import contains_secret

    for text in (
        'export APPLE_PASSWORD="app-specific-password"',
        'export API_KEY="your-api-key-here"',
        'password: "replace-with-your-password"',
        'token = "your_token_value_goes_here"',
        'client_secret="your-client-secret-value"',
    ):
        assert not contains_secret(text), text


def test_placeholder_exemption_still_detects_real_generic_credentials() -> None:
    """The placeholder exemption must not become a bypass.

    Entropy (digits, mixed case, symbols) or unrecognized words keep a value
    classified as a possible credential, and a single long opaque segment is
    never treated as prose.
    """
    from ai_core.redact import contains_secret

    # Values are concatenated at runtime so this test file never contains a
    # credential-shaped literal that the scanner would flag in tracked source.
    for text in (
        'api_key: "' + "9f8e7d6c5b4a" + "39281706abcdef" + '"',
        'token="' + "eyJhbGciOiJIUzI1" + "NiIsInR5cCI6" + '"',
        'secret="' + "correct-horse" + "-battery-staple" + '"',
        'password="' + "administrator" + "-override-console" + '"',
        'api_key = "' + "abcdefghijklm" + "nopqrstuvwxyz" + '"',
        'password = "' + "Sup3r-S3cret" + "-Value-Here1" + '"',
    ):
        assert contains_secret(text), text


def test_placeholder_exemption_requires_a_declared_placeholder_marker() -> None:
    """A value made only of descriptive nouns is still treated as a credential.

    The exemption needs an explicit substitute-me marker (`your`, `example`,
    `replace`, `specific`, ...). Without one, `password=<lowercase words>` could
    be a weak but real credential, so it must keep being reported.
    """
    from ai_core.redact import contains_secret

    for text in (
        'password="' + "prod-server" + "-account-password" + '"',
        'secret="' + "the-real" + "-account-credential" + '"',
        'api_key="' + "my-project" + "-server-key-value" + '"',
    ):
        assert contains_secret(text), text
