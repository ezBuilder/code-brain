# Code Brain

[한국어](../../README.md) · [English](en.md) · [中文](zh-CN.md) · 日本語 · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain をリポジトリに追加する:
```bash
git clone https://github.com/ezBuilder/code-brain.git && cd code-brain
./scripts/install-into.sh install /path/to/project
make install-into TARGET=/path/to/repo
make upgrade-in TARGET=/path/to/repo
make uninstall-from TARGET=/path/to/repo
cd /path/to/project && .ai/bin/ai session start --agent codex --query "current task" --json && .ai/bin/ai doctor --strict --json
```

Code Brain は repo-local な基盤です。Claude Code、Codex CLI、Google Antigravity が同じワークスペースでメモリ、コード検索、ポリシー、hooks、監査ログを共有できます。

BM25 の lexical-first 検索を中心に、hashline 整合性チェック、MCP ツール、hooks、cross-session memory を重ねます。hot path はローカル・オフラインで動作し、ネットワーク呼び出しを避けます。
