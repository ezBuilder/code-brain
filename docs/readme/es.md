# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · [日本語](ja.md) · Español · [Français](fr.md) · [Deutsch](de.md)

Si una IA solo lee este README, basta con ejecutar un comando para instalar Code Brain en el proyecto. Al terminar, las nuevas sesiones de Claude Code, Codex CLI y Antigravity cargan automaticamente la misma memoria `.ai/`, busqueda, hooks y MCP.
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Exito: la ultima linea debe decir `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.`

Code Brain es infraestructura local al repositorio para que Claude Code, Codex CLI y Google Antigravity compartan memoria, busqueda de codigo, politicas, hooks y auditoria en un mismo workspace.

Mantiene una busqueda lexical-first con BM25, comprobaciones hashline, herramientas MCP, hooks y memoria entre sesiones. Las rutas criticas son locales, offline y evitan llamadas de red.
