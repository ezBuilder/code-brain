<p align="center"><img src="../assets/social-preview.png" alt="Code Brain — repo-local memory, code search, MCP and hooks for AI coding agents" width="820"></p>

# Code Brain

[![Release](https://img.shields.io/github/v/release/ezBuilder/code-brain?sort=semver&style=flat-square&color=2962FF)](https://github.com/ezBuilder/code-brain/releases)
[![License](https://img.shields.io/github/license/ezBuilder/code-brain?style=flat-square&color=4CAF50)](https://github.com/ezBuilder/code-brain/blob/main/LICENSE)
[![Release Gate](https://img.shields.io/github/actions/workflow/status/ezBuilder/code-brain/release-gate.yml?branch=main&style=flat-square&label=release-gate)](https://github.com/ezBuilder/code-brain/actions/workflows/release-gate.yml)
[![Stars](https://img.shields.io/github/stars/ezBuilder/code-brain?style=flat-square&color=FFC107)](https://github.com/ezBuilder/code-brain/stargazers)

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-8A2BE2?style=flat-square)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-ready-111111?style=flat-square)
![Antigravity](https://img.shields.io/badge/Antigravity-ready-4285F4?style=flat-square)

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · 日本語 · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain は、本格的な AI コーディングエージェントのためのリポジトリローカルなインフラストラクチャです。Claude Code、Codex CLI、Google Antigravity に対して、同じプロジェクトメモリ、BM25 コード検索、フックポリシー、MCP ツール、監査証跡、そしてアップグレード経路を、ひとつのワークスペース内で提供します。

これは、ひとつの不都合な真実のために構築されています。エージェントは強力ですが、コンテキストを忘れ、コードを読みすぎ、膨大な出力を吐き出し、ツールをまたいでドリフトします。Code Brain は、リポジトリをエージェント対応のオペレーティングレイヤーへと変えます。

## 際立つ理由

- **複数のエージェントにひとつの頭脳。** Claude、Codex、Antigravity が同じ `.ai/` コントラクト、メモリ、検索インデックス、フック、コマンド面を共有します。
- **デフォルトでトークンを意識。** MCP はリーンな `usage` プロファイルで起動します。最初に公開されるのは `obs_usage`、`code_query`、`context_pack`、`code_read_hashline`、`tool_search` のみです。
- **散らかす前に検索。** エージェントは、ファイルを盲目的にプロンプトへ流し込む代わりに、BM25/FTS5 とコンパクトなコンテキストパックでコードを特定します。
- **ハッシュライン安全な編集。** `code_read_hashline` は編集前に行番号 + sha のアンカーを提供し、古くなったパッチや位置のずれたパッチを削減します。
- **ホットパス上のガードレール。** フックは、破壊的な git、広範な grep/find のダンプ、シークレット漏洩、長大な出力を、それらがトークンを浪費したりデータを露出したりする前にブロックします。
- **境界付きのメモリとアーティファクト。** ランタイムが生成する JSONL / ログ / 証跡ファイルには上限と doctor チェックがあり、リポジトリが無言のうちに肥大化しないようにします。
- **オフラインでのメモリ統合。** スリープ時の `ai memory page-in` が、サリエンスでランク付けされた HOT キャッシュを事前にウォームアップし、次のセッションがネットワーク呼び出しなしで、よりタイトで少ないトークンのコンテキストをロードできるようにします。
- **公開リポジトリからのアップグレード経路。** インストール済みのプロジェクトは `/cb-upgrade` または `.ai/bin/ai upgrade latest --json` を実行して、GitHub からプルし再ブートストラップできます。
- **公開リリースの衛生。** ソースのメモリ / 状態はターゲットプロジェクトへ伝播されません。シークレットスキャン、監査チェーン、マニフェスト、生成アーティファクトのチェックが組み込まれています。

## クイックインストール

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

インタラクティブシェルでは、macOS/Linux のインストーラはデフォルトで Claude/Codex のグローバルキットも提供します。既存の `~/.claude/CLAUDE.md` と `~/.codex/AGENTS.md` はバックアップされ保持されます。Code Brain はその管理対象ブロックのみを追加または更新します。CI および非インタラクティブなインストールでは、`--global` を渡さない限りグローバル書き込みをスキップします。明示的にオプトアウトするには `--no-global` を使用してください。

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

成功すると次で終わります:

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

インストール後は、新しい Claude/Codex/Antigravity セッションを開いてください。

## GitHub からのアップグレード

インストール済みのプロジェクト内で:

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

エージェントセッション内では、次を実行します:

```text
/cb-upgrade
```

アップグレードが成功したら、新しいフック、MCP 設定、`AGENTS.md`、`CLAUDE.md` がロードされるよう、新しいエージェントセッションを開いてください。

ローカルクローンを保持せずに初回ブートストラップする場合:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

グローバルキット付きで非インタラクティブにブートストラップする場合:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

バージョンまたはブランチを固定する場合:

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

アップグレードは明示的です。`SessionStart` フックと MCP のホットパスはネットワークを呼び出しません。

## エージェントワークフロー

狭く始めて、それからアンカーで編集します:

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

デフォルトの MCP ツール:

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

よく使うスラッシュ / ソースコマンド:

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## 証明ポイント

合成ベンチマークの主張を信用しないでください。Code Brain は、あなた自身のリポジトリで実行できるチェックを同梱しています:

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

これらが証明するもの:

- Claude、Codex、Antigravity 向けのインストールおよびアップグレードのアセットが存在すること
- 公開リポジトリからのアップグレード計画が、ドライランモードでファイルに触れることなく機能すること
- strict doctor が、設定、インデックスの鮮度、マニフェスト、監査チェーン、シークレットスキャン、ホットパス SLO、境界付きの生成アーティファクト、コマンド登録を検証すること
- 使用状況レポートが、トークン節約を見積もるのではなく、実際の Claude/Codex ログを読み取ること

公開 README では、これらの再現可能なチェックを先頭に置いてください。ベンチマーク数値は、`scripts/` または CI 内の再現可能なスクリプトによって生成された場合にのみ追加してください。

## インストールされるもの

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

手動でのインストール、アップグレード、アンインストール:

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Antigravity のグローバル MCP はオプトインのみです:

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

macOS/Linux のトップレベルインストーラは、Claude/Codex のグローバルキットをインストールできます。既存のファイルをバックアップし、管理対象の Code Brain ブロックのみを `~/.claude/CLAUDE.md` と `~/.codex/AGENTS.md` にマージします。Claude の設定、フック、コマンド、エージェント、スキルは `~/.claude/` の下にマージまたはコピーされます。Antigravity のグローバルセットアップは、明示的に要求された場合にのみ `code-brain` エントリを更新します。

## トークンとディスクのデフォルト

デフォルトの MCP プロファイル:

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

プロファイルごとのツール公開:

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

生成アーティファクトの上限:

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

手動クリーンアップ:

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

上限付きのファイルが制限を超えて増加すると、`doctor --strict` は `generated_artifacts_bounded` を失敗とします。

## セキュリティと公開リポジトリの衛生

- 実際のシークレットを読み取ったり、出力したり、編集したり、コミットしたりしないでください。
- `.env`、キー、トークン、証明書、パスワードストア、ランタイム状態、プライベートメモリは、公開ソースリポジトリの外に保ちます。
- インストーラは、ソースの `.ai/memory/*` や `.ai/runtime/state/*` のデータをターゲットプロジェクトにコピーしません。
- フック / MCP のホットパスはローカルであり、ネットワークを呼び出しません。
- グローバルな Antigravity ファイルを変更する前には、`AI_INSTALL_GLOBAL_ANTIGRAVITY=1` が必要です。
- CI とリリース候補は、`make lint`、対象を絞ったテスト、`make doctor`、ロックファイルチェック、`make release-gate` をパスすべきです。

## アーキテクチャマップ

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

## ライセンス

Apache-2.0.
