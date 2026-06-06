"""SSRF defense for outbound fetch (PRD §12.2.7). Module-ready, NOT wired into Stage 0.

Stage 0 ingests local content only; this guards the future deepresearch / url-ingest path
(Stage 3). `validate_url` must run before any fetch: https-only, blocks private / loopback /
link-local (incl. cloud metadata 169.254.169.254) / reserved IPs, and resolves-then-pins to
defend against DNS rebinding (the caller must connect to a pinned IP, not re-resolve).
stdlib only (ipaddress, socket, urllib).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("https",)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # embeds an IPv4 dest
_6TO4 = ipaddress.ip_network("2002::/16")      # embeds an IPv4 dest


class FetchBlocked(ValueError):
    """Raised when a URL fails SSRF validation (fail-closed)."""


def _ip_is_blocked(ip: str) -> bool:
    """Block any non-globally-routable address: private, loopback, link-local (incl. IMDS
    169.254.169.254), reserved, multicast, unspecified. Unwraps IPv4-mapped IPv6 and blocks
    NAT64/6to4 — is_global mis-rates those as global. Unparseable → blocked (fail-closed)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return not addr.ipv4_mapped.is_global  # ::ffff:x.x.x.x → judge embedded IPv4
        if addr in _NAT64 or addr in _6TO4:
            return True  # tunnel-embedded IPv4 reachability — block conservatively
    return not addr.is_global


def resolve_pinned(host: str, port: int) -> list[str]:
    """Resolve host → IPs and raise FetchBlocked if ANY resolved IP is non-public.

    Returns the pinned IPs; the caller must connect to one of these (not re-resolve) so a
    rebinding DNS server cannot swap in a private IP after validation.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchBlocked("dns_resolution_failed") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise FetchBlocked("no_address")
    for ip in ips:
        if _ip_is_blocked(ip):
            raise FetchBlocked("blocked_ip_range")
    return ips


def validate_url(url: str) -> dict:
    """Validate a URL for SSRF safety. Returns {host, port, pinned_ips} or raises FetchBlocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked(f"scheme_not_allowed:{parsed.scheme or 'none'}")
    host = parsed.hostname
    if not host:
        raise FetchBlocked("no_host")
    host = host.rstrip(".")  # normalize FQDN trailing dot before IP/resolve checks
    if not host:
        raise FetchBlocked("no_host")
    # if the URL embeds a literal IP, it must pass the same block list
    try:
        ipaddress.ip_address(host)
        if _ip_is_blocked(host):
            raise FetchBlocked("blocked_ip_literal")
    except ValueError:
        pass  # not a literal IP — it's a hostname, resolved below
    port = parsed.port or 443
    pinned = resolve_pinned(host, port)
    return {"host": host, "port": port, "pinned_ips": pinned}
