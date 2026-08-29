<p align="center"><img src="docs/assets/social-preview.png" alt="Code Brain — repo-local memory, code search, MCP and hooks for AI coding agents" width="820"></p>

<h1 align="center">Code Brain</h1>

<p align="center"><b>Repo-local infrastructure that gives AI coding agents memory, search, guardrails, and an upgrade path.</b></p>

<p align="center">
<a href="https://github.com/ezBuilder/code-brain/releases"><img src="https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF" alt="Release"></a>
<a href="https://github.com/ezBuilder/code-brain/blob/main/LICENSE"><img src="https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50" alt="License"></a>
<a href="https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml"><img src="https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate" alt="Release Gate"></a>
<a href="https://github.com/ezBuilder/code-brain/stargazers"><img src="https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107" alt="Stars"></a>
</p>

<p align="center">
<img src="https://img.shields.io/badge/Claude_Code-21_hooks-8A2BE2?style=flat-square" alt="Claude Code">
<img src="https://img.shields.io/badge/Codex_CLI-12_hooks-111111?style=flat-square" alt="Codex CLI">
<img src="https://img.shields.io/badge/Antigravity-3_hooks-4285F4?style=flat-square" alt="Antigravity">
<img src="https://img.shields.io/badge/Kiro_IDE%2FCLI_v3-5_hooks-7B61FF?style=flat-square" alt="Kiro">
<img src="https://img.shields.io/badge/MCP-62_methods-FF6F00?style=flat-square" alt="MCP methods">
<img src="https://img.shields.io/badge/strict_doctor-37_checks-00897B?style=flat-square" alt="Doctor checks">
</p>

<p align="center">
<a href="docs/readme/ko.md">한국어</a> · English · <a href="docs/readme/zh-CN.md">中文</a> · <a href="docs/readme/ja.md">日本語</a> · <a href="docs/readme/es.md">Español</a> · <a href="docs/readme/fr.md">Français</a> · <a href="docs/readme/de.md">Deutsch</a>
</p>

---

Agents are powerful and forgetful. They re-read the same files, dump 40k-token greps into context, patch stale line numbers, lose every decision at session end, and stop mid-task claiming success. Code Brain installs into a repository and fixes those failures at the tool boundary — the same `.ai/` runtime, memory, index, hook policy, and audit trail for **Claude Code, Codex CLI, Google Antigravity, and Kiro**.

One install. One brain. Every agent in the repo shares it.

## Highlights

| | |
|---|---|
| **One brain, four hosts** | Claude, Codex, Antigravity, and Kiro share one `.ai/` contract, memory, search index, and guarded hook runtime. MCP exposure stays host-specific. |
| **Search before sprawl** | Agents locate code with BM25/FTS5 and bounded context packs instead of dumping files into the prompt. |
| **Hashline-safe edits** | `code_read_hashline` returns line+sha anchors, so patches land where the agent thinks they will. |
| **Guardrails on the hot path** | Hooks block destructive git, broad grep/find dumps, secret leaks, and runaway output before they cost tokens or leak data. |
| **Agents that finish** | The completion guard refuses `Stop` while request-scoped machine evidence of unfinished work remains — and yields when the evidence is stale. |
| **Lossless memory, bounded indexes** | Raw audit history is immutable truth in sealed 64MB segments; every rollup, episodic tier, and log is capped and doctor-checked. |
| **Logarithmic recall** | A deterministic fanout-10 episodic pyramid keeps resident context `O(log N)` while `drill-down` always reaches the raw rows. |
| **Token-aware by default** | MCP starts in the lean `usage` profile; `tool_search` reveals the rest on demand. |
| **Public upgrade path** | Installed projects run `/cb-upgrade` or `.ai/bin/ai upgrade latest --json` to pull from GitHub and re-bootstrap. |
| **Offline by contract** | Hook and MCP hot paths never call the network. Model downloads and memory sync are explicit commands. |

## Quick Start

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

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

Then open a **new** agent session so hooks, MCP config, and `AGENTS.md` load.

In an interactive shell the macOS/Linux installer also offers the Claude/Codex global kit. Existing `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` are backed up and preserved — Code Brain only adds or refreshes its own managed block. CI and non-interactive installs skip global writes unless you pass `--global`; `--no-global` opts out explicitly.

No local clone? Bootstrap straight from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

## Upgrade

From inside an installed project:

```bash
.ai/bin/ai upgrade latest --json     # pull the public repo and re-bootstrap
.ai/bin/ai upgrade latest --dry-run --json
```

From inside an agent session, run `/cb-upgrade`, then start a new session.

Pin a ref:

```bash
.ai/bin/ai upgrade latest --ref v0.9.1 --json
CODE_BRAIN_REF=v0.9.1 bash scripts/upgrade-from-github.sh /path/to/project
```

Upgrades are always explicit: `SessionStart` hooks and MCP hot paths do not call the network.

## Agent Workflow

Start narrow, anchor the edit, verify the result:

```bash
.ai/bin/ai code query "auth flow" --json                       # 1. find it
.ai/bin/ai context pack "auth flow" --json                     # 2. bounded context + structure
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80    # 3. line+sha anchors before editing
.ai/bin/ai doctor --strict --json                              # 4. prove the repo is still healthy
.ai/bin/ai obs usage --json                                    # actual host token usage + CB overhead
```

Durable memory across sessions:

```bash
.ai/bin/ai memory recall --query "auth flow" --json
.ai/bin/ai memory decision add --text "use X" --contradicts dec-1234 --expires-at 2026-12-31
.ai/bin/ai memory decision list --kind failure --json
.ai/bin/ai memory conflicts --json
.ai/bin/ai memory forget --id dec-1234 --confirm-id dec-1234 --yes
.ai/bin/ai plan init --id feat --step "do A" --step "do B"
```

`memory recall` spans decisions, failures, lessons, and procedures in one ranked, cited answer; `memory conflicts` flags contradicting decisions offline. A durable plan (`ai plan`) keeps multi-step work honest — with `AI_LOOP_CONTINUATION` the Stop hook re-prompts until every step is checked. `ai memory forget` hard-deletes a record (tombstone + compaction + deletion receipt) so no read path, `SessionStart` injection included, can resurface it.

`code_find_references` / `code_goto_definition` add LSP-grade navigation when a language server is installed.

### Retrieval Coverage

Symbol and call-graph extraction covers **Python, TypeScript, JavaScript, Rust, Go, Dart, and Kotlin** through ast-grep, with the same treatment for oversized sources. Files above the 100KB chunk cap are streamed into bounded overlapping windows and capped symbol spans instead of being silently skipped; search returns one result per real file, and index diagnostics classify every omission (generated, binary, lock, encoding, unsupported, trust, hard-size) with a reason.

Structural context is on by default: exact function spans, typed graph provenance, deterministic one-hop personalized PageRank, and fail-soft suppression of stale edges.

## Memory Model

Raw trajectory is the source of truth; summaries are only an index.

```text
raw audit  ──► sealed 64MB digest-named segments (immutable, hash-chained)
   │
   ├─ tier 0   1 event      verbatim
   ├─ tier 1   10 events    deterministic summary  ─┐
   ├─ tier 2   100 events   deterministic summary   ├─ fanout 10, O(log N) resident context
   └─ tier 3+  1,000 …      deterministic summary  ─┘
   │
   └─ every summary keeps raw step IDs ──► ai memory episodic drill-down
```

```bash
.ai/bin/ai memory episodic status --json
.ai/bin/ai memory episodic context --byte-budget 8000 --raw-tail 20 --json
.ai/bin/ai memory episodic drill-down --start 100 --end 120 --json
.ai/bin/ai memory episodic build --json
```

Older memory is coarse, recent memory is detailed, and total index size grows logarithmically with session lifetime. Every tier fails closed on malformed rows, hash-chain changes, or any summary that cannot be reproduced from raw truth. `SessionStart` reads only a prebuilt `cb-life:` cache capped at 200 bytes — important judgments drill down to raw rows instead. See [Episodic Memory and Lossless Audit History](docs/EPISODIC_MEMORY.md).

Raw memory is private by default. Install and upgrade never propagate project memory, and cross-machine sync requires explicit `.ai/bin/ai memory sync --private-remote-confirmed`, which stages every segment atomically, disables union merge, and refuses sequence gaps. Sync never runs from a hook.

## Guardrails

**Completion guard** (default on) refuses `Stop`/`SubagentStop` only when request-scoped machine evidence remains: a changed active plan, a new conflict/syntax/unfinished marker, a mutation without a later successful relevant check, or a new failed acceptance run. Verification is bound to host tool-call identity, exit status, edit order, and the current edit-target content hash — stale, corrupt, or partial evidence yields. It never treats a dirty tree or old backlog as current work, never overrides security/user-input/context-pressure stops, and yields after repeated identical evidence or the shared eight-continuation / 30-minute cap. `AI_COMPLETION_GUARD=0` is an emergency kill switch only.

**Hook policy** blocks destructive git, broad `grep -r` / `find` / `tree` dumps, secret-bearing commits, and oversized tool output on `PreToolUse`, routing agents to `sandbox_execute` / `ai exec run` instead.

**MCP argument contract** validates every call before a handler runs. Required strings publish `minLength: 1` in `tools/list`, rejections name the offending schema field (never caller text), and after 3 consecutive identical rejections the error escalates to an explicit stop order — so a client without loop detection cannot spin.

**Host-aware context delivery** prevents duplicate injection: Claude receives static rules and durable memory through `SessionStart`; Antigravity reads the managed root `AGENTS.md`; Codex uses that file when its bounded fingerprint is current and otherwise receives only live deltas plus opted-in recommendations.

## Host Support

| Host | Hooks | MCP config | Commands |
|---|---|---|---|
| Claude Code | 21 | `.mcp.json` | `.claude/commands/cb-*` |
| Codex CLI | 12 | `.codex/config.toml` (`usage` profile) | `.codex/prompts/cb-*` |
| Google Antigravity | 3 of 5 (`PreToolUse`, `PostInvocation` unsupported) | `.agents/mcp_config.json` | `.agents/skills/` |
| Kiro IDE / CLI v3 | 5 | — | `.agents/skills/` |

Kiro's standalone `.kiro/hooks/code-brain.json` is active in the IDE and CLI v3; CLI v2 keeps it as an inert, forward-compatible seed. New Claude/Codex events are version-gated, bounded per-event timeouts and Codex context-spill thresholds are set automatically, and foreign hooks survive repeated upgrades and downgrades.

Slash / source commands available in every host:

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
/cb-proof    legacy/v2 retrieval A/B and durability proof
```

## MCP Surface

62 registered methods, exposed progressively. Default profile:

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:     all tools except the hidden worker-pool surface (loopd_*, loop_submit)
full-all: everything, worker pool included
```

The five `usage` tools:

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

Optional pilots — 4 of 6 are on by default (`AI_MCP_RESOURCES`, `AI_DIR_CONTEXT`, `AI_MEMORY_CONFLICT_SCAN`, `AI_LOOP_CONTINUATION`); `AI_AST_CHUNK` and `AI_SELF_IMPROVE_AUTO` are off. Manage them with `ai config pilots`. cAST self-validates through `ai cast eval`, a recall ratchet that enables AST-aware chunking only when it beats the default chunker on your repo — nothing changes unmeasured.

## Proof Points

Do not trust synthetic benchmark claims. Run these in your own repo:

```bash
make lint
make test        # 2,622 tests as parallel shards (~3m); make test-serial for one process
make eval
make doctor
scripts/lockfile-check.sh
uv lock --check --project .ai/runtime
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai index rebuild --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
.ai/bin/ai context prove --json
```

What they establish:

- **37 strict doctor checks** cover config, index freshness, manifest, audit chain, secret scan, hot-path SLO, bounded generated artifacts, storage limits, hook capabilities, and command registration.
- **7 gated eval axes** — `precall_routing`, `context_budget`, `tool_discovery`, `autoresearch_retrieval`, `code_retrieval`, `line_span_retrieval`, `memory_retrieval` — fail the build on recall regressions. Expired, refuted, and tombstoned memory records must never rank.
- **`ai context prove` / `/cb-proof`** measures legacy-vs-v2 retrieval A/B, graph activation, determinism, bounded context, latency, and post-warmup no-growth of retrieval files.
- **`obs usage`** reads actual Claude/Codex logs. Code Brain never prints estimated token savings; `no_token_estimates` is a doctor check.
- Install and upgrade assets exist and validate for all four hosts, and public-repo upgrade planning works without touching files in dry-run mode.

Add benchmark numbers only when a repeatable script in `scripts/` or CI generates them.

## What Gets Installed

```text
.ai/                         runtime, memory structure, hooks, MCP shim
.mcp.json                    Claude Code MCP
.claude/settings.json        Claude Code hooks
.claude/commands/            slash commands
.codex/config.toml           Codex MCP profile usage
.codex/hooks.json            Codex hooks
.codex/prompts/              Codex prompts
.agents/mcp_config.json      Antigravity MCP
.agents/hooks.json           Antigravity hooks
.agents/skills/              source-command skills
.kiro/hooks/code-brain.json  Kiro IDE/CLI v3 hooks (CLI v2 seed)
.githooks/post-merge         index refresh
.githooks/post-checkout      index refresh
AGENTS.md                    canonical seed + managed durable-memory block
CLAUDE.md                    seed-only mirror of .ai/AGENTS.md
```

Manual lifecycle:

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 install C:\path\to\project
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 upgrade C:\path\to\project
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 uninstall C:\path\to\project
```

Use `upgrade` for a pre-manifest partial Code Brain runtime; adoption requires three independent managed markers to agree, and unrelated `.ai` directories are never overwritten. Both entry points share the same manifest ownership, symlink confinement, byte-no-op, and non-destructive uninstall contract. A persistent write-ahead journal restores managed/config files, the prior `core.hooksPath`, and the known-good venv after command failure, interrupt, SIGKILL, or power-loss retry — backup hashes are verified before restoration and `COMMITTED` is durable before cleanup. Git for Windows supplies the Bash runtime behind the PowerShell entry point; generated MCP and hook commands stay native PowerShell.

Install and upgrade trust the exact Code Brain-managed Codex project hooks after the target transaction commits, so a fresh install is immediately usable. Foreign/custom hooks and global user hooks are never auto-trusted. `AI_CODEX_HOOK_AUTO_TRUST=0` keeps hooks review-gated; CI defaults to the opt-out unless `AI_CODEX_HOOK_AUTO_TRUST=1` is explicit. See [Operations](OPERATIONS.md#codex-hook-trust-after-upgrades).

Antigravity global MCP is opt-in only:

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

## Token And Disk Defaults

```text
AI_CODE_BRAIN_PROFILE=usage      lean MCP surface
AI_MCP_COMPACT_TOOLS=1           compact tool schemas
AI_PROMPT_GROWTH=0               prompt-growth telemetry off
AI_MEMORY_TIER_SUMMARY=0         tier telemetry off
AI_CODEGRAPH_SUMMARY=0           hotspot telemetry off
AI_MEMORY_PAGE_IN=0              HOT cache pre-warm off
AI_AUTO_PAGE_OUT=0               inline page-out off
```

Everything expensive is opt-in. Detached sleep-time page-out stays on to seal lossless audit segments, refresh non-destructive rollups, and build the episodic pyramid without charging the Stop hot path.

Generated artifact caps:

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/memory/audit/YYYY.jsonl          seals losslessly near 64MB; raw retained
.ai/memory/audit-rollups/            64MB cap, 16 top-level entries
.ai/memory/episodic/                 128MB cap, 8 top-level entries
.ai/memory/episodic-tombstones/      authoritative forget markers; never reclaimed
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
.ai/tmp/                             512MB cap, 7-day retention, 256 entries
.ai/outputs/                         1GB cap, 512 top-level entries
.ai/                                 2GB reclaimable-data cap; authoritative memory excluded
```

Session start and upgrades prune the oldest untracked reclaimable entries when a limit is exceeded. A tracked top-level entry, a directory containing `.keep`, or an item with a sibling `<name>.keep` is preserved and excluded from the enforceable cap — pinned bytes never make a cap impossible to satisfy. Authoritative audit/decision/session memory and forget tombstones are reported separately and never auto-deleted; derived episodic and rollup sidecars may be reclaimed because they rebuild from raw truth.

Manual cleanup:

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai memory episodic build --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

`doctor --strict` fails `generated_artifacts_bounded` or `storage_limits` if anything grows past its limit.

## Security

- Never read, print, edit, or commit real secrets. `.env`, keys, tokens, certs, password stores, runtime state, and private memory stay out of the public source repo.
- Installers do not copy source `.ai/memory/*` or `.ai/runtime/state/*` into target projects.
- Hook and MCP hot paths are local and never call the network.
- `AI_INSTALL_GLOBAL_ANTIGRAVITY=1` is required before any global Antigravity file changes.
- Audit-chain verification rejects duplicate IDs/sequences, digest/link/byte-count tampering, and nondeterministic sync branches. Repair renames changed segments to their canonical digest and cascades downstream links without dropping content.
- Generic secret detection keeps high-signal literals while rejecting ordinary identifier expressions and repeated-character test placeholders; acknowledged exceptions live in `secret_scan_allowlist.txt`.
- MCP, diagnostics, and external-channel output is redacted, and `redaction_self_test` is a doctor check.
- CI is read-only: write commands are rejected with exit code `16` before any worker is contacted.
- Release candidates pass `make lint`, targeted tests, `make doctor`, `scripts/lockfile-check.sh`, and `make release-gate`.

## Architecture

```text
.ai/
├── bin/                         ai / ai-hook / ai-mcp (+ PowerShell shims)
├── runtime/src/ai_core/
│   ├── search.py                BM25 FTS5 + chunking
│   ├── astgrep_integration.py   multi-language symbol and graph extraction
│   ├── codebase_map.py          personalized PageRank repo map
│   ├── hashline.py              line+sha edit anchors
│   ├── hooks.py                 Claude/Codex/Antigravity/Kiro hook handling
│   ├── completion_guard.py      evidence-bound stop refusal
│   ├── mcp_server.py            MCP JSON-RPC stdio server
│   ├── mcp_config.py            per-host config dialects
│   ├── memory.py                decisions/todos + lossless audit segmentation
│   ├── memory_tier.py           page-out / page-in / tiering
│   ├── memory_hot.py            salience-ranked HOT memory cache
│   ├── episodic_memory.py       deterministic logarithmic pyramid core
│   ├── episodic_runtime.py      audit integration, receipts, raw drill-down
│   ├── evidence.py              bounded evidence ledger
│   ├── doctor.py                37 release and safety checks
│   ├── obs.py                   usage/health/search diagnostics
│   ├── sandbox.py               bounded command output capture
│   └── security_findings.py     redacted security finding ledger
├── memory/                      per-project durable memory
├── cache/                       sqlite / sandbox / generated cache
├── generated/                   render and install manifests
└── AGENTS.md                    canonical local agent contract
```

Full CLI surface: `ai {version,config,cast,render,doctor,worker,queue,loop,plan,trust,secrets,inbox,notify,prompt-growth,selfimprove,loopd,obs,diagnostics,migrate,upgrade,hook,memory,lessons,audit,exec,index,recommend,skills,precall,federated,eval,embedding,reranker,agents,code,guard,context,evidence,security,session,mcp,release-gate,kit,runtime,report}`

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component map and design contracts |
| [OPERATIONS.md](OPERATIONS.md) | Operator runbook, diagnostics, CI policy, hook trust |
| [docs/EPISODIC_MEMORY.md](docs/EPISODIC_MEMORY.md) | Episodic pyramid and lossless audit history |
| [SECURITY.md](SECURITY.md) | Reporting and security boundaries |
| [RELEASE.md](RELEASE.md) | Release checklist and expected state |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow |

## License

Apache-2.0.
