"""Deterministic data models — stdlib dataclasses (NO pydantic; runtime is no-deps).

trust_tier is SERVER-DERIVED from a host allowlist, never taken from caller input
(PRD §12.2.6). All raw sources are treated as untrusted regardless of tier; tier
only governs promotion policy. `sources:` (page→origin) is the single source of
truth for provenance; manifest.wiki_pages is a derived cache.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

TRUST_TIERS = ("primary", "secondary", "untrusted")
STATUSES = ("active", "stale", "draft", "quarantined")
PAGE_TYPES = ("entity", "concept", "synthesis", "summary")


@dataclass
class RawManifest:
    id: str
    sha256: str
    source_url: str
    title: str
    mime: str
    trust_tier: str                 # server-derived; default untrusted
    ingested_at: str
    status: str = "draft"
    status_reason: str = ""
    wiki_pages: list[str] = field(default_factory=list)  # derived cache

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "RawManifest":
        d = json.loads(line)
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class WikiPageMetadata:
    id: str
    type: str                       # entity | concept | synthesis | summary
    title: str
    sources: list[str] = field(default_factory=list)   # raw manifest ids — source of truth
    updated: str = ""
    status: str = "draft"
    taint: bool = False             # derived from a quarantined source (laundering guard)
    links: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "WikiPageMetadata":
        d = json.loads(line)
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class VerifyDetResult:
    format_ok: bool
    substring_ok: bool
    sources_exist: bool
    failed_reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.format_ok and self.substring_ok and self.sources_exist

    @property
    def status(self) -> str:
        return "active" if self.passed else "draft"
