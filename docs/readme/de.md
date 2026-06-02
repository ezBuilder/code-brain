# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · Deutsch

Code Brain in ein Repository installieren:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain ist repo-lokale Infrastruktur, mit der Claude Code, Codex CLI und Google Antigravity im selben Workspace Speicher, Codesuche, Policies, Hooks und Audit-Trails teilen.

Die Suche bleibt lexical-first mit BM25, hashline-Integritaetspruefung, MCP-Tools, Hooks und Cross-Session-Memory. Hot paths laufen lokal und offline und vermeiden Netzwerkaufrufe.
