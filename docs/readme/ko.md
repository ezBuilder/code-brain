# Code Brain

한국어 · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain은 Claude Code, Codex CLI, Google Antigravity가 한 repo 안에서 같은 `.ai/` 메모리, BM25 코드 검색, hook 정책, MCP 도구, audit trail, 업그레이드 경로를 공유하게 만드는 repo-local 에이전트 인프라다.

핵심은 단순하다. 에이전트가 맥락을 잊고, 코드를 과하게 읽고, 긴 출력을 쏟고, 도구마다 다른 상태로 drift 되는 문제를 repo 안에서 잡는다.

## 내세울 포인트

- 여러 에이전트가 하나의 brain을 공유한다.
- 기본 MCP는 `usage` profile이라 토큰 비용이 낮다.
- BM25/FTS5 검색과 `context_pack`으로 필요한 코드부터 좁힌다.
- `code_read_hashline`으로 편집 전 line+sha 앵커를 확인한다.
- hook이 destructive git, secret leak, broad grep/find, 긴 출력 dump를 막는다.
- runtime JSONL/log/evidence 파일은 cap이 있고 `doctor`가 감시한다.
- `/cb-upgrade` 또는 `.ai/bin/ai upgrade latest --json`으로 공개 GitHub repo 기준 업그레이드가 가능하다.
- source repo의 private memory/state는 target project로 복사하지 않는다.

## 설치

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

대화형 macOS/Linux 설치는 Claude/Codex 전역 kit도 기본으로 제안한다. 기존 `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`는 백업하고 보존하며, Code Brain managed block만 추가/갱신한다. CI나 비대화형 설치는 `--global`을 명시하지 않으면 전역 쓰기를 건너뛴다.

Windows:

```powershell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

성공 후 새 Claude/Codex/Antigravity 세션을 열면 적용된다.

## 업그레이드

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

에이전트 세션에서는 `/cb-upgrade`를 실행한다. 성공 후 새 세션을 열어야 새 hook, MCP, `AGENTS.md`, `CLAUDE.md`가 적용된다.

처음 설치된 구버전 사용자는 raw script로 1회 bootstrap할 수 있다.

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

비대화형 bootstrap에서 전역 kit까지 설치하려면 `--global`을 붙인다.

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

## 벤치마크 대신 증명

허술한 벤치마크 숫자보다 재현 가능한 검증을 앞세운다.

```bash
make lint
.ai/bin/ai upgrade latest --dry-run --json
.ai/bin/ai index rebuild --json
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

`doctor --strict`는 config, index freshness, manifest, audit chain, secret scan, hot-path SLO, generated artifact cap, command registration을 확인한다. 공개 README에는 조작 가능한 숫자보다 이 검증 루트를 먼저 보여 주는 편이 낫다.

## 기본 명령

```text
/cb-usage    토큰/활동
/cb-search   코드 검색
/cb-health   상태 요약
/cb-doctor   엄격 진단
/cb-exec     bounded sandbox 실행
/cb-upgrade  공개 repo 업그레이드
```

License: Apache-2.0.
