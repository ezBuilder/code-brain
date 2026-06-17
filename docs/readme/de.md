# Code Brain

[![Release](https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF)](https://github.com/ezBuilder/code-brain/releases)
[![License](https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50)](https://github.com/ezBuilder/code-brain/blob/main/LICENSE)
[![Release Gate](https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate)](https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml)
[![Stars](https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107)](https://github.com/ezBuilder/code-brain/stargazers)

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-8A2BE2?style=flat-square)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-ready-111111?style=flat-square)
![Antigravity](https://img.shields.io/badge/Antigravity-ready-4285F4?style=flat-square)

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · Deutsch

Code Brain ist repo-lokale Infrastruktur für ernsthafte KI-Coding-Agents. Es gibt Claude Code, Codex CLI und Google Antigravity dasselbe Projektgedächtnis, dieselbe BM25-Codesuche, Hook-Policy, MCP-Tools, denselben Audit-Trail und Upgrade-Pfad innerhalb eines Workspace.

Es ist für eine unbequeme Wahrheit gebaut: Agents sind mächtig, aber sie vergessen Kontext, lesen Code übermäßig, geben riesige Ausgaben aus und driften zwischen Tools ab. Code Brain verwandelt ein Repository in eine agent-fähige Betriebsschicht.

## Warum es heraussticht

- **Ein Brain für mehrere Agents.** Claude, Codex und Antigravity teilen sich denselben `.ai/`-Vertrag, dasselbe Gedächtnis, denselben Suchindex, dieselben Hooks und dieselbe Befehlsoberfläche.
- **Standardmäßig token-bewusst.** MCP startet im schlanken `usage`-Profil: zunächst werden nur `obs_usage`, `code_query`, `context_pack`, `code_read_hashline` und `tool_search` bereitgestellt.
- **Suchen statt Wuchern.** Agents lokalisieren Code mit BM25/FTS5 und kompakten Context-Packs, statt Dateien blind in den Prompt zu kippen.
- **Hashline-sichere Edits.** `code_read_hashline` liefert Zeilen+SHA-Anker vor Edits und reduziert veraltete oder fehlplatzierte Patches.
- **Schutzplanken auf dem Hot Path.** Hooks blockieren destruktives Git, breite grep/find-Dumps, Secret-Lecks und lange Ausgaben, bevor sie Tokens verschwenden oder Daten preisgeben.
- **Begrenztes Gedächtnis und begrenzte Artefakte.** Zur Laufzeit erzeugte JSONL-/Log-/Evidence-Dateien haben Obergrenzen und Doctor-Checks, damit ein Repo nicht still aufgebläht wird.
- **Offline-Gedächtniskonsolidierung.** Das schlafzeitliche `ai memory page-in` wärmt einen salienz-gerankten HOT-Cache vor, sodass die nächste Session einen engeren, token-ärmeren Kontext lädt — ganz ohne Netzwerkaufruf.
- **Upgrade-Pfad über das öffentliche Repo.** Installierte Projekte können `/cb-upgrade` oder `.ai/bin/ai upgrade latest --json` ausführen, um von GitHub zu ziehen und neu zu bootstrappen.
- **Hygiene für öffentliche Releases.** Quell-Gedächtnis/-Zustand wird nicht in Zielprojekte propagiert; Secret-Scan, Audit-Chain, Manifest und Checks für generierte Artefakte sind eingebaut.

## Schnellinstallation

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

In einer interaktiven Shell bietet der macOS/Linux-Installer standardmäßig auch das globale Claude/Codex-Kit an. Vorhandene `~/.claude/CLAUDE.md` und `~/.codex/AGENTS.md` werden gesichert und bewahrt; Code Brain fügt nur seinen verwalteten Block hinzu oder aktualisiert ihn. CI und nicht-interaktive Installationen überspringen globale Schreibvorgänge, sofern du nicht `--global` übergibst; nutze `--no-global`, um dich explizit abzumelden.

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Erfolg endet mit:

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

Öffne nach der Installation eine neue Claude-/Codex-/Antigravity-Session.

## Upgrade von GitHub

Innerhalb eines installierten Projekts:

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

Führe innerhalb einer Agent-Session aus:

```text
/cb-upgrade
```

Öffne nach einem erfolgreichen Upgrade eine neue Agent-Session, damit neue Hooks, MCP-Konfiguration, `AGENTS.md` und `CLAUDE.md` geladen werden.

Für ein erstmaliges Bootstrap ohne lokalen Klon:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

Für ein nicht-interaktives Bootstrap mit dem globalen Kit:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

Eine Version oder einen Branch pinnen:

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

Upgrades sind explizit. `SessionStart`-Hooks und MCP-Hot-Paths rufen das Netzwerk nicht auf.

## Agent-Workflow

Eng beginnen, dann mit Ankern editieren:

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

Standard-MCP-Tools:

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

Gängige Slash-/Source-Befehle:

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## Belegpunkte

Vertraue keinen synthetischen Benchmark-Behauptungen. Code Brain liefert Checks, die du in deinem eigenen Repo ausführen kannst:

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

Was diese belegen:

- Install- und Upgrade-Assets existieren für Claude, Codex und Antigravity
- die Upgrade-Planung über das öffentliche Repo funktioniert im Dry-Run-Modus, ohne Dateien zu berühren
- der strenge Doctor verifiziert Konfiguration, Index-Frische, Manifest, Audit-Chain, Secret-Scan, Hot-Path-SLO, begrenzte generierte Artefakte und Befehlsregistrierung
- das Usage-Reporting liest tatsächliche Claude-/Codex-Logs, statt Token-Einsparungen zu schätzen

Für ein öffentliches README solltest du mit diesen reproduzierbaren Checks beginnen. Füge Benchmark-Zahlen nur dann hinzu, wenn sie von einem wiederholbaren Skript in `scripts/` oder CI generiert werden.

## Was installiert wird

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

Manuelle Installation, Upgrade, Deinstallation:

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Globales Antigravity-MCP ist ausschließlich per Opt-in:

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

Der macOS/Linux-Top-Level-Installer kann das globale Claude/Codex-Kit installieren. Er sichert vorhandene Dateien und führt nur den verwalteten Code-Brain-Block in `~/.claude/CLAUDE.md` und `~/.codex/AGENTS.md` zusammen; Claude-Settings, -Hooks, -Befehle, -Agents und -Skills werden unter `~/.claude/` zusammengeführt oder kopiert. Das globale Antigravity-Setup aktualisiert den `code-brain`-Eintrag nur auf ausdrückliche Anforderung.

## Token- und Disk-Standardwerte

Standard-MCP-Profil:

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

Tool-Bereitstellung nach Profil:

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

Obergrenzen für generierte Artefakte:

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

Manuelle Bereinigung:

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

`doctor --strict` lässt `generated_artifacts_bounded` fehlschlagen, wenn die begrenzten Dateien über ihre Limits wachsen.

## Sicherheit und Hygiene für öffentliche Repos

- Reale Secrets nicht lesen, ausgeben, editieren oder committen.
- `.env`, Keys, Tokens, Zertifikate, Passwort-Stores, Laufzeitzustand und privates Gedächtnis bleiben aus dem öffentlichen Quell-Repo heraus.
- Installer kopieren keine Quell-Daten aus `.ai/memory/*` oder `.ai/runtime/state/*` in Zielprojekte.
- Hook-/MCP-Hot-Paths sind lokal und rufen das Netzwerk nicht auf.
- `AI_INSTALL_GLOBAL_ANTIGRAVITY=1` ist erforderlich, bevor eine globale Antigravity-Datei geändert wird.
- CI und Release-Kandidaten sollten `make lint`, gezielte Tests, `make doctor`, Lockfile-Checks und `make release-gate` bestehen.

## Architekturübersicht

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

## Lizenz

Apache-2.0.
