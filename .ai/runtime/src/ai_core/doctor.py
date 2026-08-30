from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import load_config
from .preflight_proof import PROOF_MAX_AGE_SECONDS, PROOF_SCHEMA, environment_fingerprint
from .private_write import (
    iter_root_confined_text_lines,
    list_root_confined_directory,
    read_root_confined_text,
    validate_root_confined_directory,
    validate_root_confined_regular_file,
)
from .redact import contains_secret, redact_value
from .render import build_manifest
from .trust import inspect_machine_files


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(
    root: Path,
    *,
    precomputed_index_status: dict[str, object] | None = None,
    precomputed_session_start_ms: int | None = None,
    lightweight: bool = False,
    update_scan_state: bool = True,
) -> list[Check]:
    index_check = (
        check_index_freshness_from_status(precomputed_index_status)
        if precomputed_index_status is not None
        else check_index_freshness(root)
    )
    hot_path_check = check_hot_path_slo(
        root,
        session_start_ms=precomputed_session_start_ms,
        sample_baseline=not lightweight,
    )
    diagnostics_check = (
        Check("diagnostics_dry_run", True, "deferred: run doctor --strict for full diagnostics smoke")
        if lightweight
        else check_diagnostics(root)
    )
    checks = [
        check_layout(root),
        check_config(root),
        check_network_defaults(root),
        check_gitattributes(root),
        check_sqlite_features(),
        index_check,
        check_index_coverage(root),
        check_manifest(root),
        check_trust(root),
        check_jsonl(root),
        check_autonomous_round_completeness(root),
        check_injected_context_budget(root),
        check_global_kit_source_health(root),
        check_global_kit_install_drift(root),
        check_generated_artifacts_bounded(root),
        check_storage_limits(root),
        check_audit_index(root),
        check_audit_chain(root),
        check_episodic_memory(root, lightweight=lightweight),
        hot_path_check,
        check_secret_scan(
            root,
            incremental=lightweight,
            update_state=update_scan_state,
        ),
        check_no_token_estimates(root),
        check_mcp_methods_registered(root),
        check_redaction_self_test(),
        check_bootstrap_preflight(root),
        check_worker_singleton_lock(root),
        check_queue_lease_recovery(root),
        check_queue_age(root),
        diagnostics_check,
        check_skills_catalog(root),
        check_hook_capabilities(root),
        check_completion_guard(root),
        check_precall_rules(root),
        check_antigravity_artifacts(root),
        check_lsp_available(root),
        check_codegraph_coverage(root),
        check_pilots(root),
    ]
    return checks


def check_injected_context_budget(_root: Path) -> Check:
    """Validate the bounded context-injection contract exposed by hooks."""
    from .hooks import (
        CONTEXT_INJECTION_HOOKS,
        INJECTION_HOOKS,
        MAX_INJECTION_BYTES,
        SESSION_START_MAX_INJECTION_BYTES,
        _max_injection_bytes_for,
    )

    expected_hooks = {"SessionStart", "UserPromptSubmit", "SubagentStart"}
    issues: list[str] = []
    if INJECTION_HOOKS != expected_hooks:
        issues.append(f"injection hooks={sorted(INJECTION_HOOKS)!r}")
    if CONTEXT_INJECTION_HOOKS != expected_hooks:
        issues.append(f"context hooks={sorted(CONTEXT_INJECTION_HOOKS)!r}")
    if not 256 <= MAX_INJECTION_BYTES <= 8192:
        issues.append(f"general budget out of bounds: {MAX_INJECTION_BYTES}")
    if not MAX_INJECTION_BYTES <= SESSION_START_MAX_INJECTION_BYTES <= 32768:
        issues.append(f"session budget out of bounds: {SESSION_START_MAX_INJECTION_BYTES}")
    for hook_name in sorted(expected_hooks):
        expected_limit = (
            SESSION_START_MAX_INJECTION_BYTES
            if hook_name == "SessionStart"
            else MAX_INJECTION_BYTES
        )
        actual_limit = _max_injection_bytes_for(hook_name)
        if actual_limit != expected_limit:
            issues.append(f"{hook_name} budget={actual_limit}, expected={expected_limit}")

    if issues:
        return Check("injected_context_budget", False, "; ".join(issues))
    return Check(
        "injected_context_budget",
        True,
        (
            f"general={MAX_INJECTION_BYTES}B; "
            f"session_start={SESSION_START_MAX_INJECTION_BYTES}B; hooks=3"
        ),
    )


def check_global_kit_source_health(root: Path) -> Check:
    """Validate the repo-owned global-agent-kit source inventory without writes."""
    from .global_kit_health import check_global_kit_source

    kit_root = root / "kits" / "global-agent-kit"
    if not kit_root.exists():
        return Check("global_kit_source_health", True, "not applicable: consumer install has no global kit source")
    result = check_global_kit_source(root)
    return Check("global_kit_source_health", result.ok, result.detail)


def check_global_kit_install_drift(root: Path) -> Check:
    """Compare an installed global-agent-kit against source without mutating HOME."""
    from .global_kit_health import check_global_kit_install

    kit_root = root / "kits" / "global-agent-kit"
    if not kit_root.exists():
        return Check("global_kit_install_drift", True, "not applicable: use /kit-doctor for the global install")
    result = check_global_kit_install(root)
    return Check("global_kit_install_drift", result.ok, result.detail)


def check_autonomous_round_completeness(root: Path) -> Check:
    """Read-only validation of bounded typed autonomous-round reports."""
    from .evidence import (
        AUTONOMOUS_ROUND_MAX_BYTES,
        AUTONOMOUS_ROUND_MAX_FILES,
        AUTONOMOUS_ROUND_PREFIX,
        validate_autonomous_round_record,
    )

    outputs = root / ".ai" / "outputs"
    try:
        outputs.lstat()
    except FileNotFoundError:
        return Check("autonomous_round_completeness", True, "no typed round reports")
    except OSError as exc:
        return Check("autonomous_round_completeness", False, f"outputs probe failed: {redact_value(str(exc))}")

    try:
        names = list_root_confined_directory(outputs, root=root, max_entries=1_000)
    except OSError as exc:
        return Check("autonomous_round_completeness", False, f"outputs unreadable: {redact_value(str(exc))}")

    reports = sorted(
        name
        for name in names
        if name.startswith(AUTONOMOUS_ROUND_PREFIX) and name.endswith(".json")
    )
    if not reports:
        return Check("autonomous_round_completeness", True, "no typed round reports")
    if len(reports) > AUTONOMOUS_ROUND_MAX_FILES:
        return Check(
            "autonomous_round_completeness",
            False,
            f"typed round report limit exceeded: {len(reports)}>{AUTONOMOUS_ROUND_MAX_FILES}",
        )

    failures: list[str] = []
    for index, name in enumerate(reports):
        path = outputs / name
        try:
            text, _state = read_root_confined_text(
                path,
                root=root,
                max_bytes=AUTONOMOUS_ROUND_MAX_BYTES,
                require_private=False,
                require_owner=True,
                reject_group_other_writable=True,
            )
            payload = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            failures.append(f"report[{index}]: unreadable_or_invalid_json")
            continue
        result = validate_autonomous_round_record(payload)
        if not result["ok"]:
            issue_text = ",".join(str(issue) for issue in result["issues"][:4])
            failures.append(f"report[{index}]: {issue_text}")

    if failures:
        return Check("autonomous_round_completeness", False, "; ".join(failures[:4]))
    return Check("autonomous_round_completeness", True, f"reports={len(reports)} complete")


def check_pilots(root: Path) -> Check:
    """INFO-only surfacing of pilot/optional features. ALWAYS ok=True — this never
    fails the gate; it just makes the opt-in switches discoverable in doctor output."""
    try:
        from .pilots import status as pilot_status
        states = pilot_status(root)
    except Exception as exc:  # probing must never break doctor
        return Check("pilots", True, f"probe skipped: {exc}")
    total = len(states)
    on = [info["env"] for info in states.values() if info.get("effective_on")]
    off = [info["env"] for info in states.values() if not info.get("effective_on")]
    parts = [f"{len(on)}/{total} on"]
    if on:
        parts.append("on=" + ",".join(on))
    if off:
        parts.append("off=" + ",".join(off))
    detail = str(redact_value("; ".join(parts)))
    return Check("pilots", True, detail)


def check_lsp_available(root: Path) -> Check:
    """INFO-only probe for optional LSP-grade navigation (G5). NEVER fails the gate — the backend
    is an opt-in extra (multilspy + a language server on PATH); absence is the normal default."""
    try:
        from .lsp import lsp_available
        info = lsp_available(root)
    except Exception as exc:  # probing must never break doctor
        return Check("lsp_available", True, f"probe skipped: {exc}")
    if info.get("ok"):
        servers = ", ".join(info.get("servers_detected") or []) or "?"
        return Check("lsp_available", True, f"ready ({servers})")
    return Check("lsp_available", True, f"optional, inactive: {info.get('reason', 'unknown')}")


def check_index_coverage(root: Path) -> Check:
    """INFO-only inventory of paths omitted or stubbed by the source policy."""
    try:
        from .search import index_diagnostics

        report = index_diagnostics(root)
    except Exception as exc:
        return Check("index_coverage", True, f"probe skipped: {type(exc).__name__}")
    skipped = report.get("skipped") if isinstance(report.get("skipped"), list) else []
    not_indexed = report.get("not_indexed_count", 0)
    stubs = report.get("classification_stubs")
    stub_count = len(stubs) if isinstance(stubs, list) else 0
    symbol_budget_count = int(report.get("symbol_budget_count", 0) or 0)
    details = [
        f"candidates={int(report.get('candidate_count', 0) or 0)}",
        f"skipped={int(report.get('skipped_count', 0) or 0)}",
        f"not_indexed={int(not_indexed or 0)}",
        f"generated_stubs={stub_count}",
        f"symbol_budget_skipped={symbol_budget_count}",
    ]
    if skipped:
        compact = ", ".join(
            f"{item.get('path')}:{item.get('class')}:{item.get('reason')}"
            for item in skipped[:8]
            if isinstance(item, dict)
        )
        if compact:
            details.append(f"reasons={compact}")
    return Check("index_coverage", True, str(redact_value("; ".join(details))))


def check_codegraph_coverage(root: Path) -> Check:
    """INFO-only probe for graph-layer (code_symbols/code_calls) language coverage.

    NEVER fails the gate. Its purpose is to make an otherwise SILENT no-op visible:
    multi-language symbol/call extraction (JS/TS/Go/Rust) requires the optional
    ``ast-grep`` binary on PATH. When it is absent the indexer's extractors return
    ``[]`` without error, so ``code_graph_*`` tools and PageRank-personalised
    ranking degrade to empty results while every existing doctor check stays green.
    Python extraction uses the stdlib ``ast`` module and always works.
    """
    # AI_ASTGREP_DISABLE short-circuits every extractor, so treat it as "absent"
    # rather than reporting a present binary that cannot possibly have run.
    astgrep_disabled = os.environ.get("AI_ASTGREP_DISABLE") == "1"
    try:
        from .astgrep_integration import astgrep_available
        has_astgrep = astgrep_available() and not astgrep_disabled
    except Exception:
        has_astgrep = False

    db = root / ".ai" / "cache" / "code.sqlite"
    if not db.exists():
        state = (
            "ast-grep present"
            if has_astgrep
            else "ast-grep disabled via AI_ASTGREP_DISABLE (Python-only graph)"
            if astgrep_disabled
            else "ast-grep absent (Python-only graph)"
        )
        return Check("codegraph_coverage", True, f"not indexed; {state}")

    # ast-grep-dependent source extensions, by indexer language mapping.
    astgrep_exts = {
        ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".go": "go", ".rs": "rust",
        ".kt": "kotlin", ".kts": "kotlin",
        ".dart": "dart",
    }

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return Check("codegraph_coverage", True, f"probe skipped: {exc}")
    try:
        try:
            rows = conn.execute("select path from chunks where kind = 'file'").fetchall()
        except sqlite3.Error:
            try:
                rows = conn.execute("select distinct path from chunks").fetchall()
            except sqlite3.Error as exc:
                return Check("codegraph_coverage", True, f"probe skipped: {exc}")
        try:
            symbol_langs = {
                str(lang) for (lang,) in conn.execute("select distinct lang from code_symbols")
            }
        except sqlite3.Error:
            symbol_langs = set()
        # A file can legitimately declare no NAMED function while still containing
        # calls (an eslint config of object literals, a tracking script that is one
        # anonymous IIFE). Both were observed in real consumer repos. Call edges
        # prove extraction ran, so treat them as evidence of coverage too;
        # otherwise the probe reports a defect where the source simply has
        # nothing to name.
        try:
            call_langs = {
                str(lang) for (lang,) in conn.execute("select distinct lang from code_calls")
            }
        except sqlite3.Error:
            call_langs = set()
    finally:
        conn.close()

    affected: dict[str, int] = {}
    for (raw_path,) in rows:
        rel = str(raw_path or "")
        for ext, lang in astgrep_exts.items():
            if rel.endswith(ext):
                affected[lang] = affected.get(lang, 0) + 1
                break

    extracted_langs = symbol_langs | call_langs
    missing = sorted(lang for lang in affected if lang not in extracted_langs)
    binary_state = (
        "ast-grep present"
        if has_astgrep
        else "ast-grep disabled via AI_ASTGREP_DISABLE"
        if astgrep_disabled
        else "ast-grep absent"
    )
    if not affected:
        return Check("codegraph_coverage", True, f"ok python-only workspace; {binary_state}")
    if not missing:
        summary = ", ".join(f"{lang}={affected[lang]}" for lang in sorted(affected))
        return Check("codegraph_coverage", True, f"ok covered={summary}; {binary_state}")

    gap = ", ".join(f"{lang}:{affected[lang]} files" for lang in missing)
    if astgrep_disabled:
        hint = "unset AI_ASTGREP_DISABLE then run ai index rebuild"
    elif not has_astgrep:
        hint = "install ast-grep (brew install ast-grep | cargo install ast-grep) then run ai index rebuild"
    else:
        hint = "ast-grep is installed but produced no symbols; run ai index rebuild"
    detail = f"optional, degraded: no graph symbols for {gap}; {hint}"
    return Check("codegraph_coverage", True, str(redact_value(detail)))


def _command_semver(binary: str) -> tuple[int, int, int] | None:
    """Best-effort local CLI version probe; no network and never a doctor failure itself."""
    executable = shutil.which(binary)
    if not executable:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            cwd=str(Path.home()),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", f"{proc.stdout}\n{proc.stderr}")
    return tuple(int(part) for part in match.groups()) if match else None


def _contains_code_brain_hook(value: object) -> bool:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and ".ai/bin/ai-hook" in command:
            return True
        return any(_contains_code_brain_hook(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_code_brain_hook(child) for child in value)
    return False


def _check_code_brain_command_hooks(
    host: str,
    hooks: dict[str, object],
) -> list[str]:
    """Validate timeout/matcher policy for managed command hooks only."""
    hot_path = {
        "PreToolUse", "UserPromptSubmit", "PermissionRequest", "Stop",
        "SubagentStop", "TaskCompleted", "TeammateIdle",
    }
    context_limits = {"SessionStart": 5000, "SubagentStart": 5000, "UserPromptSubmit": 2500}
    issues: list[str] = []

    def walk(event: str, value: object, matcher: str | None = None) -> None:
        if isinstance(value, dict):
            next_matcher = value.get("matcher") if isinstance(value.get("matcher"), str) else matcher
            if value.get("type") == "command" and isinstance(value.get("command"), str):
                command = value["command"]
                if ".ai/bin/ai-hook" not in command:
                    return
                timeout = value.get("timeout")
                limit = 3 if host == "codex" and event == "SessionEnd" else (5 if event in hot_path else 2)
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                    issues.append(f"{host} {event} Code Brain command hook timeout missing")
                elif timeout > limit:
                    issues.append(f"{host} {event} Code Brain command hook timeout {timeout}>{limit}s")
                if event == "PreToolUse" and host in {"claude", "codex"}:
                    matcher_text = next_matcher or ""
                    required_matchers = ("apply_patch", "Edit", "Write") if host == "codex" else ("Edit", "Write")
                    if not all(token in matcher_text for token in required_matchers):
                        issues.append(
                            f"{host} PreToolUse Code Brain matcher must include {','.join(required_matchers)}"
                        )
                elif host == "kiro" and event in {"PreToolUse", "PostToolUse"}:
                    # Kiro v1 standalone hooks define an omitted matcher as
                    # always-match.  A bare "*" is not a wildcard here: the
                    # host compiles it as JavaScript RegExp and rejects it as
                    # "Nothing to repeat".  Managed guard/observer hooks must
                    # see every tool, so any narrower matcher is also unsafe.
                    if next_matcher not in {None, ""}:
                        issues.append(
                            f"kiro {event} Code Brain matcher must be omitted for always-match"
                        )
                if host == "codex" and event in context_limits:
                    expected_context_limit = context_limits[event]
                    context_limit = value.get("additionalContextLimit")
                    if not isinstance(context_limit, (int, float)) or isinstance(context_limit, bool) or context_limit <= 0:
                        issues.append(f"{host} {event} Code Brain command hook additionalContextLimit missing/zero")
                    elif context_limit != expected_context_limit:
                        issues.append(
                            f"{host} {event} Code Brain command hook additionalContextLimit "
                            f"{context_limit}!={expected_context_limit}"
                        )
                return
            for child in value.values():
                walk(event, child, next_matcher)
        elif isinstance(value, list):
            for child in value:
                walk(event, child, matcher)

    for event, entries in hooks.items():
        walk(str(event), entries)
    return issues


def check_hook_capabilities(root: Path) -> Check:
    """Report configured *and active* host hooks instead of equating keys with support.

    This catches two production failure classes: a new event written into a strict older
    Codex schema, and a manifest that names events but carries no Code Brain handler.
    Optional/unsupported host events remain explicit in the detail rather than inflating
    the active count.
    """
    root = Path(root)
    details: list[str] = []
    issues: list[str] = []

    def load(path: Path) -> dict[str, object] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{path.relative_to(root)} unreadable: {exc}")
            return None
        if not isinstance(value, dict):
            issues.append(f"{path.relative_to(root)} is not a JSON object")
            return None
        return value

    claude_path = root / ".claude" / "settings.json"
    if claude_path.exists():
        payload = load(claude_path)
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        if not isinstance(hooks, dict):
            issues.append(".claude/settings.json hooks is not an object")
        else:
            issues.extend(_check_code_brain_command_hooks("claude", hooks))
            active = {name for name, entries in hooks.items() if _contains_code_brain_hook(entries)}
            version = _command_semver("claude")
            expected = {"PreToolUse", "PostToolUse", "SessionStart", "Stop", "SubagentStop"}
            if version is not None and version >= (2, 1, 33):
                expected.update({"TaskCompleted", "TeammateIdle"})
            if version is not None and version >= (2, 1, 78):
                expected.add("StopFailure")
            if version is not None and version >= (2, 1, 83):
                expected.update({"CwdChanged", "FileChanged"})
            if version is not None and version >= (2, 1, 84):
                expected.add("TaskCreated")
            missing = sorted(expected - active)
            if missing:
                issues.append(f"Claude missing active hooks {','.join(missing)}")
            details.append(
                f"claude={len(active)} active"
                + (f" v{'.'.join(map(str, version))}" if version else " version=unknown")
            )

    codex_path = root / ".codex" / "hooks.json"
    if codex_path.exists():
        payload = load(codex_path)
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        if not isinstance(hooks, dict):
            issues.append(".codex/hooks.json hooks is not an object")
        else:
            issues.extend(_check_code_brain_command_hooks("codex", hooks))
            active = {name for name, entries in hooks.items() if _contains_code_brain_hook(entries)}
            version = _command_semver("codex")
            expected = {"PreToolUse", "PostToolUse", "SessionStart", "Stop", "SubagentStop"}
            if version is not None and version >= (0, 117, 0):
                expected.add("SessionEnd")
            if version is not None and version >= (0, 150, 0):
                expected.add("Interrupt")
            if version is not None and version < (0, 150, 0) and "Interrupt" in active:
                issues.append(
                    f"Codex v{'.'.join(map(str, version))} cannot parse Interrupt; rerun upgrade"
                )
            missing = sorted(expected - active)
            if missing:
                issues.append(f"Codex missing active hooks {','.join(missing)}")
            details.append(
                f"codex={len(active)} active"
                + (f" v{'.'.join(map(str, version))}" if version else " version=unknown")
            )

    antigravity_path = root / ".agents" / "hooks.json"
    if antigravity_path.exists():
        payload = load(antigravity_path)
        spec = payload.get("code-brain") if isinstance(payload, dict) else None
        if not isinstance(spec, dict):
            issues.append(".agents/hooks.json missing code-brain spec")
        else:
            issues.extend(_check_code_brain_command_hooks("antigravity", spec))
            active = {name for name, entries in spec.items() if _contains_code_brain_hook(entries)}
            required = {"PostToolUse", "PreInvocation", "Stop"}
            missing = sorted(required - active)
            if missing:
                issues.append(f"Antigravity missing active hooks {','.join(missing)}")
            disabled = sorted(name for name in ("PreToolUse", "PostInvocation") if name not in active)
            details.append(
                f"antigravity={len(active)}/5 active"
                + (f" disabled={','.join(disabled)}" if disabled else "")
            )

    kiro_path = root / ".kiro" / "hooks" / "code-brain.json"
    if kiro_path.exists():
        payload = load(kiro_path)
        rows = payload.get("hooks") if isinstance(payload, dict) else None
        if payload is not None and payload.get("version") != "v1":
            issues.append(".kiro/hooks/code-brain.json version must be v1")
        if not isinstance(rows, list):
            issues.append(".kiro/hooks/code-brain.json hooks is not an array")
        else:
            kiro_hooks = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # Kiro keeps timeout (seconds) and matcher on the hook row,
                # while command/type live under action. Preserve both fields
                # for the generic managed-command validator.
                action = row.get("action")
                merged = dict(action) if isinstance(action, dict) else {}
                merged.update(row)
                kiro_hooks[str(row.get("trigger"))] = merged
            issues.extend(_check_code_brain_command_hooks("kiro", kiro_hooks))
            active: set[str] = set()
            for row in rows:
                if not isinstance(row, dict) or row.get("enabled", True) is False:
                    continue
                if _contains_code_brain_hook(row.get("action")):
                    active.add(str(row.get("trigger") or ""))
            expected = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
            missing = sorted(expected - active)
            if missing:
                issues.append(f"Kiro missing active hooks {','.join(missing)}")
            version = _command_semver("kiro-cli")
            surface = "IDE/v3" if version is not None and version < (3, 0, 0) else "CLI/IDE"
            details.append(
                f"kiro={len(active)} active surface={surface}"
                + (f" v{'.'.join(map(str, version))}" if version else " version=unknown")
                + " stop=advisory"
            )
    elif (root / ".kiro").exists():
        details.append("kiro=unmanaged (existing user hooks preserved)")

    if issues:
        return Check("hook_capabilities", False, "; ".join(issues[:6]))
    return Check("hook_capabilities", True, "; ".join(details) if details else "no host hook manifests")


def check_antigravity_artifacts(root: Path) -> Check:
    """Verify the workspace's Antigravity wiring is internally consistent.

    Not a hard requirement — Antigravity install is optional — but when the
    workspace HAS opted in (``.agents/`` exists), the two managed artifacts
    must both be well-formed and point at this project's Code Brain.
    """
    agents_dir = root / ".agents"
    if not agents_dir.exists():
        return Check("antigravity_artifacts", True, "not installed")
    mcp = agents_dir / "mcp_config.json"
    hooks = agents_dir / "hooks.json"
    issues: list[str] = []
    if mcp.exists():
        try:
            import json as _json
            payload = _json.loads(mcp.read_text(encoding="utf-8"))
            servers = payload.get("mcpServers", {}) if isinstance(payload, dict) else {}
            if "code-brain" not in servers:
                issues.append("mcp_config.json missing code-brain server")
        except Exception as exc:
            issues.append(f"mcp_config.json unreadable: {exc}")
    if hooks.exists():
        try:
            import json as _json
            payload = _json.loads(hooks.read_text(encoding="utf-8"))
            # Antigravity 2.0 / CLI 1.1.x schema: top-level {name: spec}; spec carries the
            # native events. NOT the Claude {"hooks": {...}} wrapper (Antigravity
            # cannot parse that — it errors "string into jsonhook.JSONHookSpec").
            # Antigravity has no SessionStart/UserPromptSubmit; PreInvocation supplies the
            # request-start baseline while prompt injection still comes from AGENTS.md.
            if not isinstance(payload, dict):
                issues.append("hooks.json is not a JSON object")
            elif "hooks" in payload or "_note" in payload:
                issues.append("hooks.json uses the legacy Claude wrapper; run install-into to regenerate")
            else:
                spec = payload.get("code-brain")
                if not isinstance(spec, dict):
                    issues.append("hooks.json missing code-brain entry")
                else:
                    post = spec.get("PostToolUse")
                    if not isinstance(post, list) or not post:
                        issues.append("hooks.json code-brain missing event PostToolUse")
                    pre = spec.get("PreInvocation")
                    if not isinstance(pre, list) or not pre:
                        issues.append("hooks.json code-brain missing event PreInvocation")
                    elif any(
                        not isinstance(handler, dict)
                        or handler.get("type", "command") != "command"
                        or ".ai/bin/ai-hook" not in str(handler.get("command") or "")
                        or "PreInvocation" not in str(handler.get("command") or "")
                        or "matcher" in handler
                        or "hooks" in handler
                        for handler in pre
                    ):
                        issues.append("hooks.json code-brain PreInvocation must use direct handlers")
                    stop = spec.get("Stop")
                    if not isinstance(stop, list) or not stop:
                        issues.append("hooks.json code-brain missing event Stop")
                    elif any(
                        not isinstance(handler, dict)
                        or handler.get("type", "command") != "command"
                        or ".ai/bin/ai-hook" not in str(handler.get("command") or "")
                        or "matcher" in handler
                        or "hooks" in handler
                        for handler in stop
                    ):
                        # Stop uses a DIRECT handler list. A matcher-group is Claude-shaped
                        # and Antigravity ignores/rejects it, silently killing continuation.
                        issues.append("hooks.json code-brain Stop must use direct handlers")
        except Exception as exc:
            issues.append(f"hooks.json unreadable: {exc}")
    if issues:
        return Check("antigravity_artifacts", False, "; ".join(issues[:5]))
    active = 0
    disabled: list[str] = []
    if hooks.exists():
        try:
            payload = json.loads(hooks.read_text(encoding="utf-8"))
            spec = payload.get("code-brain") if isinstance(payload, dict) else None
            if isinstance(spec, dict):
                active = sum(1 for event in ("PreToolUse", "PostToolUse", "PreInvocation", "PostInvocation", "Stop") if spec.get(event))
                disabled = [event for event in ("PreToolUse", "PostInvocation") if not spec.get(event)]
        except (OSError, json.JSONDecodeError):
            pass
    detail = f"ok active={active}/5"
    if disabled:
        detail += f" disabled={','.join(disabled)}"
    return Check("antigravity_artifacts", True, detail)


def check_completion_guard(root: Path) -> Check:
    """Prove the premature-stop guard is wired end to end, not merely present on disk.

    This check exists because the PREVIOUS guard (`loop_continuation`) passed every doctor
    check while being completely dead: its `AI_LOOP_CONTINUATION` flag lived only in the
    source kit and the installer merged `hooks` without `env`, so consumers had the Stop hook
    registered and the flag absent. Nothing surfaced that. A guard that cannot be observed is
    a guard nobody can trust, so liveness is asserted here on three axes:

      1. the module imports, exposes its activity observer, and the kill switch is not engaged;
      2. `detect()` runs against the real tree without raising (the signal probes touch git);
      3. PostToolUse plus the Stop-like events the guard rides on are registered for each host
         config present in this workspace.

    Reporting only — the guard's own decisions are never made here.
    """
    import json as _json

    notes: list[str] = []
    issues: list[str] = []
    try:
        from . import completion_guard
    except Exception as exc:
        return Check("completion_guard", False, f"module unavailable: {exc}")

    enabled = completion_guard._enabled()
    notes.append("enabled" if enabled else "disabled via AI_COMPLETION_GUARD")
    if not enabled:
        issues.append("disabled via AI_COMPLETION_GUARD")
    if not callable(getattr(completion_guard, "observe_tool_event", None)):
        issues.append("PostToolUse activity observer unavailable")

    try:
        signal = completion_guard.detect(root)
    except Exception as exc:
        # A raising probe would be swallowed by guard_directive's fail-soft and the guard
        # would silently never fire again. That is precisely the class of defect this
        # check exists to surface.
        return Check("completion_guard", False, f"detect() raised: {exc}")
    notes.append(f"signal={signal.get('kind') if signal else 'none'}")

    # Stop wiring per host. Each file is optional (a workspace need not install every host),
    # but when present it must carry the events the guard is delivered through.
    claude_settings = root / ".claude" / "settings.json"
    if claude_settings.exists():
        try:
            payload = _json.loads(claude_settings.read_text(encoding="utf-8"))
            hooks = payload.get("hooks") if isinstance(payload, dict) else None
            hooks = hooks if isinstance(hooks, dict) else {}
            for event in ("PostToolUse", "Stop", "SubagentStop"):
                if not isinstance(hooks.get(event), list) or not hooks.get(event):
                    issues.append(f".claude/settings.json missing {event}")
        except Exception as exc:
            issues.append(f".claude/settings.json unreadable: {exc}")

    codex_hooks = root / ".codex" / "hooks.json"
    if codex_hooks.exists():
        try:
            payload = _json.loads(codex_hooks.read_text(encoding="utf-8"))
            hooks = payload.get("hooks") if isinstance(payload, dict) else None
            hooks = hooks if isinstance(hooks, dict) else {}
            for event in ("PostToolUse", "Stop", "SubagentStop"):
                if not isinstance(hooks.get(event), list) or not hooks.get(event):
                    issues.append(f".codex/hooks.json missing {event}")
        except Exception as exc:
            issues.append(f".codex/hooks.json unreadable: {exc}")

    agent_hooks = root / ".agents" / "hooks.json"
    if agent_hooks.exists():
        try:
            payload = _json.loads(agent_hooks.read_text(encoding="utf-8"))
            spec = payload.get("code-brain") if isinstance(payload, dict) else None
            stop = spec.get("Stop") if isinstance(spec, dict) else None
            pre = spec.get("PreInvocation") if isinstance(spec, dict) else None
            post = spec.get("PostToolUse") if isinstance(spec, dict) else None
            if not isinstance(pre, list) or not pre:
                issues.append(".agents/hooks.json missing PreInvocation")
            elif any(
                not isinstance(handler, dict)
                or ".ai/bin/ai-hook" not in str(handler.get("command") or "")
                or "PreInvocation" not in str(handler.get("command") or "")
                or "hooks" in handler
                or "matcher" in handler
                for handler in pre
            ):
                issues.append(".agents/hooks.json PreInvocation is not a direct handler list")
            if not isinstance(stop, list) or not stop:
                issues.append(".agents/hooks.json missing Stop")
            elif any(
                not isinstance(handler, dict)
                or ".ai/bin/ai-hook" not in str(handler.get("command") or "")
                or "hooks" in handler
                for handler in stop
            ):
                issues.append(".agents/hooks.json Stop is not a direct handler list")
            if not isinstance(post, list) or not post:
                issues.append(".agents/hooks.json missing PostToolUse")
        except Exception as exc:
            issues.append(f".agents/hooks.json unreadable: {exc}")
    if issues:
        return Check("completion_guard", False, "; ".join(issues[:5]))
    return Check("completion_guard", True, ", ".join(notes))


def check_precall_rules(root: Path) -> Check:
    catalog = root / ".ai" / "precall_rules" / "catalog.jsonl"
    if not catalog.exists():
        return Check("precall_rules", True, "no rules yet")
    try:
        import re as _re
        from .precall_recommend import list_catalog
        entries = list_catalog(root)
    except Exception as exc:
        return Check("precall_rules", False, f"catalog read error: {exc}")
    bad_regex = 0
    stuck_dry_run = 0
    for e in entries:
        try:
            _re.compile(e.pattern)
        except _re.error:
            bad_regex += 1
        if e.status == "dry_run" and e.dry_run_observations > 100:
            stuck_dry_run += 1
    if bad_regex:
        return Check(
            "precall_rules", False,
            f"entries={len(entries)} bad_regex={bad_regex}",
        )
    detail = f"entries={len(entries)} active={sum(1 for e in entries if e.status=='active')}"
    if stuck_dry_run:
        detail += f" stuck_dry_run={stuck_dry_run}"
    return Check("precall_rules", True, detail)


def check_skills_catalog(root: Path) -> Check:
    catalog = root / ".ai" / "skills" / "catalog.jsonl"
    if not catalog.exists():
        return Check("skills_catalog", True, "no catalog yet")
    try:
        from .recommend import _read_marker, _sha256, list_catalog
        entries = list_catalog(root)
    except Exception as exc:
        return Check("skills_catalog", False, f"catalog read error: {exc}")
    drift = 0
    missing = 0
    for entry in entries:
        if entry.status != "installed":
            continue
        for rel in entry.installed_paths:
            path = root / rel
            if not path.exists():
                missing += 1
                continue
            marker = _read_marker(path)
            disk_sha = _sha256(marker.get("__body__", ""))
            if entry.body_sha256 and disk_sha != entry.body_sha256:
                drift += 1
    if missing or drift:
        return Check(
            "skills_catalog",
            False,
            f"installed={sum(1 for e in entries if e.status == 'installed')} drift={drift} missing={missing}",
        )
    return Check(
        "skills_catalog",
        True,
        f"entries={len(entries)} installed={sum(1 for e in entries if e.status == 'installed')}",
    )


def check_layout(root: Path) -> Check:
    required = [
        ".ai/AGENTS.md",
        ".ai/config.yaml",
        ".ai/.gitignore",
        ".ai/.gitattributes",
        ".ai/runtime/pyproject.toml",
        ".ai/runtime/.python-version",
        ".ai/bin/ai",
        ".ai/generated",
        ".ai/memory/audit",
        ".ai/memory/queue/.tmp/.gitkeep",
        ".ai/memory/queue/processing/.gitkeep",
        ".ai/memory/queue/dead/.gitkeep",
    ]
    missing = [item for item in required if not (root / item).exists()]
    if missing:
        return Check("layout", False, "missing: " + ", ".join(missing))

    docs_check_script = root / "scripts" / "docs-check.sh"
    if docs_check_script.is_file() and not os.access(docs_check_script, os.X_OK):
        data = docs_check_script.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        return Check(
            "layout",
            False,
            "scripts/docs-check.sh is not executable; "
            f"bytes={len(data)} blob={blob} sha256={hashlib.sha256(data).hexdigest()}",
        )

    # Source checkouts ship the architecture/upgrade contract documents. Keep
    # their doctor/eval inventory claims tied to source without imposing those
    # repository-only documents on consumer installs.
    source_checkout = (root / "kits" / "global-agent-kit").is_dir() and (root / "scripts" / "package.sh").is_file()
    source_contract_docs = (root / "ARCHITECTURE.md", root / "docs" / "WORLD_CLASS_AUTONOMOUS_UPGRADE.md")
    source_contract_present = tuple(path.is_file() for path in source_contract_docs)
    if source_checkout and not all(source_contract_present):
        missing_contract_docs = [
            str(path.relative_to(root))
            for path, present in zip(source_contract_docs, source_contract_present)
            if not present
        ]
        return Check("layout", False, "docs contract source incomplete: " + ", ".join(missing_contract_docs))
    if source_checkout and all(source_contract_present):
        try:
            from .docs_contract import DocsContractSourceError, load_source_contract, validate_docs_contract

            contract = load_source_contract(root)
            issues = validate_docs_contract(root, contract)
        except DocsContractSourceError as exc:
            return Check("layout", False, f"docs contract source error: {exc}")
        if issues:
            return Check("layout", False, "docs contract drift: " + "; ".join(issues[:3]))
    return Check("layout", True, "ok")


def check_config(root: Path) -> Check:
    try:
        config = load_config(root)
    except Exception as exc:
        return Check("config", False, str(exc))
    features = config.get("features", {})
    bad = [key for key in ("embeddings", "remote_llm", "external_notifications") if features.get(key) is not False]
    if bad:
        return Check("config", False, "default-off features enabled: " + ", ".join(bad))
    search = config.get("search", {})
    if not isinstance(search, dict):
        return Check("config", False, "search config must be a mapping")
    retriever = search.get("retriever", "bm25")
    if retriever not in {"bm25", "vector", "hybrid"}:
        return Check("config", False, f"unknown search retriever: {retriever}")
    if retriever != "bm25":
        return Check("config", False, f"search retriever not implemented by default install: {retriever}")
    # Hook-triggered git sync was retired because hook/MCP hot paths must never
    # cause network I/O. Keep the old key as a one-release, informational no-op
    # so upgrades remain compatible; explicit `ai memory sync` still works.
    sync_block = config.get("memory_sync")
    if isinstance(sync_block, dict) and sync_block.get("enabled"):
        return Check(
            "config",
            True,
            "ok (memory_sync.enabled is deprecated: no longer auto-spawned from a hook; "
            "run `ai memory sync` explicitly instead)",
        )
    return Check("config", True, "ok")


def check_network_defaults(root: Path) -> Check:
    """Egress alignment (-006): the query path never downloads model artifacts.

    Surfaces two stale states instead of silently ignoring them:
      - a truthy AI_SEARCH_*_AUTO_INSTALL env — the in-query background install
        those envs used to gate was removed, so a lingering opt-in means the
        operator still expects egress that will never happen;
      - an .install-lock without model artifacts — residue of the pre-fix broken
        reranker spawn (which retried hourly against an unregistered command) or
        of an aborted install; nothing consumes it anymore.
    """
    problems: list[str] = []
    truthy = {"1", "true", "yes", "on"}
    for env in ("AI_SEARCH_DENSE_AUTO_INSTALL", "AI_SEARCH_RERANK_AUTO_INSTALL"):
        if os.environ.get(env, "").lower() in truthy:
            problems.append(
                f"{env} is set but in-query auto-install was removed; run `ai embedding install` / `ai reranker install` explicitly"
            )
    try:
        from . import embedding as _emb
        from . import reranker as _rr
        for mod, name in ((_emb, "embedding"), (_rr, "reranker")):
            lock = mod.model_cache_dir(root) / ".install-lock"
            if lock.exists() and not mod.is_model_present(root):
                problems.append(f"stale {name} install-lock without artifacts: {lock.relative_to(root)} (delete it)")
    except Exception as exc:  # cache probe must not crash doctor, but must not pass silently either
        problems.append(f"model cache probe failed: {exc}")
    return Check("network_defaults", not problems, "ok" if not problems else "; ".join(problems))


def check_gitattributes(root: Path) -> Check:
    path = root / ".ai" / ".gitattributes"
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=1024 * 1024,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError):
        return Check("gitattributes", False, "unavailable or untrusted")
    active_rules: list[tuple[str, set[str]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            active_rules.append((fields[0], set(fields[1:])))
    required = {
        "*.jsonl": {"merge=union"},
        "memory/audit/*.jsonl": {"-merge"},
        "memory/daily/*.md": {"merge=union"},
        "*.enc.yaml": {"-merge"},
        "*": {"text=auto", "eol=lf"},
    }
    missing = [
        f"{pattern} {' '.join(sorted(attributes))}"
        for pattern, attributes in required.items()
        if not any(rule_pattern == pattern and attributes <= tokens for rule_pattern, tokens in active_rules)
    ]
    try:
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        inside = None
    if inside is not None and inside.returncode == 0:
        try:
            effective = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "check-attr",
                    "merge",
                    "--",
                    ".ai/memory/audit/__code_brain_probe__.jsonl",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            effective = None
        if (
            effective is None
            or effective.returncode != 0
            or not effective.stdout.rstrip().endswith(": merge: unset")
        ):
            missing.append("effective memory/audit/*.jsonl -merge")
    return Check("gitattributes", not missing, "ok" if not missing else "missing: " + ", ".join(missing))


def check_sqlite_features() -> Check:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("create virtual table docs using fts5(body)")
        conn.execute("select json('{\"ok\": true}')")
    except sqlite3.Error as exc:
        return Check("sqlite_features", False, str(exc))
    finally:
        conn.close()
    return Check("sqlite_features", True, "FTS5 and JSON1 available")


def check_index_freshness(root: Path) -> Check:
    db = root / ".ai" / "cache" / "code.sqlite"
    if not db.exists():
        return Check("index_freshness", True, "not indexed")
    from .search import index_hash_status

    return check_index_freshness_from_status(index_hash_status(root))


def check_index_freshness_from_status(status: dict[str, object]) -> Check:
    reason = str(status.get("reason") or "unreadable")
    if status.get("ok"):
        return Check("index_freshness", True, f"ok indexed={status.get('indexed_files', 0)}")
    if status.get("stale") is False:
        return Check("index_freshness", True, f"ok indexed={status.get('indexed', 0)}")
    if reason == "missing":
        return Check("index_freshness", True, "not indexed")
    if reason == "legacy_schema":
        return Check("index_freshness", False, "legacy index schema; run ai index rebuild")
    if reason == "outdated_schema":
        return Check("index_freshness", False, "outdated index schema; rebuilds on next query or ai index rebuild")
    if reason == "unreadable":
        return Check("index_freshness", False, str(status.get("detail") or "index unreadable"))
    raw_changed = status.get("changed_paths") or []
    changed = list(raw_changed) if isinstance(raw_changed, (list, tuple, set)) else []
    if changed:
        return Check("index_freshness", False, "stale: " + ", ".join(changed[:10]))
    return Check("index_freshness", False, reason)


def check_manifest(root: Path) -> Check:
    path = root / ".ai" / "generated" / "manifest.json"
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=2 * 1024 * 1024,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return Check("manifest", False, "manifest missing; run ai render")
    except (OSError, UnicodeDecodeError):
        return Check("manifest", False, "manifest unavailable or untrusted")
    try:
        existing = json.loads(text)
    except json.JSONDecodeError:
        return Check("manifest", False, "invalid json")
    if not isinstance(existing, dict):
        return Check("manifest", False, "invalid manifest shape")
    expected = build_manifest(root)
    drift_fields = []
    for key in ("schema_version", "embedding", "sqlite_vec", "summarizer", "chunker", "trust"):
        if existing.get(key) != expected.get(key):
            drift_fields.append(key)
    return Check("manifest", not drift_fields, "ok" if not drift_fields else "drift: " + ", ".join(drift_fields))


def check_trust(root: Path) -> Check:
    _machines, bad = inspect_machine_files(root)
    return Check("trust", not bad, "ok" if not bad else "invalid: " + ", ".join(bad))


def check_jsonl(root: Path) -> Check:
    from .memory import _AUDIT_MAX_BYTES, _JSONL_AUTO_MAX_BYTES, _JSONL_LINE_MAX_BYTES

    memory_root = root / ".ai" / "memory"
    bad: list[str] = []
    try:
        validate_root_confined_directory(memory_root, root=root)
    except FileNotFoundError:
        return Check("jsonl", True, "ok")
    except OSError:
        return Check("jsonl", False, "invalid: memory-directory-untrusted")
    files_seen = 0
    dirs_seen = 0
    for current, dirnames, filenames in os.walk(memory_root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs_seen += 1
        if dirs_seen > 2_000:
            bad.append("directory-limit")
            break
        safe_dirs: list[str] = []
        for name in dirnames:
            candidate = current_path / name
            try:
                validate_root_confined_directory(candidate, root=root)
            except (FileNotFoundError, OSError):
                bad.append(f"{candidate.relative_to(root).as_posix()}:untrusted-directory")
                continue
            safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            files_seen += 1
            if files_seen > 5_000:
                bad.append("file-limit")
                break
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            cap = _AUDIT_MAX_BYTES if path.parent.name == "audit" else _JSONL_AUTO_MAX_BYTES
            try:
                validate_root_confined_regular_file(
                    path,
                    root=root,
                    max_bytes=cap,
                    require_owner=True,
                    reject_group_other_writable=True,
                )
                for line_no, line in enumerate(
                    iter_root_confined_text_lines(
                        path,
                        root=root,
                        max_bytes=cap,
                        max_line_bytes=_JSONL_LINE_MAX_BYTES,
                        require_private=False,
                        require_owner=True,
                        reject_group_other_writable=True,
                    ),
                    1,
                ):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        bad.append(f"{rel}:{line_no}")
                        continue
                    if not isinstance(value, dict):
                        bad.append(f"{rel}:{line_no}:not-object")
            except (OSError, UnicodeDecodeError):
                bad.append(f"{rel}:untrusted-or-oversized")
        if bad and bad[-1] == "file-limit":
            break
    return Check("jsonl", not bad, "ok" if not bad else "invalid: " + ", ".join(bad[:10]))


def check_storage_limits(root: Path) -> Check:
    from .search import INDEX_DB_MAX_BYTES, INDEX_SIDECAR_MAX_BYTES, db_path
    from .storage_lifecycle import (
        AI_MAX_TOTAL_BYTES,
        DIAGNOSTIC_MAX_FILES,
        DIAGNOSTIC_MAX_TOTAL_BYTES,
        DIAGNOSTIC_RETENTION_DAYS,
        LOG_MAX_FILES,
        LOG_MAX_TOTAL_BYTES,
        LOG_RETENTION_DAYS,
        OUTPUT_MAX_ENTRIES,
        OUTPUT_MAX_TOTAL_BYTES,
        TMP_MAX_ENTRIES,
        TMP_MAX_TOTAL_BYTES,
        UPGRADE_BACKUP_MAX_FILES,
        UPGRADE_BACKUP_MAX_TOTAL_BYTES,
        UPGRADE_BACKUP_RETENTION_DAYS,
        workspace_storage_status,
    )

    bad: list[str] = []
    db = db_path(root)
    for path, cap in (
        (db, INDEX_DB_MAX_BYTES),
        (Path(str(db) + "-wal"), INDEX_SIDECAR_MAX_BYTES),
        (Path(str(db) + "-shm"), INDEX_SIDECAR_MAX_BYTES),
        (Path(str(db) + "-journal"), INDEX_SIDECAR_MAX_BYTES),
    ):
        try:
            state = validate_root_confined_regular_file(
                path, root=root, require_owner=True, reject_group_other_writable=True
            )
        except FileNotFoundError:
            continue
        except OSError:
            bad.append(f"{path.relative_to(root).as_posix()}:untrusted")
            continue
        if int(state.st_size) > cap:
            bad.append(f"{path.relative_to(root).as_posix()}:oversized")

    policies = (
        (root / ".ai" / "cache" / "logs", lambda n: n.endswith(".jsonl"), LOG_RETENTION_DAYS, LOG_MAX_FILES, LOG_MAX_TOTAL_BYTES),
        (root / ".ai" / "cache" / "diagnostics", lambda n: n.startswith("diagnostics-") and n.endswith((".json", ".zip")), DIAGNOSTIC_RETENTION_DAYS, DIAGNOSTIC_MAX_FILES, DIAGNOSTIC_MAX_TOTAL_BYTES),
        (root / ".ai" / "cache" / "upgrade", lambda n: n.startswith("rollback-") and n.endswith(".json"), UPGRADE_BACKUP_RETENTION_DAYS, UPGRADE_BACKUP_MAX_FILES, UPGRADE_BACKUP_MAX_TOTAL_BYTES),
    )
    now = time.time()
    checked = 0
    for directory, accept, keep_days, max_files, max_total in policies:
        try:
            names = list_root_confined_directory(directory, root=root, max_entries=4096)
        except FileNotFoundError:
            continue
        except OSError:
            bad.append(f"{directory.relative_to(root).as_posix()}:untrusted")
            continue
        count = 0
        total = 0
        for name in names:
            if not accept(name):
                continue
            path = directory / name
            try:
                state = validate_root_confined_regular_file(
                    path, root=root, require_owner=True, reject_group_other_writable=True
                )
            except (FileNotFoundError, OSError):
                bad.append(f"{path.relative_to(root).as_posix()}:untrusted")
                continue
            checked += 1
            count += 1
            total += int(state.st_size)
            if float(state.st_mtime) < now - keep_days * 86400:
                bad.append(f"{path.relative_to(root).as_posix()}:expired")
        if count > max_files:
            bad.append(f"{directory.relative_to(root).as_posix()}:file-count")
        if total > max_total:
            bad.append(f"{directory.relative_to(root).as_posix()}:total-bytes")
    workspace = workspace_storage_status(root)
    if not workspace["complete"] or workspace["errors"]:
        bad.append(".ai:scan-incomplete")
    # Caps apply to RECLAIMABLE bytes. Pinned entries (tracked, referenced by tracked
    # source, or an explicit .keep) cannot be deleted by the enforcer, so counting them
    # produced a failure the user could never clear except by deleting their own fixtures.
    if int(workspace.get("tmp_reclaimable_bytes", workspace["tmp_bytes"])) > TMP_MAX_TOTAL_BYTES:
        bad.append(".ai/tmp:total-bytes")
    if int(workspace.get("tmp_reclaimable_entries", workspace["tmp_top_entries"])) > TMP_MAX_ENTRIES:
        bad.append(".ai/tmp:file-count")
    if int(workspace.get("output_reclaimable_bytes", workspace["output_bytes"])) > OUTPUT_MAX_TOTAL_BYTES:
        bad.append(".ai/outputs:total-bytes")
    if int(workspace.get("output_reclaimable_entries", workspace["output_top_entries"])) > OUTPUT_MAX_ENTRIES:
        bad.append(".ai/outputs:file-count")
    if int(workspace.get("ai_reclaimable_bytes", workspace["ai_bytes"])) > AI_MAX_TOTAL_BYTES:
        bad.append(".ai:total-bytes")
    pinned_note = ""
    pinned_total = int(workspace.get("tmp_pinned_bytes", 0)) + int(workspace.get("output_pinned_bytes", 0))
    if pinned_total:
        pinned_note = f" pinned_bytes={pinned_total}"
    detail = (
        f"ok checked={checked} ai_bytes={workspace['ai_bytes']} tmp_bytes={workspace['tmp_bytes']} "
        f"output_bytes={workspace['output_bytes']}{pinned_note}"
    )
    return Check("storage_limits", not bad, detail if not bad else "invalid: " + ", ".join(bad[:10]))


def check_generated_artifacts_bounded(root: Path) -> Check:
    from .evidence import EVIDENCE_MAX_BYTES, evidence_path
    from .memory import _SESSION_NOTE_MAX_BYTES, EVENTS_MAX_BYTES, events_path, session_current_path
    from .prompt_growth import PROMPT_GROWTH_MAX_BYTES, log_path

    targets = (
        (events_path(root), EVENTS_MAX_BYTES),
        (log_path(root), PROMPT_GROWTH_MAX_BYTES),
        (evidence_path(root), EVIDENCE_MAX_BYTES),
        (session_current_path(root), _SESSION_NOTE_MAX_BYTES),
    )
    oversized: list[str] = []
    for path, cap in targets:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > cap:
            oversized.append(f"{path.relative_to(root).as_posix()}={size}>{cap}")
    if oversized:
        return Check(
            "generated_artifacts_bounded",
            False,
            "oversized: " + ", ".join(oversized[:8]) + "; run ai memory page-out --json",
        )
    return Check("generated_artifacts_bounded", True, f"ok checked={len(targets)}")


def read_jsonl(path: Path, *, root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    text, _state = read_root_confined_text(
        path,
        root=root,
        max_bytes=100_000_000,
        require_private=False,
    )
    for line in text.splitlines():
        if line.strip():
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                records.append(loaded)
    return records


def audit_key(record: dict[str, object], path: str | None = None) -> tuple[object, object, object, object]:
    return (record.get("ts"), record.get("action"), record.get("category"), path or record.get("path"))


def _unsafe_audit_entries(root: Path) -> list[str]:
    audit_root = root / ".ai" / "memory" / "audit"
    try:
        from .memory import _AUDIT_FILE_MAX_COUNT

        names = list_root_confined_directory(
            audit_root, root=root, max_entries=_AUDIT_FILE_MAX_COUNT
        )
    except FileNotFoundError:
        return []
    except OSError:
        return ["audit-directory-untrusted"]
    bad: list[str] = []
    from .memory import _audit_file_sort_key

    for name in names:
        if _audit_file_sort_key(name) is None:
            continue
        path = audit_root / name
        try:
            validate_root_confined_regular_file(
                path,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError):
            bad.append(path.relative_to(root).as_posix())
    return bad


def _check_audit_index_snapshot(root: Path) -> Check:
    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    bad: list[str] = []
    try:
        validate_root_confined_directory(audit_root, root=root)
    except FileNotFoundError:
        pass
    except OSError as exc:
        bad.append(f"audit-directory-untrusted:{exc}")
    bad.extend(_unsafe_audit_entries(root))
    try:
        index_records = read_jsonl(index_path, root=root)
    except FileNotFoundError:
        index_records = []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        index_records = []
        bad.append(f"audit-index-untrusted:{exc}")
    index_keys = {audit_key(record) for record in index_records}

    for record in index_records:
        rel_path = record.get("path")
        if not isinstance(rel_path, str):
            bad.append("audit-index:path")
            continue
        target = root / rel_path
        if target.parent != audit_root or target.suffix != ".jsonl":
            bad.append(rel_path)
            continue
        try:
            validate_root_confined_regular_file(
                target,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError):
            bad.append(rel_path)

    audit_keys: set[tuple[object, object, object, object]] = set()
    from .memory import _AUDIT_INDEX_MAX_ROWS, all_audit_files

    expected_index_keys: deque[tuple[object, object, object, object]] = deque(
        maxlen=_AUDIT_INDEX_MAX_ROWS
    )

    for path in all_audit_files(root):
        rel_path = path.relative_to(root).as_posix()
        try:
            records = read_jsonl(path, root=root)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            bad.append(f"{rel_path}:untrusted:{exc}")
            continue
        for record in records:
            key = audit_key(record, rel_path)
            audit_keys.add(key)
            expected_index_keys.append(key)

    for key in expected_index_keys:
        if key not in index_keys:
            bad.append(f"{key[3]}:missing-index:{key[0]}")

    for key in sorted(index_keys - audit_keys, key=str):
        bad.append(f"audit-index:orphan:{key[0]}")

    return Check("audit_index", not bad, "ok" if not bad else "invalid: " + ", ".join(bad[:10]))


def check_audit_index(root: Path) -> Check:
    """Validate one transaction-consistent audit/index snapshot."""

    from .memory import audit_transaction_lock_path
    from .private_write import private_file_lock

    try:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            return _check_audit_index_snapshot(root)
    except OSError as exc:
        return Check("audit_index", False, f"unavailable or untrusted: {exc}")


def _check_audit_chain_snapshot(root: Path) -> Check:
    audit_root = root / ".ai" / "memory" / "audit"
    bad: list[str] = []
    chained = 0
    try:
        validate_root_confined_directory(audit_root, root=root)
    except FileNotFoundError:
        detail = "ok no chained lines yet"
        return Check("audit_chain", True, detail)
    except OSError as exc:
        return Check("audit_chain", False, f"invalid: audit-directory-untrusted:{exc}")
    bad.extend(_unsafe_audit_entries(root))

    from .memory import _audit_file_sort_key, all_audit_files, audit_segment_sequence_issues

    audit_files = all_audit_files(root)
    for issue in audit_segment_sequence_issues(audit_files):
        kind = str(issue["kind"])
        year = int(issue["year"])
        if kind == "duplicate":
            duplicate_path = Path(str(issue["paths"][-1])).relative_to(root).as_posix()
            bad.append(
                f"{duplicate_path}:segment_sequence_duplicate:{year}.{int(issue['sequence']):06d}"
            )
        elif kind == "start":
            bad.append(
                f"audit/{year}:segment_sequence_start:expected=000001 actual={int(issue['actual']):06d}"
            )
        elif kind == "gap":
            bad.append(
                f"audit/{year}:segment_sequence_gap:missing="
                f"{int(issue['missing_start']):06d}-{int(issue['missing_end']):06d}"
            )

    event_ids: set[str] = set()
    previous_year: int | None = None
    previous_rel: str | None = None
    previous_last_sha: str | None = None
    previous_file_sha256: str | None = None
    previous_file_bytes: int | None = None
    for path in audit_files:
        previous_line: str | None = None
        legacy_records = 0
        rel_path = path.relative_to(root).as_posix()
        try:
            text, _state = read_root_confined_text(
                path,
                root=root,
                max_bytes=100_000_000,
                require_private=False,
            )
        except (OSError, UnicodeDecodeError) as exc:
            bad.append(f"{rel_path}:untrusted:{exc}")
            continue
        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        sort_key = _audit_file_sort_key(path.name)
        file_year = sort_key[0] if sort_key is not None else None
        file_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if sort_key is not None and sort_key[1] == 0:
            filename_digest = path.name.split(".")[2]
            if filename_digest != file_sha256[:12]:
                bad.append(f"{rel_path}:segment_filename_digest_mismatch")
        try:
            first_record = json.loads(nonempty_lines[0]) if nonempty_lines else None
        except json.JSONDecodeError:
            first_record = None
        payload = (
            first_record.get("payload")
            if isinstance(first_record, dict) and isinstance(first_record.get("payload"), dict)
            else {}
        )
        if previous_year is None or file_year != previous_year:
            if isinstance(first_record, dict) and first_record.get("action") == "audit.segment_started":
                bad.append(f"{rel_path}:segment_link_orphan")
        else:
            if not isinstance(first_record, dict) or first_record.get("action") != "audit.segment_started":
                bad.append(f"{rel_path}:segment_link_marker_missing")
            else:
                if payload.get("previous_segment") != previous_rel:
                    bad.append(f"{rel_path}:segment_link_path_mismatch")
                if payload.get("previous_last_sha") != previous_last_sha:
                    bad.append(f"{rel_path}:segment_link_line_mismatch")
                if payload.get("previous_file_sha256") != previous_file_sha256:
                    bad.append(f"{rel_path}:segment_link_file_mismatch")
                if payload.get("bytes_segmented") != previous_file_bytes:
                    bad.append(f"{rel_path}:segment_link_bytes_mismatch")
                if payload.get("lossy") is not False:
                    bad.append(f"{rel_path}:segment_link_lossy")
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                bad.append(f"{rel_path}:line {line_no}:invalid_json:{exc.msg}")
                previous_line = line
                continue
            if not isinstance(record, dict):
                bad.append(f"{rel_path}:line {line_no}:not_object")
                previous_line = line
                continue

            event_id = record.get("event_id")
            if "event_id" in record:
                if not isinstance(event_id, str) or not event_id.startswith("evt-") or len(event_id) != 36:
                    bad.append(f"{rel_path}:line {line_no}:event_id_invalid")
                elif event_id in event_ids:
                    bad.append(f"{rel_path}:line {line_no}:event_id_duplicate")
                else:
                    event_ids.add(event_id)
            else:
                legacy_records += 1

            is_chained = "prev_sha" in record
            if is_chained:
                chained += 1
                prev_sha = record.get("prev_sha")
                if prev_sha is not None and not (isinstance(prev_sha, str) and len(prev_sha) == 64):
                    bad.append(f"{rel_path}:line {line_no}:prev_sha_invalid")
                expected = hashlib.sha256(previous_line.encode("utf-8")).hexdigest() if previous_line is not None else None
                if prev_sha != expected:
                    bad.append(f"{rel_path}:line {line_no}:prev_sha_mismatch")

            previous_line = line

        if legacy_records:
            bad.append(f"{rel_path}:legacy_unverifiable={legacy_records}")
        previous_year = file_year
        previous_rel = rel_path
        previous_last_sha = hashlib.sha256(nonempty_lines[-1].encode("utf-8")).hexdigest() if nonempty_lines else None
        previous_file_sha256 = file_sha256
        previous_file_bytes = len(text.encode("utf-8"))

    hard_bad = [item for item in bad if "legacy_unverifiable=" not in item]
    if not hard_bad and bad:
        # Existing legacy rows are reported honestly but do not make a normal
        # upgrade red. Any malformed row, unsafe path, bad event ID, or chain
        # mismatch remains a hard failure.
        return Check("audit_chain", True, "unverifiable: " + ", ".join(bad[:10]))
    if hard_bad:
        # Chain damage usually comes from stash union merges or partial restore;
        # `ai audit repair-chain` can fix it deterministically without dropping content.
        if any(
            marker in item
            for item in bad
            for marker in ("segment_sequence_", "segment_link_orphan")
        ):
            hint = "restore the missing raw segment; repair refuses evidence gaps"
        elif any("legacy_unverifiable=" in item for item in bad):
            hint = "legacy boundary requires explicit migration"
        else:
            hint = "run `ai audit repair-chain` to fix"
        return Check(
            "audit_chain",
            False,
            "invalid: " + ", ".join(hard_bad[:10]) + " — " + hint,
        )
    detail = f"ok chained_lines={chained}" if chained else "ok no chained lines yet"
    return Check("audit_chain", True, detail)


def check_audit_chain(root: Path) -> Check:
    """Validate one transaction-consistent raw-audit snapshot."""

    from .memory import audit_transaction_lock_path
    from .private_write import private_file_lock

    try:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            return _check_audit_chain_snapshot(root)
    except OSError as exc:
        return Check("audit_chain", False, f"unavailable or untrusted: {exc}")


def check_episodic_memory(root: Path, *, lightweight: bool = False) -> Check:
    """Validate the disposable episodic index without penalizing hook checks."""
    if lightweight:
        return Check("episodic_memory", True, "deferred: run doctor --strict for full integrity")
    try:
        from .episodic_runtime import status

        report = status(root)
    except Exception as exc:
        return Check("episodic_memory", False, f"invalid: {type(exc).__name__}; rebuild index")
    if not report.get("ok") or not report.get("integrity_ok", False):
        return Check("episodic_memory", False, "invalid derived index; run `ai memory episodic build`")
    if not report.get("built"):
        return Check("episodic_memory", True, "inactive: built by detached memory page-out")
    detail = (
        f"ok indexed={int(report.get('indexed_events', 0) or 0)} "
        f"raw={int(report.get('raw_events', 0) or 0)} rows={int(report.get('tier_rows', 0) or 0)}"
    )
    if report.get("stale"):
        detail = "stale append lag; next page-out refreshes — " + detail
    if not report.get("source_truth_complete", True):
        gap = report.get("source_history_gap")
        if isinstance(gap, dict):
            detail += (
                "; history_gap="
                f"{int(gap.get('previous_indexed_events', 0) or 0)}"
                f"->{int(gap.get('current_raw_events', 0) or 0)}"
            )
        legacy_rows = int(report.get("legacy_fold_rows", 0) or 0)
        if legacy_rows:
            detail += f"; legacy_lossy={legacy_rows}"
    return Check("episodic_memory", True, detail)


# Wall-clock hot-path timings vary widely across machines (cold caches, slow or
# shared CI runners, the Windows job that strips CI markers). This gate is a coarse
# guard against GROSS regressions, not a per-runner benchmark, so it fails only at a
# generous multiple of the target. Determinism does NOT depend on is_ci() — the
# Windows portability job unsets CI/GITHUB_ACTIONS, so an is_ci()-conditional relax
# would silently not apply there and the gate would flake again.
SLO_GATE_HEADROOM = 3
# Process launch is materially noisier than in-process work under sharded CI. A 4x
# coarse gate still catches a user-visible one-second regression at the 250ms target.
ENTRYPOINT_SLO_GATE_HEADROOM = 4


def check_hot_path_slo(
    root: Path,
    *,
    session_start_ms: int | None = None,
    sample_baseline: bool = True,
) -> Check:
    from .hooks import HOT_PATH_TARGET_MS, SESSION_START_TARGET_MS, handle_hook

    def best_elapsed_ms(hook: str, n: int) -> int:
        # Best-of-N: a single sample is dominated by scheduler/GC/cold-cache noise;
        # the SLO is about steady-state hot-path cost.
        return min(
            int(handle_hook(root, hook, {"agent": "doctor", "dry": True})["elapsed_ms"])
            for _ in range(n)
        )

    samples = []
    if sample_baseline:
        for _ in range(10):
            payload = handle_hook(root, "DoctorSLOBaseline", {"agent": "doctor", "dry": True})
            samples.append(int(payload["elapsed_ms"]))
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)] if samples else None
    start_ms = (
        max(0, int(session_start_ms))
        if session_start_ms is not None
        else best_elapsed_ms("SessionStart", 5)
    )
    if sample_baseline:
        try:
            from .obs import hook_entrypoint_latency

            entrypoint = hook_entrypoint_latency(root)
        except Exception:
            entrypoint = {
                "ok": True,
                "measured": False,
                "reason": "measurement_failed_soft",
                "scope": "end_to_end",
                "hook": "SessionStart",
                "best_ms": None,
                "p95_ms": None,
                "target_ms": None,
            }
    else:
        entrypoint = {
            "ok": True,
            "measured": False,
            "reason": "deferred_lightweight",
            "scope": "end_to_end",
            "hook": "SessionStart",
            "best_ms": None,
            "p95_ms": None,
            "target_ms": None,
        }

    entrypoint_p95 = entrypoint.get("p95_ms") if entrypoint.get("measured") else None
    entrypoint_target = entrypoint.get("target_ms")
    entrypoint_gate_ms = (
        int(entrypoint_target) * ENTRYPOINT_SLO_GATE_HEADROOM
        if isinstance(entrypoint_target, (int, float))
        else None
    )
    entrypoint_ok = (
        entrypoint_p95 is None
        or entrypoint_gate_ms is None
        or int(entrypoint_p95) <= entrypoint_gate_ms
    )
    ok = (
        (p95 is None or p95 <= HOT_PATH_TARGET_MS * SLO_GATE_HEADROOM)
        and start_ms <= SESSION_START_TARGET_MS * SLO_GATE_HEADROOM
        and entrypoint_ok
    )
    return Check(
        "hot_path_slo",
        ok,
        f"p95_ms={p95 if p95 is not None else 'deferred'}, p95_scope=in_process, "
        f"target_ms={HOT_PATH_TARGET_MS}, "
        f"session_start_ms={start_ms}, "
        f"session_start_target_ms={SESSION_START_TARGET_MS}, "
        f"entrypoint_p95_ms={entrypoint.get('p95_ms') if entrypoint.get('measured') else 'unmeasured'}, "
        f"entrypoint_best_ms={entrypoint.get('best_ms') if entrypoint.get('measured') else 'unmeasured'}, "
        f"entrypoint_scope={entrypoint.get('scope') or 'end_to_end'}, "
        f"entrypoint_hook={entrypoint.get('hook') or 'SessionStart'}, "
        f"entrypoint_target_ms={entrypoint.get('target_ms')}, "
        f"entrypoint_gate_ms={entrypoint_gate_ms}, "
        f"entrypoint_reason={entrypoint.get('reason') or 'unknown'}",
    )


def check_secret_scan(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> Check:
    from .tracked_files import GitBaselineUnavailable

    allowlist = read_secret_scan_allowlist(root)
    flagged: list[str] = []
    acknowledged: list[str] = []
    try:
        report = secret_scan_report(
            root,
            incremental=incremental,
            update_state=update_state,
        )
    except GitBaselineUnavailable:
        mode = "incremental" if incremental else "full"
        return Check(
            "secret_scan",
            False,
            f"Git tracked-file baseline unavailable; mode={mode}; remediation: restore Git access and rerun doctor",
        )
    for hit in report["hits"]:
        (acknowledged if hit in allowlist else flagged).append(hit)
    scan_detail = (
        f"mode={report['mode']} baseline={report['baseline']} total={report['total']} "
        f"reused={report['reused']} "
        f"rescanned={report['rescanned']} unreadable={report['unreadable']} "
        f"unstable={report['unstable']}"
    )
    if flagged:
        detail = (
            f"flagged={len(flagged)} acknowledged={len(acknowledged)} "
            f"allowlist=.ai/secret_scan_allowlist.txt: " + ", ".join(flagged[:10]) + f"; {scan_detail}"
        )
        return Check("secret_scan", False, detail)
    if acknowledged:
        return Check(
            "secret_scan",
            True,
            f"ok (flagged=0 acknowledged={len(acknowledged)} via allowlist); {scan_detail}",
        )
    return Check("secret_scan", True, f"ok; {scan_detail}")


def read_secret_scan_allowlist(root: Path) -> set[str]:
    entries: set[str] = {
        ".ai/runtime/tests/test_failure_memory.py",
        ".ai/runtime/tests/test_posttooluse_wire.py",
    }
    path = root / ".ai" / "secret_scan_allowlist.txt"
    if not path.exists():
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped)
    return entries


FORBIDDEN_TOKEN_ESTIMATE_KEYWORDS = (
    "estimated_tokens",
    "tokens_estimated",
    "tokens_saved",
    "token_savings",
    "estimated_token_savings",
    "estimate_tokens(",
    "guess_tokens(",
)

TOKEN_ESTIMATE_GUARDED_FILES = (
    "obs.py",
    "report.py",
    "session.py",
    "transcripts.py",
    "search.py",
)


def check_no_token_estimates(root: Path) -> Check:
    base = root / ".ai" / "runtime" / "src" / "ai_core"
    if not base.exists():
        return Check("no_token_estimates", True, "ai_core not found")
    offenders: list[str] = []
    for name in TOKEN_ESTIMATE_GUARDED_FILES:
        path = base / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for keyword in FORBIDDEN_TOKEN_ESTIMATE_KEYWORDS:
            if keyword in text:
                offenders.append(f"{name}:{keyword}")
    if offenders:
        return Check("no_token_estimates", False, "estimates leaked: " + ", ".join(offenders[:10]))
    return Check("no_token_estimates", True, f"ok ({len(TOKEN_ESTIMATE_GUARDED_FILES)} files scanned)")


REQUIRED_SLASH_COMMAND_FILES = (
    ".claude/commands/cb-usage.md",
    ".claude/commands/cb-health.md",
    ".claude/commands/cb-search.md",
    ".claude/commands/cb-doctor.md",
    ".claude/commands/cb-exec.md",
    ".claude/commands/cb-upgrade.md",
    ".claude/commands/cb-proof.md",
)

REQUIRED_CODEX_PROMPT_FILES = (
    ".codex/prompts/cb-usage.md",
    ".codex/prompts/cb-health.md",
    ".codex/prompts/cb-search.md",
    ".codex/prompts/cb-doctor.md",
    ".codex/prompts/cb-exec.md",
    ".codex/prompts/cb-upgrade.md",
    ".codex/prompts/cb-proof.md",
)


def check_mcp_methods_registered(root: Path) -> Check:
    from .mcp_catalog_meta import MCP_METHOD_COUNT

    mcp_config = root / ".mcp.json"
    if not mcp_config.exists():
        return Check("mcp_methods_registered", False, ".mcp.json missing")
    try:
        config = json.loads(mcp_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("mcp_methods_registered", False, f".mcp.json invalid: {exc}")
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or "code-brain" not in servers:
        return Check("mcp_methods_registered", False, ".mcp.json missing mcpServers.code-brain entry")
    server = servers["code-brain"]
    if not isinstance(server, dict):
        return Check("mcp_methods_registered", False, ".mcp.json code-brain server is not an object")
    command = server.get("command")
    args = server.get("args", [])
    arg_text = " ".join(str(arg) for arg in args)
    unix_entry = command == ".ai/bin/ai-mcp"
    windows_entry = command in {"powershell", "pwsh"} and ".ai/bin/ai-mcp.ps1" in arg_text
    if not (unix_entry or windows_entry):
        return Check("mcp_methods_registered", False, ".mcp.json code-brain command is not a managed ai-mcp entry")
    missing_slash = [path for path in REQUIRED_SLASH_COMMAND_FILES if not (root / path).exists()]
    missing_codex = [path for path in REQUIRED_CODEX_PROMPT_FILES if not (root / path).exists()]
    if missing_slash:
        return Check("mcp_methods_registered", False, "missing claude commands: " + ", ".join(missing_slash))
    if missing_codex:
        return Check("mcp_methods_registered", False, "missing codex prompts: " + ", ".join(missing_codex))
    return Check(
        "mcp_methods_registered",
        True,
        f"ok mcp_methods={MCP_METHOD_COUNT} claude_commands={len(REQUIRED_SLASH_COMMAND_FILES)} "
        f"codex_prompts={len(REQUIRED_CODEX_PROMPT_FILES)}",
    )


def check_redaction_self_test() -> Check:
    samples = [
        "AKIA" + "A" * 16,
        "ghp_" + "a" * 36,
        "gho_" + "b" * 36,
        "github_pat_" + "c" * 28,
        "sk-" + "d" * 32,
        "sk-ant-" + "e" * 32,
        "xoxb-" + "1-2-" + "f" * 24,
        "Authorization: Bearer " + "eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + "." + "c" * 20,
        "token=" + "g" * 24,
        "-----BEGIN " + "PRIVATE KEY-----\n" + "h" * 32 + "\n-----END " + "PRIVATE KEY-----",
        "/Users/example/project",
        "/home/example/project",
        "C:\\Users\\example\\project",
        "192.168.1.10",
    ]
    redacted = redact_value({"samples": samples})
    text = json.dumps(redacted, sort_keys=True)
    leaked = [sample for sample in samples if sample in text]
    return Check("redaction_self_test", not leaked and "[REDACTED]" in text, "ok" if not leaked else "leaked: " + str(len(leaked)))


def _root_confined_regular_file(root: Path, path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        state = path.stat()
        if not stat.S_ISREG(state.st_mode):
            return False
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def check_bootstrap_preflight(root: Path) -> Check:
    script = root / "scripts" / "preflight.sh"
    if not _root_confined_regular_file(root, script):
        return Check(
            "bootstrap_preflight",
            False,
            "scripts/preflight.sh must be a root-confined regular file",
        )
    proof = _fresh_bootstrap_preflight_proof(root, script)
    if proof is not None:
        return proof
    command = [str(script), "--check-only", "--json"]
    env = os.environ.copy()
    # Reuse the already-running, verified runtime instead of letting a copied
    # repository trigger an implicit `uv run` sync during a read-only doctor.
    env["PYTHON"] = sys.executable
    if os.name == "nt":
        bash = shutil.which("bash") or shutil.which("bash.exe")
        if not bash:
            for candidate in (
                Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    bash = str(candidate)
                    break
        if not bash:
            return Check("bootstrap_preflight", False, "bash not found")
        command = [bash, str(script), "--check-only", "--json"]
        scripts_dir = root / ".ai" / "runtime" / ".venv" / "Scripts"
        env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        return Check("bootstrap_preflight", False, str(exc))
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return Check("bootstrap_preflight", False, detail[:500])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return Check("bootstrap_preflight", False, f"invalid json: {exc}")
    return Check("bootstrap_preflight", payload.get("ok") is True, "ok" if payload.get("ok") is True else "failed")


def _fresh_bootstrap_preflight_proof(root: Path, script: Path) -> Check | None:
    proof_path = root / ".ai" / "cache" / "preflight-proof.json"
    try:
        if not proof_path.is_file() or proof_path.is_symlink():
            return None
        resolved_root = root.resolve()
        proof_path.resolve().relative_to(resolved_root)
        proof_state = proof_path.stat()
        if os.name != "nt":
            if stat.S_IMODE(proof_state.st_mode) & 0o077:
                return None
            if hasattr(os, "geteuid") and proof_state.st_uid != os.geteuid():
                return None
        proof_text, proof_state = read_root_confined_text(
            proof_path,
            root=root,
            max_bytes=65536,
            require_private=True,
        )
        payload = json.loads(proof_text)
        created_at = float(payload.get("created_at_unix", 0))
        age = time.time() - created_at
        if age < -5 or age > PROOF_MAX_AGE_SECONDS:
            return None
        if payload.get("schema") != PROOF_SCHEMA or payload.get("ok") is not True:
            return None
        expected_script = hashlib.sha256(script.read_bytes()).hexdigest()
        if payload.get("preflight_sha256") != expected_script:
            return None
        expected_root = hashlib.sha256(str(resolved_root).encode("utf-8")).hexdigest()
        if payload.get("root_fingerprint") != expected_root:
            return None
        if payload.get("environment_fingerprint") != environment_fingerprint(resolved_root):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return Check("bootstrap_preflight", True, "ok (fresh bootstrap proof)")


def check_worker_singleton_lock(root: Path) -> Check:
    from .worker.lock import lock_status

    status = lock_status(root)
    if status.get("stale"):
        return Check("worker_singleton_lock", False, json.dumps(status, sort_keys=True))
    return Check("worker_singleton_lock", status.get("ok") is True, "ok" if status.get("ok") is True else json.dumps(status, sort_keys=True))


def check_queue_lease_recovery(root: Path) -> Check:
    from .worker.scheduler import RECOVERY_STALE_SECONDS, expired_processing_jobs, recovery_status

    expired = expired_processing_jobs(root)
    state = recovery_status(root)
    if expired:
        return Check("queue_lease_recovery", False, "expired processing jobs: " + json.dumps(expired[:5], sort_keys=True))
    if state.get("state") == "invalid":
        return Check("queue_lease_recovery", False, "invalid recovery state")
    lag = state.get("lag_seconds")
    if isinstance(lag, int) and lag > RECOVERY_STALE_SECONDS:
        return Check("queue_lease_recovery", False, f"recovery state stale lag={lag}s")
    detail = "ok" if lag is None else f"ok lag={lag}s"
    return Check("queue_lease_recovery", True, detail)


def check_queue_age(root: Path) -> Check:
    from .worker.scheduler import QUEUE_PENDING_AGE_STALE_SECONDS, QUEUE_PROCESSING_AGE_STALE_SECONDS, queue_age_stats

    stats = queue_age_stats(root)
    pending_age = int(stats["oldest_pending_age_seconds"])
    processing_age = int(stats["oldest_processing_age_seconds"])
    failures = []
    if pending_age > QUEUE_PENDING_AGE_STALE_SECONDS:
        failures.append(
            "oldest pending job "
            f"{stats.get('oldest_pending_job_id')} age={pending_age}s threshold={QUEUE_PENDING_AGE_STALE_SECONDS}s"
        )
    if processing_age > QUEUE_PROCESSING_AGE_STALE_SECONDS:
        failures.append(
            "oldest processing job "
            f"{stats.get('oldest_processing_job_id')} age={processing_age}s threshold={QUEUE_PROCESSING_AGE_STALE_SECONDS}s"
        )
    if failures:
        return Check("queue_age", False, "; ".join(failures))
    skipped = int(stats.get("age_stats_skipped", 0))
    detail = f"ok pending_age={pending_age}s processing_age={processing_age}s"
    if skipped:
        detail += f" skipped={skipped}"
    return Check("queue_age", True, detail)


def check_diagnostics(root: Path) -> Check:
    try:
        from .obs import diagnostics

        # Doctor only needs to prove that diagnostics can be assembled. Full
        # Claude/Codex transcript scans belong to an explicit diagnostics or
        # metrics request and can take seconds on a long-lived workstation.
        payload = diagnostics(root, dry_run=True, include_doctor=False, include_usage=False)
    except PermissionError as exc:
        # diagnostics walks metrics paths which may include files outside the
        # Code Brain managed tree (e.g. ~/.claude/projects/*.jsonl owned by a
        # different user when Code Brain is invoked under sudo on a shared
        # host). That is an environment fact, not a Code Brain failure —
        # skip with detail instead of failing strict.
        return Check("diagnostics_dry_run", True, f"skipped: permission denied ({exc})")
    except Exception as exc:
        return Check("diagnostics_dry_run", False, str(exc))
    return Check("diagnostics_dry_run", bool(payload.get("ok")), "ok" if payload.get("ok") else "failed")


SECRET_SCAN_IGNORED_PARTS = {
    ".venv",
    "cache",
    ".git",
    ".claude",
    ".codebrain",
    "node_modules",
    ".next",
    ".nuxt",
    ".output",
    "dist",
    "build",
    "coverage",
    "logs",
    ".playwright-mcp",
    ".dart_tool",
    "source-maps",
}

SECRET_SCAN_IGNORED_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "poetry.lock",
    "firebase_options.dart",
}

SECRET_SCAN_IGNORED_SUFFIXES = {
    ".map",
    ".min.js",
    ".min.css",
}


class _SecretCandidateList(list[Path]):
    def __init__(self, paths: list[Path], *, baseline: str) -> None:
        super().__init__(paths)
        self.baseline = baseline


def _secret_scan_candidates(
    root: Path,
    *,
    use_tracked_cache: bool = True,
    update_tracked_cache: bool = True,
) -> _SecretCandidateList:
    candidates: list[Path] = []
    baseline_paths = secret_scan_files(
        root,
        use_cache=use_tracked_cache,
        update_cache=update_tracked_cache,
    )
    baseline = str(getattr(baseline_paths, "source", "provided"))
    for path in baseline_paths:
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & SECRET_SCAN_IGNORED_PARTS:
            continue
        if path.name in SECRET_SCAN_IGNORED_NAMES:
            continue
        if any(path.name.endswith(suffix) for suffix in SECRET_SCAN_IGNORED_SUFFIXES):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".sqlite", ".db", ".rdb", ".zip", ".gz", ".tar"}:
            continue
        try:
            state = path.lstat()
        except OSError:
            # A tracked path can disappear or be replaced between the Git
            # baseline read and candidate filtering. Preserve the existing
            # fail-soft unreadable-file policy instead of aborting doctor.
            continue
        if not (stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode)):
            continue
        if state.st_size > 1_000_000:
            continue
        if path.name.endswith(".enc.yaml") or path.name.endswith(".enc.yml"):
            continue
        candidates.append(path)
    return _SecretCandidateList(candidates, baseline=baseline)


def secret_hits(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> Iterable[str]:
    yield from secret_scan_report(
        root,
        incremental=incremental,
        update_state=update_state,
    )["hits"]


def secret_scan_report(
    root: Path,
    *,
    incremental: bool = False,
    update_state: bool = True,
) -> dict[str, object]:
    from .scan_state import scan_paths_report

    candidates = _secret_scan_candidates(
        root,
        use_tracked_cache=incremental,
        update_tracked_cache=update_state,
    )
    report = scan_paths_report(
        root,
        candidates,
        incremental=incremental,
        update_state=update_state,
    )
    report["baseline"] = candidates.baseline
    return report


def secret_scan_files(
    root: Path,
    *,
    use_cache: bool = True,
    update_cache: bool = True,
) -> list[Path]:
    from .tracked_files import tracked_files

    return tracked_files(root, use_cache=use_cache, update_cache=update_cache)


def as_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks],
    }
