# Code Brain

[한국어](../../README.md) · English · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Attach Code Brain to a repository:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain is repo-local infrastructure that lets Claude Code, Codex CLI, and Google Antigravity share the same memory, code search, policy, hooks, and audit trail inside one workspace.

It keeps search lexical-first with BM25, hashline integrity checks, MCP tools, hooks, and cross-session memory. Hot paths are local, offline, and designed to avoid network calls.
