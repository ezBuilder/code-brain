"""Server-derived trust tier tests (PRD §12.2.6) — caller cannot self-declare tier."""
from __future__ import annotations

import hashlib

from ai_core.autoresearch import trust, ingest, storage, manifest


def test_derive_exact_host():
    al = {"arxiv.org": "primary", "github.com": "secondary"}
    assert trust.derive_tier("https://arxiv.org/abs/1", al) == "primary"
    assert trust.derive_tier("https://github.com/x", al) == "secondary"


def test_derive_suffix_match():
    al = {"arxiv.org": "primary"}
    assert trust.derive_tier("https://export.arxiv.org/abs/1", al) == "primary"


def test_derive_strips_port_and_userinfo():
    al = {"arxiv.org": "primary"}
    assert trust.derive_tier("https://u:p@arxiv.org:8443/x", al) == "primary"


def test_derive_unknown_and_empty_default_untrusted():
    al = {"arxiv.org": "primary"}
    assert trust.derive_tier("https://evil.com/x", al) == "untrusted"
    assert trust.derive_tier("", al) == "untrusted"
    assert trust.derive_tier("https://arxiv.org", None) == "untrusted"
    assert trust.derive_tier("https://arxiv.org", {}) == "untrusted"


def test_derive_invalid_tier_fails_closed():
    assert trust.derive_tier("https://x.com", {"x.com": "superadmin"}) == "untrusted"


def test_host_ipv6_bracket_stripped():
    assert trust._host("https://[::1]:8443/x") == "::1"


def test_load_allowlist_filters_single_label(tmp_path):
    proj = tmp_path
    (proj / ".ai").mkdir()
    (proj / ".ai" / "config.yaml").write_text(
        "autoresearch:\n  trust_hosts:\n    org: primary\n    arxiv.org: primary\n",
        encoding="utf-8",
    )
    al = trust.load_allowlist(proj / ".ai" / "autoresearch")
    assert "arxiv.org" in al and "org" not in al  # bare TLD label rejected


def test_caller_cannot_self_declare_primary(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    # caller LIES with trust_tier="primary" but there is no allowlist → must stay untrusted
    ingest.stage_source(ar, content="payload", source_url="https://attacker.example", trust_tier="primary")
    rec = manifest.find_by_sha(ar, hashlib.sha256(b"payload").hexdigest())
    assert rec is not None and rec.trust_tier == "untrusted"
