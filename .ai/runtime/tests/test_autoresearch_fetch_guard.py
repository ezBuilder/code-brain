"""SSRF defense tests (PRD §12.2.7) — module-ready, not wired into Stage 0."""
from __future__ import annotations

import pytest

from ai_core.autoresearch import fetch_guard


def test_scheme_must_be_https():
    for bad in ["http://example.com", "file:///etc/passwd", "gopher://x", "ftp://x"]:
        with pytest.raises(fetch_guard.FetchBlocked):
            fetch_guard.validate_url(bad)


def test_ip_is_blocked_ranges():
    for bad in ["127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.0.1", "172.16.5.5", "::1", "fe80::1"]:
        assert fetch_guard._ip_is_blocked(bad), bad
    for ok in ["8.8.8.8", "93.184.216.34", "1.1.1.1"]:
        assert not fetch_guard._ip_is_blocked(ok), ok


def test_ip_is_blocked_unparseable_failclosed():
    assert fetch_guard._ip_is_blocked("not-an-ip")


def test_ipv4_mapped_ipv6_blocked():
    assert fetch_guard._ip_is_blocked("::ffff:169.254.169.254")
    assert fetch_guard._ip_is_blocked("::ffff:127.0.0.1")
    assert fetch_guard._ip_is_blocked("::ffff:10.0.0.1")
    assert not fetch_guard._ip_is_blocked("::ffff:8.8.8.8")  # mapped public still allowed


def test_nat64_6to4_blocked():
    assert fetch_guard._ip_is_blocked("64:ff9b::a9fe:a9fe")  # NAT64-wrapped 169.254.169.254
    assert fetch_guard._ip_is_blocked("2002:7f00:1::")       # 6to4


def test_trailing_dot_literal_ip_blocked():
    with pytest.raises(fetch_guard.FetchBlocked):
        fetch_guard.validate_url("https://169.254.169.254./meta")


def test_literal_ip_in_url_blocked():
    for bad in ["https://127.0.0.1/", "https://169.254.169.254/latest/meta-data/",
                "https://10.0.0.1/x", "https://[::1]/"]:
        with pytest.raises(fetch_guard.FetchBlocked):
            fetch_guard.validate_url(bad)


def test_resolve_pinned_blocks_private(monkeypatch):
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(fetch_guard.FetchBlocked):
        fetch_guard.resolve_pinned("rebind.evil", 443)


def test_validate_url_public_pins_ip(monkeypatch):
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    res = fetch_guard.validate_url("https://example.com/path")
    assert res["host"] == "example.com" and res["port"] == 443
    assert res["pinned_ips"] == ["93.184.216.34"]


def test_validate_url_blocks_nonstandard_port(monkeypatch):
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 8080))])
    with pytest.raises(fetch_guard.FetchBlocked):
        fetch_guard.validate_url("https://example.com:8080/")


def test_validate_url_allows_8443(monkeypatch):
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 8443))])
    res = fetch_guard.validate_url("https://example.com:8443/")
    assert res["port"] == 8443


def test_validate_url_rebinding_mixed_ips_blocked(monkeypatch):
    # one public + one private resolved IP → block (attacker can't sneak a private one in)
    monkeypatch.setattr(fetch_guard.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)),
                                         (2, 1, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(fetch_guard.FetchBlocked):
        fetch_guard.validate_url("https://example.com")
