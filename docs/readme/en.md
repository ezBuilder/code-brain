# Code Brain

[한국어](../../README.md) · English · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

If an AI only reads this README, run one command to attach Code Brain to a project. After installation, new Claude Code, Codex CLI, and Antigravity sessions automatically load the same `.ai/` memory, search, hooks, and MCP wiring.
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

Success means the final line says: `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.`

Code Brain is repo-local infrastructure that lets Claude Code, Codex CLI, and Google Antigravity share the same memory, code search, policy, hooks, and audit trail inside one workspace.

It keeps search lexical-first with BM25, hashline integrity checks, MCP tools, hooks, and cross-session memory. Hot paths are local, offline, and designed to avoid network calls.
