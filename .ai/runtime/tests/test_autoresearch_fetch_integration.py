"""Validated fetch tests (Stage 3) — SSRF rejection before any network I/O."""
from __future__ import annotations

import pytest

from ai_core.autoresearch import fetch_integration, fetch_guard


def test_rejects_non_https_before_io():
    for bad in ["http://example.com", "file:///etc/passwd", "ftp://x/y"]:
        with pytest.raises(fetch_guard.FetchBlocked):
            fetch_integration.validated_fetch(bad)


def test_rejects_blocked_ip_literals_before_io():
    for bad in ["https://127.0.0.1/", "https://169.254.169.254/latest/meta-data/",
                "https://10.0.0.1/x", "https://[::1]/"]:
        with pytest.raises(fetch_guard.FetchBlocked):
            fetch_integration.validated_fetch(bad)


def test_rejects_rebinding_to_private(monkeypatch):
    # hostname resolves to a private IP → blocked before connecting (resolve-then-pin)
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(fetch_guard.FetchBlocked):
        fetch_integration.validated_fetch("https://rebind.evil.example/")


def test_caps_and_constants_present():
    # safety caps exist and are sane (defense-in-depth knobs)
    assert fetch_integration.MAX_BYTES <= 10_000_000
    assert 0 < fetch_integration.TIMEOUT_S <= 30
    assert fetch_integration._ALLOWED_CONTENT  # content-type allowlist enforced
