# Code Brain

[한국어](../../README.md) · [English](en.md) · 中文 · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

把 Code Brain 安装到一个仓库:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain 是仓库本地的基础设施，让 Claude Code、Codex CLI 和 Google Antigravity 在同一个工作区共享记忆、代码搜索、策略、hooks 和审计记录。

它以 BM25 词法搜索为核心，并加入 hashline 完整性检查、MCP 工具、hooks 和跨会话记忆。热路径在本地离线运行，并避免网络调用。
