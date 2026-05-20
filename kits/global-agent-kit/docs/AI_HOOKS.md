# AI_HOOKS.md

Hook-first plan for Claude Code and Codex-adjacent workflows.

## 원칙

- Hooks protect the session before prompts, skills, MCP, or agents optimize it.
- Default hooks may block secret reads and destructive shell actions, but must not block normal `Read`, `Edit`, `Write`, or shell diagnostics.
- `SessionStart` context stays short: installed kit, available commands, and approval boundaries only.
- Project-local rules may add stricter hooks, but cannot weaken secret, credential, destructive, deployment, or billing approval boundaries.

## 현재 기본값

| Hook | Event | 목적 | 실패 기준 |
| --- | --- | --- | --- |
| `block-dangerous.sh` | `PreToolUse:Bash` | destructive commands require explicit user approval | broad Bash denial or bypass guidance |
| `protect-secrets.sh` | `PreToolUse:Read/Edit/Write` | real secrets and credential files are not read or changed | `.env`, keys, tokens, certs exposed |
| `session-context.sh` | `SessionStart` | concise kit usage context injection | long context, stale commands, hidden policy changes |

## 채택 기준

- `Code Brain Skill Router`: adopt only as small commands, prompts, or checklists when they improve repeated task quality without adding background mutation.
- `Code Brain Guardrails`: adopt when tests or validation are local, deterministic, and fail closed without changing packages or production state.
- `Code Brain Snapshot`: adopt backup-before-overwrite, retention, and restore dry-run ideas; reject remote sync, credentials, or opaque restore behavior by default.
- `Code Brain Context Score`: adopt lightweight scoring for context relevance and freshness; reject token-heavy always-on context dumps.
- `Code Brain Delivery Loop`: adopt staged plan/review/QA/ship commands only as explicit invocation, never as always-on context.
- Codex Skills / MCP prompts: adopt as documented prompts or optional project-local integrations; reject automatic global MCP registration that needs OAuth, tokens, or broad filesystem/network access.

## 검증 기대치

- `make validate` must check required docs, hook files, command files, shell syntax, JSON syntax, and smoke install wiring.
- New hook or loop scripts must be listed in `scripts/validate.sh` with `bash -n`.
- `make doctor` must confirm installed global hook commands are wired to their installed absolute paths.
- `./scripts/dev-loop.sh --once` must prove adopted candidates remain documented before running validation.
