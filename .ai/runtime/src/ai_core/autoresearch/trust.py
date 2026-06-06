"""Server-derived trust tiers (PRD §12.2.6).

`trust_tier` is NEVER taken from caller input — a caller could otherwise self-declare
`primary` to bypass review policy. It is derived server-side from a host allowlist in
`.ai/config.yaml` under `autoresearch.trust_hosts` (domain → tier). Unknown/empty hosts
default to `untrusted`.

This does NOT make trusted content safe: every raw source stays quarantined (nonce-wrapped,
verify-det gated) regardless of tier. Tier only governs *promotion* policy downstream
(Stage 3). stdlib only (urllib).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TIER = "untrusted"
VALID_TIERS = ("primary", "secondary", "untrusted")


def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return ""
    if "@" in netloc:          # strip userinfo (https://arxiv.org@evil.com → evil.com)
        netloc = netloc.rsplit("@", 1)[1]
    if netloc.startswith("["):  # IPv6 literal, e.g. [::1]:8443 → ::1
        end = netloc.find("]")
        return netloc[1:end] if end > 1 else netloc[1:]
    if ":" in netloc:          # strip port
        netloc = netloc.split(":", 1)[0]
    return netloc


def derive_tier(url: str, allowlist: dict | None) -> str:
    """Map a source URL's host to a trust tier via the server-side allowlist.

    Exact host match, or registrable-suffix match (export.arxiv.org → arxiv.org).
    Caller-supplied tier is intentionally not a parameter here. Unknown → untrusted.
    An allowlist value that is not a valid tier is treated as untrusted (fail closed).
    """
    if not url or not isinstance(allowlist, dict) or not allowlist:
        return DEFAULT_TIER
    host = _host(url)
    if not host:
        return DEFAULT_TIER
    if host in allowlist:
        return allowlist[host] if allowlist[host] in VALID_TIERS else DEFAULT_TIER
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        suffix = ".".join(parts[i:])
        if suffix in allowlist:
            return allowlist[suffix] if allowlist[suffix] in VALID_TIERS else DEFAULT_TIER
    return DEFAULT_TIER


def load_allowlist(ar_root: Path) -> dict:
    """Load autoresearch.trust_hosts from the project's .ai/config.yaml.

    ar_root is <project>/.ai/autoresearch, so config lives at ar_root.parent/config.yaml.
    Returns {} on any failure (fail closed → everything untrusted).
    """
    try:
        from ..config import load_config
        proj = ar_root.parent.parent
        cfg = load_config(proj)
    except Exception:
        return {}
    section = cfg.get("autoresearch") if isinstance(cfg, dict) else None
    hosts = section.get("trust_hosts") if isinstance(section, dict) else None
    if not isinstance(hosts, dict):
        return {}
    # Reject single-label keys (e.g. "org", "com"): a suffix/exact match on a bare label
    # would trust every domain under it. Multi-label public eTLDs (co.uk, github.io) stay
    # the operator's responsibility — no public-suffix list is bundled (no-deps).
    return {h: t for h, t in hosts.items() if isinstance(h, str) and h.count(".") >= 1}
