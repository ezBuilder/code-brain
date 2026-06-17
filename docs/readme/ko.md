# Code Brain

한국어 · [English](../../README.md) · [中文](zh-CN.md) · [日本語](ja.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

Code Brain은 진지한 AI 코딩 에이전트를 위한 레포 로컬 인프라입니다. Claude Code, Codex CLI, Google Antigravity에 동일한 프로젝트 메모리, BM25 코드 검색, 훅 정책, MCP 도구, 감사 기록, 업그레이드 경로를 하나의 워크스페이스 안에서 제공합니다.

이 도구는 한 가지 불편한 진실을 위해 만들어졌습니다. 에이전트는 강력하지만 컨텍스트를 잊고, 코드를 과도하게 읽고, 거대한 출력을 쏟아내며, 여러 도구를 넘나들며 표류합니다. Code Brain은 레포지토리를 에이전트가 바로 활용할 수 있는 운영 계층으로 바꿉니다.

## Why It Stands Out

- **여러 에이전트를 위한 하나의 뇌.** Claude, Codex, Antigravity가 동일한 `.ai/` 계약, 메모리, 검색 인덱스, 훅, 명령 표면을 공유합니다.
- **기본부터 토큰을 의식.** MCP는 가벼운 `usage` 프로필로 시작합니다. 처음에는 `obs_usage`, `code_query`, `context_pack`, `code_read_hashline`, `tool_search`만 노출됩니다.
- **난잡함보다 먼저 검색.** 에이전트는 파일을 무작정 프롬프트에 쏟아붓는 대신 BM25/FTS5와 간결한 컨텍스트 팩으로 코드를 찾습니다.
- **해시라인 안전 편집.** `code_read_hashline`은 편집 전에 라인+sha 앵커를 제공하여 오래되거나 잘못 배치된 패치를 줄입니다.
- **핫 패스의 가드레일.** 훅은 파괴적 git, 광범위한 grep/find 덤프, 시크릿 유출, 긴 출력을 토큰을 낭비하거나 데이터를 노출하기 전에 차단합니다.
- **제한된 메모리와 산출물.** 런타임에서 생성되는 JSONL/로그/증거 파일에 상한과 doctor 체크가 있어 레포가 조용히 비대해지지 않습니다.
- **오프라인 메모리 통합.** 슬립 타임 `ai memory page-in`은 중요도 순으로 정렬된 HOT 캐시를 미리 예열하여, 다음 세션이 네트워크 호출 없이 더 압축되고 토큰이 적은 컨텍스트를 로드하도록 합니다.
- **공개 레포 업그레이드 경로.** 설치된 프로젝트는 `/cb-upgrade` 또는 `.ai/bin/ai upgrade latest --json`을 실행하여 GitHub에서 가져와 다시 부트스트랩할 수 있습니다.
- **공개 릴리스 위생.** 소스 메모리/상태는 대상 프로젝트로 전파되지 않으며, 시크릿 스캔, 감사 체인, 매니페스트, 생성 산출물 체크가 내장되어 있습니다.

## Quick Install

```bash
# macOS / Linux
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
bash scripts/install.sh /path/to/project
```

대화형 셸에서는 macOS/Linux 설치 프로그램이 기본적으로 Claude/Codex 글로벌 킷도 제안합니다. 기존 `~/.claude/CLAUDE.md`와 `~/.codex/AGENTS.md`는 백업되고 보존되며, Code Brain은 관리하는 블록만 추가하거나 갱신합니다. CI 및 비대화형 설치는 `--global`을 전달하지 않는 한 글로벌 쓰기를 건너뜁니다. 명시적으로 제외하려면 `--no-global`을 사용하세요.

```powershell
# Windows PowerShell
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 C:\path\to\project
```

성공하면 다음으로 끝납니다.

```text
[code-brain] installed. New AI sessions in <project> now load Code Brain memory, search, hooks, and MCP automatically.
```

설치 후 새 Claude/Codex/Antigravity 세션을 여세요.

## Upgrade From GitHub

설치된 프로젝트 내부에서:

```bash
cd /path/to/project
.ai/bin/ai upgrade latest --json
```

에이전트 세션 내부에서 실행하세요:

```text
/cb-upgrade
```

업그레이드가 성공한 후에는 새 에이전트 세션을 열어 새 훅, MCP 설정, `AGENTS.md`, `CLAUDE.md`가 로드되도록 하세요.

로컬 클론을 유지하지 않고 처음으로 부트스트랩하려면:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- /path/to/project
```

글로벌 킷과 함께 비대화형으로 부트스트랩하려면:

```bash
curl -fsSL https://raw.githubusercontent.com/ezBuilder/code-brain/main/scripts/upgrade-from-github.sh | bash -s -- --global /path/to/project
```

버전이나 브랜치를 고정하려면:

```bash
.ai/bin/ai upgrade latest --ref v0.2.0 --json
CODE_BRAIN_REF=v0.2.0 bash scripts/upgrade-from-github.sh /path/to/project
```

업그레이드는 명시적입니다. `SessionStart` 훅과 MCP 핫 패스는 네트워크를 호출하지 않습니다.

## Agent Workflow

좁게 시작한 다음 앵커로 편집하세요:

```bash
cd /path/to/project
.ai/bin/ai code query "auth flow" --json
.ai/bin/ai context pack "auth flow" --json
.ai/bin/ai code read-hashline src/app.py --start 10 --end 80
.ai/bin/ai doctor --strict --json
.ai/bin/ai obs usage --json
```

기본 MCP 도구:

```text
code_query              BM25/FTS5 code search
context_pack            compact agent-ready context
code_read_hashline      line+sha edit anchors
obs_usage               actual Claude/Codex usage and Code Brain overhead
tool_search             discover hidden MCP tool schemas
```

자주 쓰는 슬래시/소스 명령:

```text
/cb-usage    token and Code Brain activity
/cb-search   code search
/cb-health   doctor + queue + index summary
/cb-doctor   strict diagnostics
/cb-exec     bounded sandbox output
/cb-upgrade  upgrade from the public repo
```

## Proof Points

합성 벤치마크 주장을 믿지 마세요. Code Brain은 여러분의 레포에서 직접 실행할 수 있는 체크를 함께 제공합니다:

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

이 체크들이 증명하는 것:

- Claude, Codex, Antigravity를 위한 설치 및 업그레이드 자산이 존재한다
- 공개 레포 업그레이드 계획이 dry-run 모드에서 파일을 건드리지 않고 작동한다
- strict doctor가 설정, 인덱스 신선도, 매니페스트, 감사 체인, 시크릿 스캔, 핫 패스 SLO, 제한된 생성 산출물, 명령 등록을 검증한다
- 사용량 리포팅이 토큰 절감을 추정하는 대신 실제 Claude/Codex 로그를 읽는다

공개 README에서는 이렇게 재현 가능한 체크를 앞세우세요. 벤치마크 수치는 `scripts/`나 CI의 반복 가능한 스크립트로 생성될 때만 추가하세요.

## What Gets Installed

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

수동 설치, 업그레이드, 제거:

```bash
bash scripts/install-into.sh install /path/to/project
bash scripts/install-into.sh upgrade /path/to/project
bash scripts/install-into.sh uninstall /path/to/project
```

Antigravity 글로벌 MCP는 옵트인 방식으로만 가능합니다:

```bash
AI_INSTALL_GLOBAL_ANTIGRAVITY=1 bash scripts/setup-antigravity-global.sh
```

macOS/Linux 최상위 설치 프로그램은 Claude/Codex 글로벌 킷을 설치할 수 있습니다. 기존 파일을 백업하고 관리되는 Code Brain 블록만 `~/.claude/CLAUDE.md`와 `~/.codex/AGENTS.md`에 병합합니다. Claude 설정, 훅, 명령, 에이전트, 스킬은 `~/.claude/` 아래에 병합되거나 복사됩니다. Antigravity 글로벌 설정은 명시적으로 요청한 경우에만 `code-brain` 항목을 갱신합니다.

## Token And Disk Defaults

기본 MCP 프로필:

```text
AI_CODE_BRAIN_PROFILE=usage
AI_MCP_COMPACT_TOOLS=1
```

프로필별 도구 노출:

```text
usage: obs_usage, code_query, context_pack, code_read_hashline, tool_search
core:  usage + obs_health_summary, obs_search, doctor_strict
full:  all MCP tools
```

생성 산출물 상한:

```text
.ai/memory/events/events.jsonl       4MB cap, payload 20KB cap
.ai/memory/prompt_growth.jsonl       512KB cap
.ai/memory/prompt_growth/versions/   keep latest 30
.ai/memory/evidence.jsonl            4MB cap
.ai/memory/session-current.md        100KB cap
.ai/cache/sandbox/                   pruned after Stop/SessionEnd
```

수동 정리:

```bash
.ai/bin/ai memory page-out --json
.ai/bin/ai exec prune --older-than-seconds 86400 --json
.ai/bin/ai audit rebuild-index --json
```

상한이 적용된 파일이 한도를 넘어 커지면 `doctor --strict`가 `generated_artifacts_bounded`에서 실패합니다.

## Security And Public Repo Hygiene

- 실제 시크릿을 읽거나, 출력하거나, 편집하거나, 커밋하지 마세요.
- `.env`, 키, 토큰, 인증서, 비밀번호 저장소, 런타임 상태, 비공개 메모리는 공개 소스 레포 밖에 둡니다.
- 설치 프로그램은 소스 `.ai/memory/*`나 `.ai/runtime/state/*` 데이터를 대상 프로젝트로 복사하지 않습니다.
- 훅/MCP 핫 패스는 로컬에서 동작하며 네트워크를 호출하지 않습니다.
- 글로벌 Antigravity 파일을 변경하기 전에 `AI_INSTALL_GLOBAL_ANTIGRAVITY=1`이 필요합니다.
- CI 및 릴리스 후보는 `make lint`, 대상 테스트, `make doctor`, 락파일 체크, `make release-gate`를 통과해야 합니다.

## Architecture Map

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

## License

Apache-2.0.
