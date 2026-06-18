<p align="center"><img src="docs/assets/social-preview.png" alt="Code Brain — repo-local memory, code search, MCP and hooks for AI coding agents" width="820"></p>

# Code Brain

[![Release](https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF)](https://github.com/ezBuilder/code-brain/releases)
[![License](https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50)](https://github.com/ezBuilder/code-brain/blob/main/LICENSE)
[![Release Gate](https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate)](https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml)
[![Stars](https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107)](https://github.com/ezBuilder/code-brain/stargazers)

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-8A2BE2?style=flat-square)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-ready-111111?style=flat-square)
![Antigravity](https://img.shields.io/badge/Antigravity-ready-4285F4?style=flat-square)

[한국어](docs/readme/ko.md) · English · [中文](docs/readme/zh-CN.md) · [日本語](docs/readme/ja.md) · [Español](docs/readme/es.md) · [Français](docs/readme/fr.md) · [Deutsch](docs/readme/de.md)

Code Brain is repo-local infrastructure for serious AI coding agents. It gives Claude Code, Codex CLI, and Google Antigravity the same project memory, BM25 code search, hook policy, MCP tools, audit trail, and upgrade path inside one workspace.

It is built for one uncomfortable truth: agents are powerful, but they forget context, over-read code, dump huge output, and drift across tools. Code Brain turns a repository into an agent-ready operating layer.

## Why It Stands Out

- **One brain for multiple agents.** Claude, Codex, and Antigravity share the same `.ai/` contract, memory, search index, hooks, and command surface.
- **Token-aware by default.** MCP starts in the lean `usage` profile: only `obs_usage`, `code_query`, `context_pack`, `code_read_hashline`, and `tool_search` are exposed up front.
- **Search before sprawl.** Agents locate code with BM25/FTS5 and compact context packs instead of blindly dumping files into the prompt.
- **Hashline-safe edits.** `code_read_hashline` gives line+sha anchors before edits, reducing stale or misplaced patches.
- **Guardrails on the hot path.** Hooks block destructive git, broad grep/find dumps, secret leaks, and long output before they waste tokens or expose data.
- **Bounded memory and artifacts.** Runtime-generated JSONL/log/evidence files have caps and doctor checks so a repo does not balloon silently.
- **Offline memory consolidation.** Sleep-time `ai memory page-in` pre-warms a salience-ranked HOT cache so the next session loads a tighter, fewer-token context without any network call.
- **Public-repo upgrade path.** Installed projects can run `/cb-upgrade` or `.ai/bin/ai upgrade latest --json` to pull from GitHub and re-bootstrap.
- **Public-release hygiene.** Source memory/state is not propagated into target projects; secret scan, audit chain, manifest, and generated artifact checks are built in.

## Quick Install

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

In an interactive shell, the macOS/Linux installer also offers the Claude/Codex global kit by default. Existing `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` are backed up and preserved; Code Brain adds or refreshes only its managed block. CI and non-interactive installs skip global writes unless you pass `--global`; use `--no-global` to opt out explicitly.

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Success ends with:

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

Open a new Claude/Codex/Antigravity session after install.

## Upgrade From GitHub

Inside an installed project:

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

Inside an agent session, run:

```text
/cb-upgrade
```

After a successful upgrade, open a new agent session so new hooks, MCP config, `AGENTS.md`, and `CLAUDE.md` are loaded.

For first-time bootstrap without keeping a local clone:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

For non-interactive bootstrap with the global kit:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

Pin a version or branch:

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

Upgrades are explicit. `SessionStart` hooks and MCP hot paths do not call the network.

## Agent Workflow

Start narrow, then edit with anchors:

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
.ai/bin/ai memory recall --query "auth flow" --json
.ai/bin/ai memory decision list --kind failure --json
.ai/bin/ai memory conflicts --json
```

Recall spans decisions, failures, lessons, and procedures in one ranked, cited answer; `memory conflicts` flags contradicting decisions offline.

Default MCP tools:

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

Common slash/source commands:

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## Proof Points

Do not trust synthetic benchmark claims. Code Brain ships checks you can run in your own repo:

```bash
make lint
scripts/lockfile-check.sh
uv lock --check --project .ai/runtime
uv run --project .ai/runtime python -m pytest .ai/runtime/tests/test_cli.py -k "upgrade_latest or cb_upgrade_command_assets"
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai index rebuild --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

What those prove:

- install and upgrade assets exist for Claude, Codex, and Antigravity
- public-repo upgrade planning works without touching files in dry-run mode
- strict doctor verifies config, index freshness, manifest, audit chain, secret scan, hot-path SLO, bounded generated artifacts, and command registration
- usage reporting reads actual Claude/Codex logs instead of estimating token savings

For a public README, lead with these reproducible checks. Add benchmark numbers only when they are generated by a repeatable script in `scripts/` or CI.

## What Gets Installed

```text
.ai/                         runtime, memory structure, hooks, MCP shim
.mcp.json                    Claude Code MCP
.codex/config.toml           Codex MCP profile usage
.codex/hooks.json            Codex hooks
.claude/settings.json        Claude Code hooks
.claude/commands/            slash commands
.codex/prompts/              Codex prompts
.agents/mcp_config.json      Antigravity MCP
.agents/hooks.json           Antigravity hooks
.agents/skills/              source-command skills
.githooks/post-merge         index refresh
.githooks/post-checkout      index refresh
AGENTS.md                    seed-only mirror of .ai/AGENTS.md
CLAUDE.md                    seed-only mirror of .ai/AGENTS.md
```

Manual install, upgrade, uninstall:

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Antigravity global MCP is opt-in only:

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

The macOS/Linux top-level installer can install the Claude/Codex global kit. It backs up existing files and merges only the managed Code Brain block into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`; Claude settings, hooks, commands, agents, and skills are merged or copied under `~/.claude/`. Antigravity global setup only updates the `code-brain` entry when explicitly requested.

## Token And Disk Defaults

Default MCP profile:

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

Tool exposure by profile:

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

Generated artifact caps:

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

Manual cleanup:

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

`doctor --strict` fails `generated_artifacts_bounded` if the capped files grow past limits.

## Security And Public Repo Hygiene

- Do not read, print, edit, or commit real secrets.
- `.env`, keys, tokens, certs, password stores, runtime state, and private memory stay out of the public source repo.
- Installers do not copy source `.ai/memory/*` or `.ai/runtime/state/*` data into target projects.
- Hook/MCP hot paths are local and do not call the network.
- `AI_INSTALL_GLOBAL_ANTIGRAVITY=1` is required before any global Antigravity file is changed.
- CI and release candidates should pass `make lint`, targeted tests, `make doctor`, lockfile checks, and `make release-gate`.

## Architecture Map

```text
.ai/
├── bin/                         ai / ai-hook / ai-mcp (+ PowerShell shims)
├── runtime/src/ai_core/
│   ├── search.py                BM25 FTS5 + chunking
│   ├── hashline.py              line+sha edit anchors
│   ├── hooks.py                 Claude/Codex/Antigravity hook handling
│   ├── mcp_server.py            MCP JSON-RPC stdio server
│   ├── mcp_config.py            Claude/Codex/Antigravity config dialects
│   ├── memory.py                decisions/todos/audit/events rotation
│   ├── memory_tier.py           page-out / page-in / tiering
│   ├── memory_hot.py            sleep-time salience-ranked HOT memory cache
│   ├── evidence.py              bounded evidence ledger
│   ├── doctor.py                release and safety checks
│   ├── obs.py                   usage/health/search diagnostics
│   ├── sandbox.py               bounded command output capture
│   └── security_findings.py     redacted security finding ledger
├── memory/                      per-project durable memory
├── cache/                       sqlite/sandbox/generated cache
├── generated/                   render/install manifests
└── AGENTS.md                    canonical local agent contract
```

## License

Apache-2.0.
