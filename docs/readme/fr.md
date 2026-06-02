# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · Français · [Deutsch](de.md)

Installer Code Brain dans un depot:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain est une infrastructure locale au depot qui permet a Claude Code, Codex CLI et Google Antigravity de partager la memoire, la recherche de code, les politiques, les hooks et l'audit dans le meme workspace.

La recherche reste lexical-first avec BM25, des controles hashline, des outils MCP, des hooks et une memoire entre sessions. Les chemins critiques sont locaux, hors ligne et evitent les appels reseau.
