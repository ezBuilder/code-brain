# Code Brain

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · Français · [Deutsch](de.md)

Code Brain est une infrastructure locale au depot pour agents de code. Claude Code, Codex CLI et Google Antigravity partagent la meme memoire `.ai/`, recherche BM25, politique de hooks, outils MCP, audit trail et voie de mise a jour.

## Pourquoi c'est different

- Plusieurs agents partagent un meme brain local au repo.
- Le profil MCP `usage` expose seulement les outils frequents et reduit la pression token.
- BM25/FTS5 et `context_pack` ciblent le code avant de lire trop large.
- `code_read_hashline` fournit des ancres line+sha avant edition.
- Les hooks bloquent git destructif, secrets, grep/find trop larges et sorties massives.
- Les JSONL/log/evidence generes ont des limites et `doctor` les verifie.
- `/cb-upgrade` ou `.ai/bin/ai upgrade latest --json` met a jour depuis le repo GitHub public.

## Installation

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

Dans un shell macOS/Linux interactif, l'installeur propose aussi par defaut le kit global Claude/Codex. Les fichiers existants `~/.claude/CLAUDE.md` et `~/.codex/AGENTS.md` sont sauvegardes et conserves; Code Brain ajoute ou met a jour seulement son bloc gere. En CI ou en mode non interactif, aucune ecriture globale n'est faite sans `--global`.

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Ouvrez ensuite une nouvelle session Claude/Codex/Antigravity.

## Mise a jour

```bash
.ai/bin/ai upgrade latest --json
```

Dans une session agent, lancez `/cb-upgrade`. Apres succes, ouvrez une nouvelle session.

## Preuves reproductibles

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

License: Apache-2.0.
