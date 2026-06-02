# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · Deutsch

Wenn eine KI nur diese README liest, reicht ein Befehl, um Code Brain im Projekt zu installieren. Danach laden neue Claude Code-, Codex CLI- und Antigravity-Sessions automatisch denselben `.ai/`-Speicher, die Suche, Hooks und MCP-Konfiguration.
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Erfolg: Die letzte Zeile lautet `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.`

Code Brain ist repo-lokale Infrastruktur, mit der Claude Code, Codex CLI und Google Antigravity im selben Workspace Speicher, Codesuche, Policies, Hooks und Audit-Trails teilen.

Die Suche bleibt lexical-first mit BM25, hashline-Integritaetspruefung, MCP-Tools, Hooks und Cross-Session-Memory. Hot paths laufen lokal und offline und vermeiden Netzwerkaufrufe.
