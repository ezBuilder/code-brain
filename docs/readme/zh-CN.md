# Code Brain

[한국어](../../README.md) · [English](en.md) · 中文 · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

如果 AI 只读这个 README，只需要运行下面一条安装命令。安装完成后，新的 Claude Code、Codex CLI 和 Antigravity 会话会自动加载同一套 `.ai/` 记忆、搜索、hooks 和 MCP 配置。
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

成功标志：最后一行显示 `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.`

Code Brain 是仓库本地的基础设施，让 Claude Code、Codex CLI 和 Google Antigravity 在同一个工作区共享记忆、代码搜索、策略、hooks 和审计记录。

它以 BM25 词法搜索为核心，并加入 hashline 完整性检查、MCP 工具、hooks 和跨会话记忆。热路径在本地离线运行，并避免网络调用。
