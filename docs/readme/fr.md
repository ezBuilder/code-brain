# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · Français · [Deutsch](de.md)

Si une IA ne lit que ce README, une seule commande suffit pour installer Code Brain dans le projet. Ensuite, les nouvelles sessions Claude Code, Codex CLI et Antigravity chargent automatiquement la meme memoire `.ai/`, recherche, hooks et configuration MCP.
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Succes: la derniere ligne doit afficher `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.`

Code Brain est une infrastructure locale au depot qui permet a Claude Code, Codex CLI et Google Antigravity de partager la memoire, la recherche de code, les politiques, les hooks et l'audit dans le meme workspace.

La recherche reste lexical-first avec BM25, des controles hashline, des outils MCP, des hooks et une memoire entre sessions. Les chemins critiques sont locaux, hors ligne et evitent les appels reseau.
