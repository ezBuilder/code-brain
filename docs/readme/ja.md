# Code Brain

[한국어](ko.md) · [English](../../README.md) · [中文](zh-CN.md) · 日本語 · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain は repo-local な AI コーディング基盤です。Claude Code、Codex CLI、Google Antigravity が同じワークスペースで `.ai/` メモリ、BM25 コード検索、hook ポリシー、MCP ツール、監査ログ、アップグレード経路を共有できます。

## 強み

- 複数エージェントが 1 つの repo-local brain を共有します。
- 既定の `usage` MCP profile は高頻度ツールだけを公開し、token 負荷を抑えます。
- BM25/FTS5 と `context_pack` で、ファイルを大量投入する前に対象を絞ります。
- `code_read_hashline` が編集前の line+sha アンカーを提供します。
- hooks が危険な git、secret leak、広すぎる grep/find、長大出力を止めます。
- runtime JSONL/log/evidence には上限があり、`doctor` が検査します。
- `/cb-upgrade` または `.ai/bin/ai upgrade latest --json` で公開 GitHub repo から更新できます。

## インストール

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

完了後、新しい Claude/Codex/Antigravity セッションを開きます。

## アップグレード

```bash
.ai/bin/ai upgrade latest --json
```

エージェント内では `/cb-upgrade` を実行します。成功後は新しいセッションを開いてください。

## 再現可能な検証

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

License: Apache-2.0.
