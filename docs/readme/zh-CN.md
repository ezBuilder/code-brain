# Code Brain

[![Release](https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF)](https://github.com/ezBuilder/code-brain/releases)
[![License](https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50)](https://github.com/ezBuilder/code-brain/blob/main/LICENSE)
[![Release Gate](https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate)](https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml)
[![Stars](https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107)](https://github.com/ezBuilder/code-brain/stargazers)

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-8A2BE2?style=flat-square)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-ready-111111?style=flat-square)
![Antigravity](https://img.shields.io/badge/Antigravity-ready-4285F4?style=flat-square)

[한국어](ko.md) · [English](../../README.md) · 中文 · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain 是面向严肃 AI 编码代理的仓库本地基础设施。它在同一个工作区内为 Claude Code、Codex CLI 和 Google Antigravity 提供一致的项目记忆、BM25 代码搜索、hook 策略、MCP 工具、审计轨迹和升级路径。

它的构建源于一个令人不安的事实：代理很强大，但它们会遗忘上下文、过度读取代码、倾倒海量输出，并在不同工具之间产生漂移。Code Brain 把一个仓库变成可供代理使用的操作层。

## 为什么与众不同

- **多代理共用一个大脑。** Claude、Codex 和 Antigravity 共享同一套 `.ai/` 契约、记忆、搜索索引、hook 和命令界面。
- **默认就关注 token。** MCP 以精简的 `usage` 配置启动：前期只暴露 `obs_usage`、`code_query`、`context_pack`、`code_read_hashline` 和 `tool_search`。
- **先搜索，再展开。** 代理使用 BM25/FTS5 和紧凑的上下文包定位代码，而不是盲目地把文件倾倒进提示词。
- **基于 hashline 的安全编辑。** `code_read_hashline` 在编辑前给出行号 + sha 锚点，减少陈旧或错位的补丁。
- **热路径上的护栏。** hook 会在破坏性 git、宽泛的 grep/find 倾倒、密钥泄露和过长输出浪费 token 或暴露数据之前将其拦截。
- **有界的记忆和产物。** 运行时生成的 JSONL/日志/证据文件都有上限和 doctor 检查，因此仓库不会悄无声息地膨胀。
- **离线记忆整合。** 睡眠期的 `ai memory page-in` 会预热一个按显著性排序的 HOT 缓存，使下一次会话在不发起任何网络调用的情况下加载更紧凑、更省 token 的上下文。
- **面向公共仓库的升级路径。** 已安装的项目可以运行 `/cb-upgrade` 或 `.ai/bin/ai upgrade latest --json`，从 GitHub 拉取并重新引导。
- **公共发布的卫生。** 源端记忆/状态不会传播到目标项目；内置了密钥扫描、审计链、清单和生成产物检查。

## 快速安装

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

在交互式 shell 中，macOS/Linux 安装程序默认还会提供 Claude/Codex 全局套件。已有的 `~/.claude/CLAUDE.md` 和 `~/.codex/AGENTS.md` 会被备份并保留；Code Brain 只会添加或刷新它所管理的那一块。CI 和非交互式安装会跳过全局写入，除非你传入 `--global`；使用 `--no-global` 可以显式选择退出。

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

成功时以如下内容结束：

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

安装后请打开一个新的 Claude/Codex/Antigravity 会话。

## 从 GitHub 升级

在已安装的项目内：

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

在代理会话内运行：

```text
/cb-upgrade
```

升级成功后，请打开一个新的代理会话，以便加载新的 hook、MCP 配置、`AGENTS.md` 和 `CLAUDE.md`。

如需在不保留本地克隆的情况下首次引导：

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

如需带全局套件的非交互式引导：

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

固定某个版本或分支：

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

升级是显式的。`SessionStart` hook 和 MCP 热路径不会发起网络调用。

## 代理工作流

从窄处开始，然后使用锚点进行编辑：

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

默认 MCP 工具：

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

常用斜杠/源命令：

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## 实证要点

不要相信合成的基准测试声明。Code Brain 自带了你可以在自己仓库里运行的检查：

```bash
make lint
scripts/lockfile-check.sh
uv lock --check --project .ai/runtime
uv run --project .ai/runtime python -m pytest .ai/runtime/tests/test_cli.py -k "upgrade_latest or cb_upgrade_command_assets"
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai index rebuild --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

这些检查证明了什么：

- 面向 Claude、Codex 和 Antigravity 的安装与升级产物存在
- 公共仓库升级规划在 dry-run 模式下可以正常工作而不触碰文件
- 严格的 doctor 会验证配置、索引新鲜度、清单、审计链、密钥扫描、热路径 SLO、有界的生成产物以及命令注册
- 用量报告读取的是真实的 Claude/Codex 日志，而不是估算 token 节省量

对于公共 README，应以这些可复现的检查为先导。仅当基准数字由 `scripts/` 或 CI 中可重复的脚本生成时，才将其加入。

## 安装内容

```text
.ai/                         runtime, memory structure, hooks, MCP shim
.mcp.json                    Claude Code MCP
.codex/config.toml           Codex MCP profile usage
.codex/hooks.json            Codex hooks
.claude/settings.json        Claude Code hooks
.claude/commands/            slash commands
.codex/prompts/              Codex prompts
.agents/mcp_config.json      Antigravity MCP
.agents/hooks.json           Antigravity hooks
.agents/skills/              source-command skills
.githooks/post-merge         index refresh
.githooks/post-checkout      index refresh
AGENTS.md                    seed-only mirror of .ai/AGENTS.md
CLAUDE.md                    seed-only mirror of .ai/AGENTS.md
```

手动安装、升级、卸载：

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Antigravity 全局 MCP 仅可选启用：

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

macOS/Linux 顶层安装程序可以安装 Claude/Codex 全局套件。它会备份已有文件，并只把受管的 Code Brain 块合并进 `~/.claude/CLAUDE.md` 和 `~/.codex/AGENTS.md`；Claude 的设置、hook、命令、agents 和 skills 会被合并或复制到 `~/.claude/` 下。Antigravity 全局设置仅在被显式请求时才更新 `code-brain` 条目。

## Token 与磁盘默认值

默认 MCP 配置：

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

各配置下的工具暴露：

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

生成产物上限：

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

手动清理：

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

如果有上限的文件增长超过限制，`doctor --strict` 会让 `generated_artifacts_bounded` 失败。

## 安全与公共仓库卫生

- 不要读取、打印、编辑或提交真实密钥。
- `.env`、密钥、令牌、证书、密码库、运行时状态和私有记忆都不进入公共源仓库。
- 安装程序不会把源端的 `.ai/memory/*` 或 `.ai/runtime/state/*` 数据复制到目标项目。
- hook/MCP 热路径是本地的，不会发起网络调用。
- 在更改任何全局 Antigravity 文件之前，必须设置 `AI_INSTALL_GLOBAL_ANTIGRAVITY=1`。
- CI 和发布候选版本应通过 `make lint`、有针对性的测试、`make doctor`、lockfile 检查和 `make release-gate`。

## 架构图

```text
.ai/
├── bin/                         ai / ai-hook / ai-mcp (+ PowerShell shims)
├── runtime/src/ai_core/
│   ├── search.py                BM25 FTS5 + chunking
│   ├── hashline.py              line+sha edit anchors
│   ├── hooks.py                 Claude/Codex/Antigravity hook handling
│   ├── mcp_server.py            MCP JSON-RPC stdio server
│   ├── mcp_config.py            Claude/Codex/Antigravity config dialects
│   ├── memory.py                decisions/todos/audit/events rotation
│   ├── memory_tier.py           page-out / page-in / tiering
│   ├── memory_hot.py            sleep-time salience-ranked HOT memory cache
│   ├── evidence.py              bounded evidence ledger
│   ├── doctor.py                release and safety checks
│   ├── obs.py                   usage/health/search diagnostics
│   ├── sandbox.py               bounded command output capture
│   └── security_findings.py     redacted security finding ledger
├── memory/                      per-project durable memory
├── cache/                       sqlite/sandbox/generated cache
├── generated/                   render/install manifests
└── AGENTS.md                    canonical local agent contract
```

## 许可证

Apache-2.0.
