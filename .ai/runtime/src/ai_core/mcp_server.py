from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .doctor import as_payload, run_checks
from .memory import (
    append_decision,
    append_event,
    append_session_note,
    append_todo,
    close_todo,
)
from .obs import health_summary, search_report, usage_report
from .policy import is_ci, reject_ci_write
from .redact import redact_value
from .sandbox import execute as sandbox_execute, fetch as sandbox_fetch, list_executions as sandbox_list
from .search import context_pack, query, rebuild
from .worker.ipc import health

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_NAME = "code-brain"

# Tool catalog. Each entry is exposed via tools/list and dispatched via tools/call.
# Description text is short; the inputSchema follows JSON Schema (draft 2020-12 compatible).
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "memory_query",
        "description": "BM25 search over indexed source. Returns top-K snippets with provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "code_query",
        "description": "Alias of memory_query — BM25 code search.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "context_pack",
        "description": "BM25 query plus an additionalContext string suitable for hook injection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "mode": {
                    "type": "string",
                    "enum": ["high_fidelity", "balanced", "aggressive"],
                    "default": "balanced",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "code_graph_callers",
        "description": "Function-call graph reverse lookup: who calls this qualname? Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "qualname": {"type": "string", "description": "Function/method qualname (e.g. 'append_audit' or 'C.method')"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["qualname"],
        },
    },
    {
        "name": "code_graph_callees",
        "description": "Function-call graph forward lookup: what does this qualname call? Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "qualname": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["qualname"],
        },
    },
    {
        "name": "code_graph_symbol",
        "description": "Locate function/class definitions by qualname fragment. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Substring to match against qualname (LIKE %name%)"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["name"],
        },
    },
    {
        "name": "code_graph_hotspots",
        "description": "Most-called callees across the indexed codebase. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "code_verify",
        "description": "AST-based policy gate: rejects forbidden imports/calls/sandbox escapes. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string", "description": "Python source to verify"}},
            "required": ["source"],
        },
    },
    {
        "name": "code_read_hashline",
        "description": "Read a repo file with line+hash anchors for stale-edit detection. Read-only; refuses credential-like paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "stream_guard_scan",
        "description": "Scan text with Code Brain stream-guard rules. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["tool", "prompt", "output"], "default": "tool"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_tier",
        "description": "MemGPT-style hot/warm/cold memory classification + page-out signal + retention scoring (decay/reinforcement) of decisions/lessons/procedures. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ai_status",
        "description": "Worker health envelope.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ai_request_rebuild",
        "description": "Force-rebuild the SQLite FTS5 code index. Write-class.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_usage",
        "description": "Token usage + Code Brain effect bytes. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_sessions": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "obs_health_summary",
        "description": "Doctor + queue + worker + index roll-up. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "obs_search",
        "description": "BM25 query with stale-detection report.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
    },
    {
        "name": "doctor_strict",
        "description": "Run all doctor checks and return the full payload. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sandbox_execute",
        "description": (
            "Run shell in sandbox; returns summary+exec_id, full output on disk. Write-class. "
            "command accepts either an argv array (e.g. [\"git\", \"log\"]) or a single "
            "shell string (run under `bash -lc`) so heredocs/pipes work without JSON escaping. "
            "For small outputs (<=20 lines / <=1KB) the response replaces first_lines/last_lines with a single `output` field."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    ]
                },
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "sandbox_fetch",
        "description": "Fetch a line range or grep filter from a stored sandbox execution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "exec_id": {"type": "string"},
                "line_start": {"type": "integer", "default": 1},
                "line_end": {"type": "integer"},
                "grep_pattern": {"type": "string"},
            },
            "required": ["exec_id"],
        },
    },
    {
        "name": "sandbox_list",
        "description": "List recent sandbox executions (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "record_decision",
        "description": (
            "Persist a decision (or a re-testable failure) to .ai/memory/decisions.jsonl. "
            "Auto-injected next session. For a failure/negative result set kind='failure' and "
            "record observed_versions/environment/retest_after so it reads as a dated, re-testable "
            "observation — NOT a permanent ban. A later success retires it: kind='failure', "
            "status='refuted', supersedes_id=<original id>. Write-class."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
                "kind": {"type": "string", "enum": ["decision", "failure"]},
                "observed_at": {"type": "string", "description": "ISO-8601; when the failure was reproduced"},
                "observed_versions": {"type": "object", "description": "versions seen under, e.g. {torch: 2.4.0}"},
                "environment": {"type": "string", "description": "scope: host/preset/codepath"},
                "retest_after": {"type": "string", "description": "ISO date; re-test backstop"},
                "status": {"type": "string", "enum": ["observed", "confirmed", "stale", "refuted"]},
                "supersedes_id": {"type": "string", "description": "retire this prior failure id"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "ast_grep_search",
        "description": (
            "Structural (AST) code search: find code matching a syntactic pattern in a language "
            "(e.g. pattern 'except: $$$' lang python, or 'fetch($URL)' lang ts). Precise refactor/"
            "audit retrieval BM25 cannot do. Read-only, repo-scoped. Use code_query for intent/keyword search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "ast-grep pattern; $VAR / $$$ metavars"},
                "lang": {"type": "string", "description": "python|javascript|typescript|tsx|go|rust|java|..."},
                "path": {"type": "string", "description": "optional repo-relative subpath to scope"},
                "max_results": {"type": "integer", "default": 40},
            },
            "required": ["pattern", "lang"],
        },
    },
    {
        "name": "lessons_recall",
        "description": (
            "Recall distilled lessons relevant to a query (failure-prevention strategies mined "
            "from past runs), ranked by confidence*relevance*recency. Read-only. Call before a "
            "risky/repeated task to reuse prior experience."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "record_todo",
        "description": "Persist open todo to .ai/memory/todos.jsonl. Auto-injected next session. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "close_todo",
        "description": "Close a todo by id or title substring. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "match": {"type": "string"},
                "status": {"type": "string", "enum": ["done", "closed", "cancelled", "canceled"], "default": "done"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["match"],
        },
    },
    {
        "name": "append_session_note",
        "description": "Append milestone line to .ai/memory/session-current.md. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "evidence_list",
        "description": "List latest repo-local evidence records from .ai/memory/evidence.jsonl. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"]},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "evidence_record",
        "description": "Record an explicit repo-local evidence item. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"], "default": "candidate"},
                "snippet": {"type": "string", "default": ""},
                "source": {"type": "string", "default": "agent"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["query", "path"],
        },
    },
    {
        "name": "evidence_set_status",
        "description": "Promote or reject an evidence record. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["candidate", "curated", "verified", "rejected"]},
                "note": {"type": "string", "default": ""},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "security_finding_list",
        "description": "List latest repo-local security findings from .ai/memory/security-findings.jsonl. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "security_finding_record",
        "description": "Record a redacted security finding with summary/hash evidence. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "affected_path": {"type": "string"},
                "finding_type": {"type": "string"},
                "detail_summary": {"type": "string"},
                "evidence_hash": {"type": "string", "default": ""},
                "repro_command": {"type": "string"},
                "verification_command": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"], "default": "open"},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["affected_path", "finding_type", "detail_summary", "repro_command", "verification_command"],
        },
    },
    {
        "name": "security_finding_update",
        "description": "Update a security finding status after verification. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "verified_fixed", "accepted_risk", "false_positive"]},
                "verification_command": {"type": "string"},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["id", "status", "verification_command"],
        },
    },
    {
        "name": "append_handoff",
        "description": (
            "Set/update the resume HANDOFF (goal/plan/next_step/open_questions/blockers) at a "
            "stopping point. Git-tracked so it travels across machines (Mac↔VPS); the next session "
            "— any agent, either machine — leads its SessionStart context with it. Partial update: "
            "only provided fields change. Write-class. Call this before pausing work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What we are ultimately trying to do"},
                "next_step": {"type": "string", "description": "The very next action to take on resume"},
                "plan": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "agent": {"type": "string", "default": "agent"},
                "clear": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "recommend_skills",
        "description": "Propose slash-command skills from cross-session memory. May persist pending catalog entries; does not install.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5},
                "include_global": {"type": "boolean", "default": True},
                "min_signal": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "recommend_skills_accept",
        "description": "Install candidate slash command. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "recommend_skills_reject",
        "description": "Mark a candidate as rejected so it is not surfaced again. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "skills_list",
        "description": "List catalog entries (pending/installed/rejected/uninstalled). Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "skills_uninstall",
        "description": "Uninstall skill; rejects on drift unless force=true. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "precall_recommend",
        "description": "Propose precall rules from accumulated Bash invocations. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5},
                "min_signal": {"type": "integer", "default": 5},
                "include_transcripts": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "precall_list",
        "description": "List precall rule catalog. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "precall_accept",
        "description": "Promote pending → dry_run (safety probe + regex compile). Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "precall_activate",
        "description": "Promote dry_run → active; refuses if observed<required unless force=true. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["id"],
        },
    },
    {
        "name": "precall_reject",
        "description": "Mark a candidate as rejected (no longer surfaced). Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "precall_disable",
        "description": "Disable an active or dry_run rule. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "federated_summary",
        "description": "Cross-project pattern counts (no raw text leak). Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agents_recommend",
        "description": "Propose .claude/agents/<slug>.md from transcripts+decisions. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5},
                "min_signal": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "agents_list",
        "description": "List agent catalog entries. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agents_accept",
        "description": "Install a candidate sub-agent definition into .claude/agents. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "agents_reject",
        "description": "Mark an agent candidate as rejected. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "agents_uninstall",
        "description": "Uninstall agent; rejects on drift unless force=true. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["slug"],
        },
    },
    # remote_memory_* tools removed (T37) — .ai/ git sync replaces Cloudflare round-trip.
    # ---- Innovation modules (PoC; safe — no hot-path mutation) ----
    {
        "name": "lsp_available",
        "description": "Detect LSP backend readiness (multilspy + language servers on PATH).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lsp_find_references",
        "description": "LSP find_references — precise cross-file reference graph for a symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "line": {"type": "integer"},
                "column": {"type": "integer"},
            },
            "required": ["file_path", "line", "column"],
        },
    },
    {
        "name": "lsp_goto_definition",
        "description": "LSP goto_definition for a symbol at file:line:column.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "line": {"type": "integer"},
                "column": {"type": "integer"},
            },
            "required": ["file_path", "line", "column"],
        },
    },
    {
        "name": "lsp_workspace_symbols",
        "description": "LSP workspace_symbols — fuzzy symbol search across the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "speculative_mine_patterns",
        "description": "Mine 2-gram tool-call patterns from audit/2026.jsonl for speculative execution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_support": {"type": "integer", "default": 3},
                "min_confidence": {"type": "number", "default": 0.5},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "speculative_hit_rate",
        "description": "Speculative-execution hit/miss summary from .ai/cache/speculative.jsonl.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "trajectory_summarize",
        "description": "TRAJEVAL-style trajectory diagnosis (efficiency + failure mode) across recent sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "autoresearch_search",
        "description": "AutoResearch knowledge-wiki FTS5 BM25 search (Stage 0). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["q"],
        },
    },
    {
        "name": "autoresearch_ingest_stage",
        "description": "AutoResearch ingest phase 1: persist immutable raw + manifest (idempotent on sha256), return nonce-wrapped data for the agent to summarize. Provide `content` (local) OR `url` (Stage 3, SSRF-guarded HTTPS fetch). Web content is untrusted (quarantined if flagged). Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "url": {"type": "string"},
                "source_url": {"type": "string"},
                "title": {"type": "string"},
                "trust_tier": {"type": "string"},
            },
        },
    },
    {
        "name": "autoresearch_ingest_commit",
        "description": "AutoResearch ingest phase 2: verify-det gate, then write agent-authored wiki pages + FTS + log. Failing citations are quarantined as status:draft. Write-class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "pages": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["source_id", "pages"],
        },
    },
    {
        "name": "autoresearch_lint",
        "description": "AutoResearch wiki health lint (Stage 0): orphan / draft / taint / stale pages. Read-only, no auto-fix.",
        "inputSchema": {
            "type": "object",
            "properties": {"stale_before": {"type": "string"}},
        },
    },
    {
        "name": "autoresearch_query",
        "description": "AutoResearch knowledge query (Stage 0): FTS5 retrieval with per-page trust signals. Draft/taint pages are quarantined out of candidates (laundering defense); the calling agent writes the cited answer.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["question"],
        },
    },
    {
        "name": "autoresearch_verify",
        "description": "AutoResearch deterministic citation verification (Stage 3): scores each claim's quote against its cited source texts (faithfulness in [0,1], no LLM). The agent uses the score to accept/hedge/reject; factuality judgment is the agent's job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claims": {"type": "array", "items": {"type": "object"}},
                "long_tail_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["claims"],
        },
    },
    {
        "name": "autoresearch_deepresearch_start",
        "description": "Stage 3: start a deep-research session. Runtime tracks state only; the agent does plan→fetch (autoresearch_ingest_stage with url)→synthesize→commit. Returns the session.",
        "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        "name": "autoresearch_deepresearch_update",
        "description": "Stage 3: update a deep-research session (subquestions / add_source / status). Size-capped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "subquestions": {"type": "array", "items": {"type": "string"}},
                "add_source": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "autoresearch_deepresearch_status",
        "description": "Stage 3: get a deep-research session state by session_id.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "autoresearch_route",
        "description": "Stage 4: suggest a model tier (local/frontier) for a query via a deterministic complexity heuristic (RouteLLM-style). The agent makes the final model choice. No LLM.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "autoresearch_survey_plan",
        "description": "Stage 4: gate breadth-first multi-agent fan-out (orchestrator-worker). Returns single vs multi recommendation, a bounded worker list, and the ~15x cost warning. Deterministic policy, not an executor. No LLM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subtopics": {"type": "array", "items": {"type": "string"}},
                "independent": {"type": "boolean"},
                "max_workers": {"type": "integer"},
            },
            "required": ["subtopics"],
        },
    },
    {
        "name": "autoresearch_loop_start",
        "description": "Stage 2 (OFF by default; autoresearch.loop.enable): start a metric ratchet loop. Runtime tracks state + budget; the agent does git (worktree/commit/reset) and edits. metric_cmd must be a user-trusted command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "metric_cmd": {"type": ["string", "array"], "items": {"type": "string"}},
                "metric_grep": {"type": "string"},
                "direction": {"type": "string", "enum": ["minimize", "maximize"]},
                "edit_surface": {"type": "array", "items": {"type": "string"}},
                "max_iters": {"type": "integer"},
                "max_cost_usd": {"type": "number"},
                "per_run_timeout_s": {"type": "integer"},
            },
            "required": ["workspace", "metric_cmd", "metric_grep", "direction"],
        },
    },
    {
        "name": "autoresearch_loop_record",
        "description": "Stage 2: run one ratchet evaluation (metric in the hardened sandbox — network+env isolated). Returns decision keep|discard|crash + best + should_continue. The agent git-resets on discard/crash.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "cost_spent": {"type": "number"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "autoresearch_loop_status",
        "description": "Stage 2: get a ratchet loop session state by session_id.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
    {
        "name": "autoresearch_loop_stop",
        "description": "Stage 2: stop a ratchet loop (no auto-merge; a human reviews the best commit).",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
    },
)

MCP_METHODS = tuple(tool["name"] for tool in TOOLS)
TOOL_NAMES = frozenset(MCP_METHODS)

# tools/list payload is static within a process lifetime (TOOLS is a module-level
# constant). Profiling on real projects showed tools/list being called 100+ times
# per session with a ~8KB response — pure waste. Cache once and reuse.
#
# Safety:
#  - The cached value is the inner "result" payload (a dict {"tools": [...]}).
#  - handle_request wraps it in _ok(request_id, cached) — request_id is fresh.
#  - redact_value walks the response and returns a fresh copy, so the cached
#    payload itself is never mutated by downstream callers.
_TOOLS_LIST_CACHE: dict[str, Any] | None = None


def _build_tools_list_payload() -> dict[str, Any]:
    """Build the tools/list result payload. Pure function over module constants."""
    return {"tools": [dict(tool) for tool in TOOLS]}


def _get_tools_list_payload() -> dict[str, Any]:
    """Return the cached tools/list payload, building it on first call."""
    global _TOOLS_LIST_CACHE
    if _TOOLS_LIST_CACHE is None:
        _TOOLS_LIST_CACHE = _build_tools_list_payload()
    return _TOOLS_LIST_CACHE


def _invalidate_tools_list_cache() -> None:
    """Reset the tools/list cache. Reserved for hot-reload / test scenarios."""
    global _TOOLS_LIST_CACHE
    _TOOLS_LIST_CACHE = None


def _dispatch_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the underlying handler for a tool by name. Raises KeyError if unknown."""
    args = arguments or {}
    if name == "autoresearch_search":
        from .autoresearch import storage as _ars, hybrid as _arh
        return {"results": _arh.search(_ars.data_root(root), str(args.get("q", "")), k=int(args.get("k", 10) or 10))}
    if name == "autoresearch_ingest_stage":
        from .autoresearch import storage as _ars, ingest as _ari
        content = args.get("content")
        url = args.get("url")
        has_content = isinstance(content, str) and content
        has_url = isinstance(url, str) and url
        if not has_content and not has_url:
            raise ValueError("autoresearch_ingest_stage requires non-empty content or url")
        return _ari.stage_source(
            _ars.data_root(root),
            content=content if has_content else None,
            url=url if has_url else None,
            source_url=str(args.get("source_url", "")), title=str(args.get("title", "")),
            trust_tier=str(args.get("trust_tier", "untrusted")),
        )
    if name == "autoresearch_ingest_commit":
        from .autoresearch import storage as _ars, ingest as _ari
        sid = args.get("source_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_ingest_commit requires source_id")
        pages = args.get("pages")
        if not isinstance(pages, list):
            raise ValueError("autoresearch_ingest_commit requires pages array")
        return _ari.commit_pages(_ars.data_root(root), source_id=sid, pages=pages)
    if name == "autoresearch_lint":
        from .autoresearch import storage as _ars, lint as _arl
        sb = args.get("stale_before")
        return _arl.lint(_ars.data_root(root), stale_before=str(sb) if isinstance(sb, str) and sb else None)
    if name == "autoresearch_query":
        from .autoresearch import storage as _ars, query as _arq
        return _arq.query(_ars.data_root(root), str(args.get("question", "")), k=int(args.get("k", 10) or 10))
    if name == "autoresearch_verify":
        from .autoresearch import storage as _ars, verify as _arv
        claims = args.get("claims")
        if not isinstance(claims, list):
            raise ValueError("autoresearch_verify requires claims array")
        lt = args.get("long_tail_ids")
        return _arv.verify_claims(_ars.data_root(root), claims, long_tail_ids=lt if isinstance(lt, list) else None)
    if name == "autoresearch_deepresearch_start":
        from .autoresearch import storage as _ars, deepresearch as _dr
        q = args.get("question")
        if not isinstance(q, str) or not q:
            raise ValueError("autoresearch_deepresearch_start requires question")
        return _dr.start(_ars.data_root(root), q)
    if name == "autoresearch_deepresearch_update":
        from .autoresearch import storage as _ars, deepresearch as _dr
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_deepresearch_update requires session_id")
        res = _dr.update(
            _ars.data_root(root), sid,
            subquestions=args.get("subquestions") if isinstance(args.get("subquestions"), list) else None,
            add_source=args.get("add_source") if isinstance(args.get("add_source"), str) else None,
            status=args.get("status") if isinstance(args.get("status"), str) else None,
        )
        return res if res is not None else {"error": "session_not_found_or_invalid"}
    if name == "autoresearch_deepresearch_status":
        from .autoresearch import storage as _ars, deepresearch as _dr
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_deepresearch_status requires session_id")
        res = _dr.get(_ars.data_root(root), sid)
        return res if res is not None else {"error": "session_not_found"}
    if name == "autoresearch_route":
        from .autoresearch import complexity_router as _cr
        return _cr.classify(str(args.get("query", "")))
    if name == "autoresearch_survey_plan":
        from .autoresearch import orchestration as _orch
        return _orch.survey_plan(
            args.get("subtopics", []),
            independent=bool(args.get("independent", False)),
            max_workers=args.get("max_workers", _orch.DEFAULT_MAX_WORKERS),
        )
    if name == "autoresearch_loop_start":
        from .autoresearch import loop as _loop
        return _loop.start(
            root,
            workspace=str(args.get("workspace", "")),
            metric_cmd=args.get("metric_cmd"),
            metric_grep=str(args.get("metric_grep", "")),
            direction=str(args.get("direction", "")),
            edit_surface=args.get("edit_surface"),
            max_iters=args.get("max_iters", 50),
            max_cost_usd=args.get("max_cost_usd", 0.0),
            per_run_timeout_s=args.get("per_run_timeout_s", 600),
        )
    if name == "autoresearch_loop_record":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_record requires session_id")
        return _loop.record(root, sid, cost_spent=args.get("cost_spent", 0.0))
    if name == "autoresearch_loop_status":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_status requires session_id")
        return _loop.status(root, sid)
    if name == "autoresearch_loop_stop":
        from .autoresearch import loop as _loop
        sid = args.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("autoresearch_loop_stop requires session_id")
        return _loop.stop(root, sid)
    if name in ("memory_query", "code_query"):
        return query(root, str(args.get("query", "")), limit=int(args.get("limit", 5) or 5), evidence_source="search")
    if name == "context_pack":
        return context_pack(
            root,
            str(args.get("query", "")),
            limit=int(args.get("limit", 5) or 5),
            mode=str(args.get("mode", "balanced") or "balanced"),
        )
    if name == "code_graph_callers":
        from .codegraph import query_callers
        return query_callers(root, str(args.get("qualname", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_callees":
        from .codegraph import query_callees
        return query_callees(root, str(args.get("qualname", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_symbol":
        from .codegraph import find_symbol
        return find_symbol(root, str(args.get("name", "")), limit=int(args.get("limit", 20) or 20))
    if name == "code_graph_hotspots":
        from .codegraph import hotspot_callees
        return hotspot_callees(root, limit=int(args.get("limit", 20) or 20))
    if name == "code_verify":
        from .ast_verify import verify_source
        return verify_source(str(args.get("source", ""))).to_dict()
    if name == "code_read_hashline":
        from .hashline import read_hashline
        target = args.get("path")
        if not isinstance(target, str) or not target:
            raise ValueError("code_read_hashline requires path string")
        return read_hashline(
            root,
            target,
            start=(int(args["start"]) if isinstance(args.get("start"), int) else None),
            end=(int(args["end"]) if isinstance(args.get("end"), int) else None),
        )
    if name == "stream_guard_scan":
        from .stream_guard import scan_text
        return scan_text(str(args.get("text", "")), scope=str(args.get("scope", "tool") or "tool"))
    if name == "memory_tier":
        from .memory_tier import classify, hot_pressure, retention_report
        cls = classify(root)
        pres = hot_pressure(root)
        return {**cls, "pressure": pres, "retention": retention_report(root)}
    if name == "ai_status":
        return health(root)
    if name == "ai_request_rebuild":
        return rebuild(root)
    if name == "obs_usage":
        return usage_report(root, include_sessions=bool(args.get("include_sessions", False)))
    if name == "obs_health_summary":
        return health_summary(root)
    if name == "obs_search":
        return search_report(root, query_text=args.get("query"), limit=int(args.get("limit", 5) or 5))
    if name == "doctor_strict":
        return as_payload(run_checks(root))
    if name == "sandbox_execute":
        command = args.get("command")
        if isinstance(command, str):
            if not command.strip():
                raise ValueError("sandbox_execute requires non-empty command")
            command_payload: list[str] | str = command
        elif isinstance(command, list) and command:
            command_payload = [str(part) for part in command]
        else:
            raise ValueError("sandbox_execute requires command as non-empty string or array")
        return sandbox_execute(
            root,
            command=command_payload,
            cwd=str(args["cwd"]) if isinstance(args.get("cwd"), str) else None,
            timeout=int(args.get("timeout", 30) or 30),
        )
    if name == "sandbox_fetch":
        exec_id = args.get("exec_id")
        if not isinstance(exec_id, str) or not exec_id:
            raise ValueError("sandbox_fetch requires exec_id string")
        return sandbox_fetch(
            root,
            exec_id=exec_id,
            line_start=int(args.get("line_start", 1) or 1),
            line_end=(int(args["line_end"]) if isinstance(args.get("line_end"), int) else None),
            grep_pattern=(str(args["grep_pattern"]) if isinstance(args.get("grep_pattern"), str) else None),
        )
    if name == "sandbox_list":
        return sandbox_list(root, limit=int(args.get("limit", 20) or 20))
    if name == "record_decision":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("record_decision requires non-empty text")
        return append_decision(
            root,
            text=text,
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
            kind=args.get("kind") if isinstance(args.get("kind"), str) else None,
            observed_at=args.get("observed_at") if isinstance(args.get("observed_at"), str) else None,
            observed_versions=args.get("observed_versions") if isinstance(args.get("observed_versions"), dict) else None,
            environment=args.get("environment") if isinstance(args.get("environment"), str) else None,
            retest_after=args.get("retest_after") if isinstance(args.get("retest_after"), str) else None,
            status=args.get("status") if isinstance(args.get("status"), str) else None,
            supersedes_id=args.get("supersedes_id") if isinstance(args.get("supersedes_id"), str) else None,
        )
    if name == "ast_grep_search":
        pattern = args.get("pattern")
        lang = args.get("lang")
        if not isinstance(pattern, str) or not isinstance(lang, str):
            raise ValueError("ast_grep_search requires pattern and lang")
        from .astgrep_integration import ast_grep_search

        return ast_grep_search(
            root, pattern=pattern, lang=lang,
            path=args.get("path") if isinstance(args.get("path"), str) else None,
            max_results=int(args.get("max_results", 40) or 40),
        )
    if name == "lessons_recall":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("lessons_recall requires non-empty query")
        from .lessons import recall_lessons

        return recall_lessons(root, query=query, limit=int(args.get("limit", 5) or 5))
    if name == "record_todo":
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("record_todo requires non-empty title")
        return append_todo(
            root,
            title=title,
            owner=str(args.get("owner", "")),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            source=str(args.get("source", "agent")),
        )
    if name == "close_todo":
        match = args.get("match")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("close_todo requires match string")
        return close_todo(
            root,
            match=match,
            status=str(args.get("status", "done")),
            reason=str(args.get("reason", "")),
        )
    if name == "append_session_note":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("append_session_note requires non-empty text")
        return append_session_note(root, text=text)
    if name == "evidence_list":
        from .evidence import list_evidence
        status = args.get("status")
        return list_evidence(
            root,
            status=status if isinstance(status, str) and status else None,
            limit=int(args.get("limit", 20) or 20),
        )
    if name == "evidence_record":
        from .evidence import record_evidence
        reject_ci_write("evidence")
        return record_evidence(
            root,
            query=str(args.get("query", "")),
            path=str(args.get("path", "")),
            status=str(args.get("status", "candidate") or "candidate"),
            snippet=str(args.get("snippet", "")),
            source=str(args.get("source", "agent") or "agent"),
            note=str(args.get("note", "")),
        )
    if name == "evidence_set_status":
        from .evidence import set_evidence_status
        reject_ci_write("evidence")
        eid = args.get("id")
        status = args.get("status")
        if not isinstance(eid, str) or not eid.strip():
            raise ValueError("evidence_set_status requires id string")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("evidence_set_status requires status string")
        return set_evidence_status(
            root,
            evidence_id_value=eid,
            status=status,
            note=str(args.get("note", "")),
            source=str(args.get("source", "agent")),
        )
    if name == "security_finding_list":
        from .security_findings import list_records
        status = args.get("status")
        return list_records(
            root,
            status=status if isinstance(status, str) and status else None,
            limit=int(args.get("limit", 50) or 50),
        )
    if name == "security_finding_record":
        from .security_findings import record
        reject_ci_write("security_finding")
        return record(
            root,
            affected_path=str(args.get("affected_path", "")),
            finding_type=str(args.get("finding_type", "")),
            detail_summary=str(args.get("detail_summary", "")),
            evidence_hash=str(args.get("evidence_hash", "")),
            repro_command=str(args.get("repro_command", "")),
            verification_command=str(args.get("verification_command", "")),
            status=str(args.get("status", "open") or "open"),
            source=str(args.get("source", "agent") or "agent"),
        )
    if name == "security_finding_update":
        from .security_findings import update
        reject_ci_write("security_finding")
        fid = args.get("id")
        status = args.get("status")
        verification_command = args.get("verification_command")
        if not isinstance(fid, str) or not fid.strip():
            raise ValueError("security_finding_update requires id string")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("security_finding_update requires status string")
        if not isinstance(verification_command, str) or not verification_command.strip():
            raise ValueError("security_finding_update requires verification_command string")
        return update(
            root,
            finding_id=fid,
            status=status,
            verification_command=verification_command,
            source=str(args.get("source", "agent") or "agent"),
        )
    if name == "append_handoff":
        from .session_resume import write_handoff

        def _as_list(v: Any) -> list[str] | None:
            return [str(x) for x in v] if isinstance(v, list) else None

        return write_handoff(
            root,
            goal=(args.get("goal") if isinstance(args.get("goal"), str) else None),
            next_step=(args.get("next_step") if isinstance(args.get("next_step"), str) else None),
            plan=_as_list(args.get("plan")),
            open_questions=_as_list(args.get("open_questions")),
            blockers=_as_list(args.get("blockers")),
            agent=str(args.get("agent") or "agent"),
            clear=bool(args.get("clear")),
        )
    if name == "recommend_skills":
        from .recommend import recommend as rec_run
        return rec_run(
            root,
            limit=int(args.get("limit", 5) or 5),
            include_global=bool(args.get("include_global", True)),
            min_signal=int(args.get("min_signal", 3) or 3),
        )
    if name == "recommend_skills_accept":
        from .recommend import accept as rec_accept_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("recommend_skills_accept requires id string")
        return rec_accept_fn(root, cid)
    if name == "recommend_skills_reject":
        from .recommend import reject as rec_reject_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("recommend_skills_reject requires id string")
        return rec_reject_fn(root, cid)
    if name == "skills_list":
        from .recommend import list_visible
        return {"ok": True, "skills": list_visible(root)}
    if name == "skills_uninstall":
        from .recommend import uninstall as skills_uninstall_fn
        slug = args.get("slug")
        if not isinstance(slug, str) or not slug:
            raise ValueError("skills_uninstall requires slug string")
        return skills_uninstall_fn(root, slug, force=bool(args.get("force", False)))
    if name == "precall_recommend":
        from .precall_recommend import recommend as pc_run
        return pc_run(
            root,
            limit=int(args.get("limit", 5) or 5),
            min_signal=int(args.get("min_signal", 5) or 5),
            include_transcripts=bool(args.get("include_transcripts", False)),
        )
    if name == "precall_list":
        from .precall_recommend import list_visible
        return {"ok": True, "rules": list_visible(root)}
    if name == "precall_accept":
        from .precall_recommend import accept as pc_accept_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("precall_accept requires id string")
        return pc_accept_fn(root, cid)
    if name == "precall_activate":
        from .precall_recommend import activate as pc_activate_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("precall_activate requires id string")
        return pc_activate_fn(root, cid, force=bool(args.get("force", False)))
    if name == "precall_reject":
        from .precall_recommend import reject as pc_reject_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("precall_reject requires id string")
        return pc_reject_fn(root, cid)
    if name == "precall_disable":
        from .precall_recommend import disable as pc_disable_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("precall_disable requires id string")
        return pc_disable_fn(root, cid)
    if name == "federated_summary":
        from .federated import cross_project_summary
        return cross_project_summary(root)
    if name == "agents_recommend":
        from .agent_recommend import recommend as ag_run
        return ag_run(root, limit=int(args.get("limit", 5) or 5), min_signal=int(args.get("min_signal", 3) or 3))
    if name == "agents_list":
        from .agent_recommend import list_visible
        return {"ok": True, "agents": list_visible(root)}
    if name == "agents_accept":
        from .agent_recommend import accept as ag_accept_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("agents_accept requires id string")
        return ag_accept_fn(root, cid)
    if name == "agents_reject":
        from .agent_recommend import reject as ag_reject_fn
        cid = args.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError("agents_reject requires id string")
        return ag_reject_fn(root, cid)
    if name == "agents_uninstall":
        from .agent_recommend import uninstall as ag_uninstall_fn
        slug = args.get("slug")
        if not isinstance(slug, str) or not slug:
            raise ValueError("agents_uninstall requires slug string")
        return ag_uninstall_fn(root, slug, force=bool(args.get("force", False)))
    # remote_memory_* dispatchers removed (T37)
    # ---- Innovation modules (PoC dispatch) ----
    if name == "lsp_available":
        from .lsp import lsp_available
        return lsp_available(root)
    if name == "lsp_find_references":
        from .lsp import find_references
        return find_references(
            root,
            str(args.get("file_path", "")),
            int(args.get("line", 0) or 0),
            int(args.get("column", 0) or 0),
        )
    if name == "lsp_goto_definition":
        from .lsp import goto_definition
        return goto_definition(
            root,
            str(args.get("file_path", "")),
            int(args.get("line", 0) or 0),
            int(args.get("column", 0) or 0),
        )
    if name == "lsp_workspace_symbols":
        from .lsp import workspace_symbols
        return workspace_symbols(
            root,
            str(args.get("query", "")),
            limit=int(args.get("limit", 20) or 20),
        )
    if name == "speculative_mine_patterns":
        from .speculative import mine_patterns
        return mine_patterns(
            root,
            min_support=int(args.get("min_support", 3) or 3),
            min_confidence=float(args.get("min_confidence", 0.5) or 0.5),
            limit=int(args.get("limit", 100) or 100),
        )
    if name == "speculative_hit_rate":
        from .speculative import hit_rate
        return hit_rate(root)
    if name == "trajectory_summarize":
        from .trajectory import summarize
        return summarize(root, limit=int(args.get("limit", 10) or 10))
    raise KeyError(name)


def _parse_prompt_md(text: str) -> tuple[str, str | None, str]:
    """Parse `.claude/commands/*.md` frontmatter -> (description, argument_hint, body)."""
    lines = text.splitlines()
    desc = ""
    arg_hint: str | None = None
    in_fm = False
    body_start = 0
    seen_fm_open = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            if not seen_fm_open:
                seen_fm_open = True
                in_fm = True
                continue
            body_start = i + 1
            in_fm = False
            break
        if in_fm:
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("\"").strip("'")
            elif line.startswith("argument-hint:"):
                arg_hint = line.split(":", 1)[1].strip().strip("\"").strip("'")
    body = "\n".join(lines[body_start:]).strip() if seen_fm_open else text.strip()
    return desc, arg_hint, body


def _list_prompts(root: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    cmd_dir = root / ".claude" / "commands"
    if not cmd_dir.exists():
        return prompts
    for md in sorted(cmd_dir.glob("cb-*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        desc, arg_hint, _ = _parse_prompt_md(text)
        entry: dict[str, Any] = {"name": md.stem, "description": desc or md.stem}
        if arg_hint:
            entry["arguments"] = [{"name": "input", "description": arg_hint, "required": False}]
        prompts.append(entry)
    return prompts


def _get_prompt(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    md = root / ".claude" / "commands" / f"{name}.md"
    if not md.is_file():
        raise KeyError(name)
    text = md.read_text(encoding="utf-8")
    desc, _, body = _parse_prompt_md(text)
    args_value = ""
    if isinstance(arguments, dict):
        for key in ("input", "ARGUMENTS", "args"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                args_value = value
                break
    body = body.replace("$ARGUMENTS", args_value)
    return {
        "description": desc,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": body}},
        ],
    }


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(root: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    """Route a single JSON-RPC message. Returns None for notifications (no response).

    Supports both standard MCP protocol (initialize, tools/list, tools/call, etc.)
    and direct tool-name dispatch (legacy/internal callers like ai-mcp --once-json).
    """
    start = time.perf_counter()
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    is_notification = "id" not in request
    audit_tool_name: str | None = None

    try:
        # Notifications — no response per JSON-RPC 2.0.
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": MCP_SERVER_NAME, "version": __version__},
            }
            response = _ok(request_id, result)
        elif method == "ping":
            response = _ok(request_id, {})
        elif method == "tools/list":
            response = _ok(request_id, _get_tools_list_payload())
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            audit_tool_name = name if isinstance(name, str) else None
            if not isinstance(name, str) or name not in TOOL_NAMES:
                response = _err(request_id, -32602, f"unknown tool: {name!r}")
            else:
                try:
                    tool_result = _dispatch_tool(root, name, arguments or {})
                    response = _ok(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(tool_result, ensure_ascii=False, sort_keys=True),
                                }
                            ],
                            "isError": not bool(tool_result.get("ok", True)) if isinstance(tool_result, dict) else False,
                            "structuredContent": tool_result if isinstance(tool_result, dict) else None,
                        },
                    )
                except Exception as exc:
                    response = _ok(
                        request_id,
                        {
                            "content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True,
                        },
                    )
        elif method == "prompts/list":
            response = _ok(request_id, {"prompts": []})
        elif method == "prompts/get":
            response = _err(request_id, -32601, "prompts disabled — use local .claude/commands or .codex/prompts directly")
        elif method == "resources/list":
            response = _ok(request_id, {"resources": []})
        elif method == "resources/templates/list":
            response = _ok(request_id, {"resourceTemplates": []})
        elif isinstance(method, str) and method in TOOL_NAMES:
            # Legacy direct dispatch: e.g. {"method": "obs_usage", ...}
            audit_tool_name = method
            result = _dispatch_tool(root, method, params if isinstance(params, dict) else {})
            response = _ok(request_id, result)
        else:
            response = _err(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        response = _err(request_id, -32000, str(exc))

    record_mcp_request(
        root,
        method,
        request,
        response,
        start,
        response.get("result") if isinstance(response, dict) else None,
        tool_name=audit_tool_name,
    )
    return None if is_notification else redact_value(response)


def record_mcp_request(
    root: Path,
    method: Any,
    request: dict[str, Any],
    response: dict[str, Any],
    start: float,
    result: Any,
    *,
    tool_name: str | None = None,
) -> None:
    if is_ci():
        return
    try:
        event = {
            "hook": "mcp.request",
            "method": method,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "request_bytes": len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "response_bytes": len(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "results_count": len(result.get("results", [])) if isinstance(result, dict) else None,
        }
        if tool_name:
            event["tool_name"] = tool_name
        append_event(root, event)
    except Exception:
        # mcp.request audit is best-effort; never fail the JSON-RPC response on it.
        pass


def serve_stdio(root: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps(_err(None, -32700, f"parse error: {exc}"), ensure_ascii=False, sort_keys=True), flush=True)
            continue
        response = handle_request(root, request)
        if response is None:
            continue
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0
