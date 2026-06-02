# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · Español · [Français](fr.md) · [Deutsch](de.md)

Instala Code Brain en un repositorio:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain es infraestructura local al repositorio para que Claude Code, Codex CLI y Google Antigravity compartan memoria, busqueda de codigo, politicas, hooks y auditoria en un mismo workspace.

Mantiene una busqueda lexical-first con BM25, comprobaciones hashline, herramientas MCP, hooks y memoria entre sesiones. Las rutas criticas son locales, offline y evitan llamadas de red.
