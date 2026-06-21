"""Pilot / optional-feature discoverability registry (stdlib, offline, fail-soft).

Code Brain ships a growing set of pilot and optional features, each gated by an
environment variable. Until now the only way to learn they exist was to read the
source. This module is the single source of truth that surfaces them: their env
var, intended default, a one-line description, and their *effective* on/off state
given the current environment.

Design rules (match the rest of ai_core):
  * stdlib only, no new deps, no network, no LLM.
  * Pure / fail-soft: nothing here raises into a hook or search path. The
    registry is a static table; ``status`` only reads ``os.environ``.
  * This module never mutates the global shell. ``enable_all`` / ``disable_all``
    return a plain ``{VAR: "1"|"0"}`` mapping that the caller (CLI) may apply or
    print as export lines; applying it is the caller's responsibility.
  * Risky / eval-gated pilots (``AI_AST_CHUNK``) are excluded from the safe
    enable_all set so a single switch can't silently turn on retrieval changes
    that still need offline evaluation.

Each feature reads its env var with the *same* truthiness convention the owning
module uses, so ``effective_on`` here matches what that module actually decides:
  * ``AI_AST_CHUNK`` / ``AI_SELF_IMPROVE_AUTO`` use the strict enable-set
    convention (``1/true/yes/on`` enable; unset/anything else off).
  * The rest use the "not in the disable-set" convention (any non-empty,
    non-disable value enables; unset/empty/0/false/no/off off).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Truthiness conventions mirrored from the owning modules (kept local so this
# registry has no import-time coupling to hooks/mcp/etc.).
_ENABLE_VALUES = {"1", "true", "yes", "on"}
_DISABLE_VALUES = {"", "0", "false", "no", "off"}


def _enable_set_truthy(value: str | None) -> bool:
    """Strict convention: only an explicit enable value turns the pilot on.

    Mirrors hooks._env_enabled / ast_chunker.ast_chunk_enabled.
    """
    return str(value or "").strip().lower() in _ENABLE_VALUES


def _disable_set_truthy(value: str | None) -> bool:
    """Permissive convention: any non-disable value turns the feature on.

    Mirrors dir_context.enabled / loop_continuation._enabled /
    memory_tier conflict scan / mcp_server._resources_enabled.
    """
    return str(value or "").strip().lower() not in _DISABLE_VALUES


@dataclass(frozen=True)
class Pilot:
    """One optional feature in the registry.

    Attributes:
        env: the environment variable that gates the feature.
        default_on: the intended default posture (informational; the effective
            state is computed from the env var via ``convention``).
        desc: one-line human description.
        convention: which truthiness rule the owning module applies.
        safe: included in the one-switch ``enable_all`` / ``disable_all`` set.
            Risky/eval-gated pilots are excluded so a single switch can't flip
            them on without explicit, deliberate opt-in.
    """

    env: str
    default_on: bool
    desc: str
    convention: str  # "enable_set" | "disable_set"
    safe: bool

    def effective_on(self) -> bool:
        raw = os.environ.get(self.env)
        try:
            if raw is None:
                return bool(self.default_on)
            if self.convention == "enable_set":
                return _enable_set_truthy(raw)
            return _disable_set_truthy(raw)
        except Exception:
            # Fail-soft: never let a malformed env read raise into a hook/search.
            return bool(self.default_on)


# The single registry. Order is the surfacing order (defaults-on first, then
# opt-in pilots). AST chunking and self-improve auto stay opt-in.
_REGISTRY: tuple[Pilot, ...] = (
    Pilot(
        env="AI_MCP_RESOURCES",
        default_on=True,
        desc="Expose Code Brain context as MCP resources to the host.",
        convention="disable_set",
        safe=True,
    ),
    Pilot(
        env="AI_DIR_CONTEXT",
        default_on=True,
        desc="Walk-up nested AGENTS.md/CLAUDE.md context on Read (PostToolUse).",
        convention="disable_set",
        safe=True,
    ),
    Pilot(
        env="AI_MEMORY_CONFLICT_SCAN",
        default_on=True,
        desc="Advisory offline conflict scan over memory during page-out.",
        convention="disable_set",
        safe=True,
    ),
    Pilot(
        env="AI_LOOP_CONTINUATION",
        default_on=True,
        desc="Stop-hook plan continuation: re-prompt while plan steps remain (bounded).",
        convention="disable_set",
        safe=True,
    ),
    Pilot(
        env="AI_AST_CHUNK",
        default_on=False,
        desc="cAST AST-aware Python chunking for indexing (opt-in, eval-gated).",
        convention="enable_set",
        safe=False,
    ),
    Pilot(
        env="AI_SELF_IMPROVE_AUTO",
        default_on=False,
        desc="Auto closed-loop self-improvement proposals (opt-in).",
        convention="enable_set",
        safe=True,
    ),
)


def registry() -> tuple[Pilot, ...]:
    """The static pilot registry (no env reads)."""
    return _REGISTRY


def status(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Effective state of every pilot keyed by env var.

    ``root`` is accepted for caller symmetry/forward compatibility; the registry
    is environment-driven and does not read the repo. Fail-soft per entry.
    """
    out: dict[str, dict[str, Any]] = {}
    for pilot in _REGISTRY:
        try:
            on = pilot.effective_on()
        except Exception:
            on = bool(pilot.default_on)
        out[pilot.env] = {
            "env": pilot.env,
            "default": bool(pilot.default_on),
            "effective_on": bool(on),
            "desc": pilot.desc,
            "safe": bool(pilot.safe),
        }
    return out


def _set_all(value: str, *, include_unsafe: bool = False) -> dict[str, str]:
    return {
        pilot.env: value
        for pilot in _REGISTRY
        if include_unsafe or pilot.safe
    }


def enable_all(*, include_unsafe: bool = False) -> dict[str, str]:
    """Mapping that turns the *safe* pilot set on (``{VAR: "1"}``).

    Does NOT mutate the global shell — the caller applies/prints it. Risky,
    eval-gated pilots (AI_AST_CHUNK) are excluded unless ``include_unsafe`` is
    explicitly requested.
    """
    return _set_all("1", include_unsafe=include_unsafe)


def disable_all(*, include_unsafe: bool = False) -> dict[str, str]:
    """Mapping that turns the *safe* pilot set off (``{VAR: "0"}``).

    Symmetric with :func:`enable_all`; does not mutate the shell.
    """
    return _set_all("0", include_unsafe=include_unsafe)


def export_lines(mapping: dict[str, str]) -> list[str]:
    """Render a ``{VAR: value}`` mapping as POSIX ``export VAR=value`` lines."""
    return [f"export {key}={value}" for key, value in mapping.items()]
