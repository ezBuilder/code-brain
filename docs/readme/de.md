# Code Brain

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · Deutsch

Code Brain ist repo-lokale Infrastruktur fuer Coding-Agenten. Claude Code, Codex CLI und Google Antigravity teilen denselben `.ai/`-Speicher, BM25-Codesuche, Hook-Policy, MCP-Tools, Audit Trail und Upgrade-Pfad.

## Warum es auffaellt

- Mehrere Agenten teilen ein repo-local brain.
- Das MCP-Profil `usage` zeigt nur haeufige Tools und reduziert Token-Druck.
- BM25/FTS5 und `context_pack` grenzen Code ein, bevor zu viel gelesen wird.
- `code_read_hashline` liefert line+sha-Anker vor Edits.
- Hooks blockieren destruktives git, Secrets, zu breite grep/find-Befehle und riesige Ausgaben.
- Generierte JSONL/log/evidence-Dateien haben Limits und werden von `doctor` geprueft.
- `/cb-upgrade` oder `.ai/bin/ai upgrade latest --json` aktualisieren aus dem oeffentlichen GitHub-Repo.

## Installation

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

In einer interaktiven macOS/Linux-Shell bietet der Installer standardmaessig auch das globale Claude/Codex-Kit an. Vorhandene `~/.claude/CLAUDE.md` und `~/.codex/AGENTS.md` werden gesichert und behalten; Code Brain fuegt nur seinen verwalteten Block hinzu oder aktualisiert ihn. In CI oder nicht interaktiven Installationen werden globale Schreibvorgaenge ohne `--global` uebersprungen.

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Danach eine neue Claude/Codex/Antigravity-Session starten.

## Upgrade

```bash
.ai/bin/ai upgrade latest --json
```

In einer Agent-Session `/cb-upgrade` ausfuehren. Nach Erfolg eine neue Session starten.

## Reproduzierbare Checks

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

License: Apache-2.0.
