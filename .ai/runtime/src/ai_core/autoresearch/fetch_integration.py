"""Validated HTTPS fetch for AutoResearch deep research (Stage 3, no-deps, SSRF-guarded).

Validates a URL via fetch_guard (scheme / IP block / resolve-then-pin), then fetches by
connecting a socket to the PINNED IP — not re-resolving — which defeats DNS rebinding. TLS
SNI and certificate verification still use the original hostname. https-only, size/time caps.

Web content is UNTRUSTED: callers MUST ingest the returned text through the
nonce → verify-det → quarantine path (never write it to wiki directly).
stdlib only (socket, ssl, http.client, urllib).
"""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urlparse

from . import fetch_guard

MAX_BYTES = 5_000_000   # 5 MB cap
TIMEOUT_S = 10
_ALLOWED_CONTENT = ("text/", "application/json", "application/xml", "application/xhtml")


class FetchError(RuntimeError):
    """Raised on a transport/content failure after SSRF validation passed."""


def validated_fetch(url: str) -> dict:
    """SSRF-validate then fetch over HTTPS pinned to the resolved IP.

    Returns {url, status, content_type, text}. Raises fetch_guard.FetchBlocked for unsafe
    URLs (before any network I/O), or FetchError for transport/content problems.
    """
    info = fetch_guard.validate_url(url)        # raises FetchBlocked on unsafe (no I/O yet)
    parsed = urlparse(url)
    host, port = info["host"], info["port"]
    pinned = info["pinned_ips"][0]
    path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")

    ctx = ssl.create_default_context()          # verifies cert + hostname
    raw = socket.create_connection((pinned, port), timeout=TIMEOUT_S)
    raw.settimeout(TIMEOUT_S)                    # ensure READ timeout too (slowloris guard)
    try:
        # TLS handshake against the PINNED ip but with SNI/cert check for the real host
        tls = ctx.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT_S)
        conn.sock = tls                          # use our pinned+verified socket (skip re-resolve)
        try:
            conn.request("GET", path, headers={"Host": host, "User-Agent": "autoresearch/0.1"})
            resp = conn.getresponse()
            ct = resp.getheader("Content-Type", "") or ""
            if resp.status >= 300:
                # block 3xx redirects (we never follow them — a Location must not leak upward
                # or trigger an unvalidated re-fetch) AND 4xx/5xx errors. Only 2xx is returned.
                raise FetchError(f"status_blocked:{resp.status}")
            if not any(ct.startswith(c) for c in _ALLOWED_CONTENT):
                raise FetchError(f"content_type_blocked:{ct[:40]}")
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError("too_large")
            return {
                "url": url,
                "status": resp.status,
                "content_type": ct,
                "text": body.decode("utf-8", errors="replace"),
            }
        finally:
            conn.close()
    finally:
        raw.close()
