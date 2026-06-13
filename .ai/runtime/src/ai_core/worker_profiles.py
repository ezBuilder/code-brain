"""Per-worker isolation profiles (PRD §6.2) — auth-file separation for Codex/AGY workers.

Each profile gives a worker its OWN HOME/XDG dirs under .ai/sandboxes/<profile>/, so each
CLI keeps its login/auth cache in an isolated location and accounts never collide. The user
logs in once per profile (their action); Code Brain only manages the directory layout.

Security invariants (PRD §6.2, §12.1):
- NEVER read, store, or print auth/token/key VALUES. Only directory paths and status.
- Profile directories are created 0700.
- The registry/profile config holds paths and an opaque env map, never secrets.
stdlib only, fail-soft.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .memory import append_audit, now_iso

PROFILES_PARTS = (".ai", "runtime", "state", "worker-profiles.json")
SANDBOX_PARTS = (".ai", "sandboxes")
# env keys we refuse to persist into a profile (would risk capturing a secret value)
_SECRET_ENV_HINT = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL", "API")


def profiles_path(root: Path) -> Path:
    return root.joinpath(*PROFILES_PARTS)


def sandbox_dir(root: Path, profile: str) -> Path:
    safe = "".join(c for c in str(profile) if c.isalnum() or c in "-_")[:64] or "default"
    return root.joinpath(*SANDBOX_PARTS, safe)


def _read(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(profiles_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"schema_version": 1, "profiles": []}
    except Exception:
        return {"schema_version": 1, "profiles": []}


def _write(root: Path, data: dict[str, Any]) -> None:
    path = profiles_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_env(env: dict[str, str] | None) -> dict[str, str]:
    """Drop any env entry whose KEY looks like a secret holder — profiles store layout, not secrets."""
    out: dict[str, str] = {}
    for k, v in (env or {}).items():
        ku = str(k).upper()
        if any(h in ku for h in _SECRET_ENV_HINT):
            continue  # refuse to capture a secret-bearing var into the profile
        out[str(k)[:64]] = str(v)[:256]
    return out


def ensure_profile_dirs(root: Path, profile: str) -> dict[str, str]:
    """Create the per-profile HOME/XDG dirs at 0700 and return their paths."""
    base = sandbox_dir(root, profile)
    layout = {
        "home": str(base / "home"),
        "xdg_config_home": str(base / "config"),
        "xdg_cache_home": str(base / "cache"),
        "xdg_state_home": str(base / "state"),
    }
    for p in (base, *(Path(v) for v in layout.values())):
        p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    return layout


def register_profile(
    root: Path,
    *,
    profile: str,
    agent: str,
    worker_id: str = "",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    prof = "".join(c for c in str(profile) if c.isalnum() or c in "-_")[:64]
    if not prof:
        raise ValueError("profile is required")
    layout = ensure_profile_dirs(root, prof)
    entry = {
        "profile": prof,
        "agent": str(agent)[:32],
        "worker_id": str(worker_id)[:64] or f"{agent}-{prof}",
        **layout,
        "env": _safe_env({"CODE_BRAIN_PROFILE": prof, **(env or {})}),
        "secrets_policy": "external-auth-cache-only",
        "updated_at": now_iso(),
    }
    data = _read(root)
    profiles = [p for p in data.get("profiles", []) if p.get("profile") != prof]
    profiles.append(entry)
    data["schema_version"] = 1
    data["profiles"] = profiles
    _write(root, data)
    append_audit(root, action="worker.profile_register", category="loopd",
                 payload={"profile": prof, "agent": entry["agent"]})
    return {"ok": True, "profile": entry}


def list_profiles(root: Path) -> list[dict[str, Any]]:
    return list(_read(root).get("profiles", []))


def get_profile(root: Path, profile: str) -> dict[str, Any] | None:
    for p in _read(root).get("profiles", []):
        if p.get("profile") == profile:
            return p
    return None


def resolve_profile_env(root: Path, profile: str) -> dict[str, str]:
    """The env a wrapper exports to isolate a worker. Paths only — never secret values."""
    p = get_profile(root, profile)
    if p is None:
        safe = "".join(c for c in str(profile) if c.isalnum() or c in "-_")[:64] or "default"
        layout = ensure_profile_dirs(root, safe)
        p = {**layout, "env": {"CODE_BRAIN_PROFILE": safe}}
    env = {
        "HOME": p["home"],
        "XDG_CONFIG_HOME": p["xdg_config_home"],
        "XDG_CACHE_HOME": p["xdg_cache_home"],
        "XDG_STATE_HOME": p["xdg_state_home"],
    }
    env.update(_safe_env(p.get("env")))
    return env
