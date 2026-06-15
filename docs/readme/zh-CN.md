# Code Brain

[한국어](ko.md) · [English](../../README.md) · 中文 · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain 是仓库本地的 AI 编码基础设施。Claude Code、Codex CLI 和 Google Antigravity 可以在同一个工作区共享 `.ai/` 记忆、BM25 代码搜索、hook 策略、MCP 工具、审计轨迹和升级路径。

## 亮点

- 多个 AI 代理共享同一个 repo-local brain。
- 默认 `usage` MCP profile，只暴露少量高频工具，降低 token 压力。
- BM25/FTS5 搜索和 `context_pack` 让代理先缩小范围，而不是盲目读取大量文件。
- `code_read_hashline` 在编辑前提供 line+sha 锚点。
- hooks 会拦截危险 git、secret leak、宽泛 grep/find、超长输出。
- runtime JSONL/log/evidence 文件有大小上限，并由 `doctor` 检查。
- `/cb-upgrade` 或 `.ai/bin/ai upgrade latest --json` 可从公开 GitHub repo 升级。

## 安装

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

在交互式 macOS/Linux shell 中，安装器会默认建议安装 Claude/Codex 全局 kit。已有的 `~/.claude/CLAUDE.md` 和 `~/.codex/AGENTS.md` 会先备份并保留，Code Brain 只添加或刷新自己的 managed block。CI 和非交互式安装默认跳过全局写入，除非传入 `--global`。

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

安装后打开新的 Claude/Codex/Antigravity 会话。

## 升级

```bash
.ai/bin/ai upgrade latest --json
```

在代理会话中运行 `/cb-upgrade`。成功后需要打开新会话。

## 可复现验证

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

License: Apache-2.0.
