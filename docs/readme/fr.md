<p align="center"><img src="../assets/social-preview.png" alt="Code Brain — repo-local memory, code search, MCP and hooks for AI coding agents" width="820"></p>

# Code Brain

[![Release](https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF)](https://github.com/ezBuilder/code-brain/releases)
[![License](https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50)](https://github.com/ezBuilder/code-brain/blob/main/LICENSE)
[![Release Gate](https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate)](https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml)
[![Stars](https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107)](https://github.com/ezBuilder/code-brain/stargazers)

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-8A2BE2?style=flat-square)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-ready-111111?style=flat-square)
![Antigravity](https://img.shields.io/badge/Antigravity-ready-4285F4?style=flat-square)

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · Français · [Deutsch](de.md)

Code Brain est une infrastructure locale au dépôt destinée aux agents de codage IA sérieux. Elle offre à Claude Code, Codex CLI et Google Antigravity la même mémoire de projet, la même recherche de code BM25, la même politique de hooks, les mêmes outils MCP, la même piste d'audit et le même chemin de mise à niveau au sein d'un unique espace de travail.

Elle est conçue autour d'une vérité dérangeante : les agents sont puissants, mais ils oublient le contexte, lisent trop de code, déversent des sorties énormes et dérivent d'un outil à l'autre. Code Brain transforme un dépôt en une couche d'exploitation prête pour les agents.

## Pourquoi elle se démarque

- **Un seul cerveau pour plusieurs agents.** Claude, Codex et Antigravity partagent le même contrat `.ai/`, la même mémoire, le même index de recherche, les mêmes hooks et la même surface de commandes.
- **Soucieuse des tokens par défaut.** MCP démarre dans le profil épuré `usage` : seuls `obs_usage`, `code_query`, `context_pack`, `code_read_hashline` et `tool_search` sont exposés d'emblée.
- **Chercher avant de s'éparpiller.** Les agents localisent le code avec BM25/FTS5 et des packs de contexte compacts au lieu de déverser aveuglément des fichiers dans le prompt.
- **Modifications sécurisées par hashline.** `code_read_hashline` fournit des ancres ligne+sha avant les modifications, réduisant les correctifs périmés ou mal placés.
- **Garde-fous sur le chemin critique.** Les hooks bloquent les commandes git destructrices, les déversements grep/find trop larges, les fuites de secrets et les sorties longues avant qu'ils ne gaspillent des tokens ou n'exposent des données.
- **Mémoire et artefacts bornés.** Les fichiers JSONL/log/preuve générés à l'exécution ont des plafonds et des contrôles doctor afin qu'un dépôt ne gonfle pas silencieusement.
- **Consolidation de mémoire hors ligne.** En temps de veille, `ai memory page-in` préchauffe un cache HOT classé par saillance afin que la session suivante charge un contexte plus resserré et moins gourmand en tokens, sans aucun appel réseau.
- **Chemin de mise à niveau depuis le dépôt public.** Les projets installés peuvent lancer `/cb-upgrade` ou `.ai/bin/ai upgrade latest --json` pour récupérer depuis GitHub et réamorcer.
- **Hygiène pour publication publique.** La mémoire/l'état source ne sont pas propagés dans les projets cibles ; le scan de secrets, la chaîne d'audit, le manifeste et les contrôles d'artefacts générés sont intégrés.

## Installation rapide

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

Dans un shell interactif, l'installeur macOS/Linux propose aussi par défaut le kit global Claude/Codex. Les fichiers `~/.claude/CLAUDE.md` et `~/.codex/AGENTS.md` existants sont sauvegardés et préservés ; Code Brain ajoute ou rafraîchit uniquement son bloc géré. Les installations CI et non interactives ignorent les écritures globales sauf si vous passez `--global` ; utilisez `--no-global` pour vous désinscrire explicitement.

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Le succès se termine par :

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

Ouvrez une nouvelle session Claude/Codex/Antigravity après l'installation.

## Mise à niveau depuis GitHub

À l'intérieur d'un projet installé :

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

À l'intérieur d'une session d'agent, lancez :

```text
/cb-upgrade
```

Après une mise à niveau réussie, ouvrez une nouvelle session d'agent afin que les nouveaux hooks, la configuration MCP, `AGENTS.md` et `CLAUDE.md` soient chargés.

Pour un amorçage initial sans conserver de clone local :

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

Pour un amorçage non interactif avec le kit global :

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

Épinglez une version ou une branche :

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

Les mises à niveau sont explicites. Les hooks `SessionStart` et les chemins critiques MCP n'appellent pas le réseau.

## Flux de travail de l'agent

Commencez étroit, puis modifiez avec des ancres :

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

Outils MCP par défaut :

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

Commandes slash/source courantes :

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## Éléments de preuve

Ne faites pas confiance aux affirmations issues de benchmarks synthétiques. Code Brain livre des contrôles que vous pouvez exécuter dans votre propre dépôt :

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

Ce que ces éléments prouvent :

- les ressources d'installation et de mise à niveau existent pour Claude, Codex et Antigravity
- la planification de mise à niveau depuis le dépôt public fonctionne sans toucher aux fichiers en mode dry-run
- le doctor strict vérifie la configuration, la fraîcheur de l'index, le manifeste, la chaîne d'audit, le scan de secrets, le SLO du chemin critique, le caractère borné des artefacts générés et l'enregistrement des commandes
- le rapport d'usage lit les véritables journaux Claude/Codex au lieu d'estimer les économies de tokens

Pour un README public, mettez en avant ces contrôles reproductibles. N'ajoutez des chiffres de benchmark que lorsqu'ils sont générés par un script reproductible dans `scripts/` ou la CI.

## Ce qui est installé

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

Installation, mise à niveau et désinstallation manuelles :

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Le MCP global Antigravity est en opt-in uniquement :

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

L'installeur de premier niveau macOS/Linux peut installer le kit global Claude/Codex. Il sauvegarde les fichiers existants et fusionne uniquement le bloc géré de Code Brain dans `~/.claude/CLAUDE.md` et `~/.codex/AGENTS.md` ; les paramètres, hooks, commandes, agents et skills de Claude sont fusionnés ou copiés sous `~/.claude/`. La configuration globale d'Antigravity ne met à jour l'entrée `code-brain` que lorsque cela est explicitement demandé.

## Valeurs par défaut de tokens et de disque

Profil MCP par défaut :

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

Exposition des outils par profil :

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

Plafonds des artefacts générés :

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

Nettoyage manuel :

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

`doctor --strict` échoue sur `generated_artifacts_bounded` si les fichiers plafonnés dépassent leurs limites.

## Sécurité et hygiène du dépôt public

- Ne lisez, n'affichez, ne modifiez et ne committez jamais de vrais secrets.
- Les fichiers `.env`, clés, tokens, certificats, gestionnaires de mots de passe, l'état d'exécution et la mémoire privée restent hors du dépôt source public.
- Les installeurs ne copient pas les données source `.ai/memory/*` ou `.ai/runtime/state/*` dans les projets cibles.
- Les chemins critiques des hooks/MCP sont locaux et n'appellent pas le réseau.
- `AI_INSTALL_GLOBAL_ANTIGRAVITY=1` est requis avant toute modification d'un fichier Antigravity global.
- La CI et les candidats à publication doivent passer `make lint`, les tests ciblés, `make doctor`, les contrôles de lockfile et `make release-gate`.

## Carte d'architecture

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

## Licence

Apache-2.0.
