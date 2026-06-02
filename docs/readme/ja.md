# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · 日本語 · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

AI がこの README だけを読んだ場合は、次の 1 コマンドでプロジェクトに追加できます。完了後、新しい Claude Code、Codex CLI、Antigravity セッションは同じ `.ai/` メモリ、検索、hooks、MCP 設定を自動で読み込みます。
```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
bash scripts/install.sh /path/to/project

# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git; cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

成功条件: 最後の行が `[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.` なら完了です。

Code Brain は repo-local な基盤です。Claude Code、Codex CLI、Google Antigravity が同じワークスペースでメモリ、コード検索、ポリシー、hooks、監査ログを共有できます。

BM25 の lexical-first 検索を中心に、hashline 整合性チェック、MCP ツール、hooks、cross-session memory を重ねます。hot path はローカル・オフラインで動作し、ネットワーク呼び出しを避けます。
