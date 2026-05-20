# code-brain-global-kit

Claude Code와 Codex에 같은 작업 원칙을 적용하기 위한 전역 규칙 키트다.

이 저장소의 기본 설치 대상은 `rules/`와 Claude Code 전역 확장 자산이다. `docs/`는 상세 정책과 조사 근거, `.claude/`는 설치 가능한 settings/hooks/agents/skills 원본이다.

## 설치

Private repo로 사용할 때는 먼저 GitHub CLI 인증을 확인한다.

```bash
gh auth status
```

권장 설치 위치:

```bash
gh repo clone ezBuilder/code-brain-global-kit ~/.local/share/code-brain-global-kit
cd ~/.local/share/code-brain-global-kit
./scripts/validate.sh
```

Claude 전역 규칙만 설치:

```bash
./install.sh --claude --yes
```

Codex 전역 규칙만 설치:

```bash
./install.sh --codex --yes
```

둘 다 설치:

```bash
./install.sh --all --yes
```

`--all`은 다음을 한 번에 설치한다.

- Claude: `~/.claude/CLAUDE.md`, `~/.claude/settings.json`, `~/.claude/hooks/`, `~/.claude/policies/`, `~/.claude/agents/`, `~/.claude/skills/`, `~/.claude/commands/`
- Codex: `~/.codex/AGENTS.md`

규칙 파일만 갱신하려면:

```bash
./install.sh --all --rules-only --yes
```

설치 전 점검:

```bash
./install.sh --all --dry-run
```

## 설치 대상

- Claude: `~/.claude/CLAUDE.md`
- Claude Code assets: `~/.claude/settings.json`, `~/.claude/hooks/`, `~/.claude/agents/`, `~/.claude/skills/`
- Claude hook policy: `~/.claude/policies/hook-policy.json`
- Claude Code commands: `~/.claude/commands/kit-doctor.md`, `~/.claude/commands/kit-research.md`, `~/.claude/commands/kit-upgrade-loop.md`
- Codex: `~/.codex/AGENTS.md`
기존 파일은 `~/.local/state/code-brain-global-kit/backups/` 아래 timestamp 디렉터리에 백업된다.
기본 보존 개수는 20개이며 `CODE_BRAIN_GLOBAL_KIT_BACKUP_RETENTION=0`으로 정리를 끌 수 있다.

## 공식 기능 조사

반영 기준은 `docs/AI_RESEARCH.md`에 둔다. 현재 키트는 Claude Code settings/hooks/subagents/skills/MCP/auto-memory와 Codex CLI AGENTS/sandbox/approval 흐름을 기준으로 설계한다.
Hook-first 운영 기준은 `docs/AI_HOOKS.md`, 자율 개발 루프 기준은 `docs/AI_DEV_LOOP.md`, Evolution Loop 기준은 `docs/AI_EVOLUTION.md`에 둔다.

## Hook-first 개발 루프

기본 순서는 `hooks -> commands/prompts -> skills/subagents -> optional MCP`다. Secret, destructive command, deployment, billing, credential 경계는 prompt나 skill보다 먼저 검증되어야 한다.

Code Brain식 기능명:

- `Code Brain Skill Router`: 요청마다 필요한 절차, 스킬, 검증만 짧게 라우팅한다.
- `Code Brain Delivery Loop`: 계획, 리뷰, QA, 릴리스 점검을 호출형 명령으로만 실행한다.
- `Code Brain Guardrails`: secret, destructive command, deploy, billing, auth 경계를 훅에서 먼저 잡는다.
- `Code Brain Snapshot`: Claude/Codex 설정을 로컬 백업하고 restore dry-run으로 복구 가능성을 확인한다.
- `Code Brain Context Score`: CLAUDE/AGENTS/hooks/skills/MCP 품질을 점수화하되 항상 컨텍스트에 주입하지 않는다.

채택 기준:

- 외부 workflow/tool 아이디어는 이름이나 구조를 그대로 들여오지 않고, Code Brain 기능명과 정책으로 재설계한다.
- 작고 검증 가능한 로컬 명령, 문서, prompt, hook policy로만 채택한다.
- OAuth/token, 원격 동기화, package 변경, production 작업, broad filesystem/network access가 필요한 후보는 기본 설치에서 제외한다.
- 새 hook 또는 loop script는 `scripts/validate.sh`의 required file과 `bash -n` 검증에 포함한다.

## 자율 하네스

한 번만 실행:

```bash
make harness-install-once
```

장시간 반복 실행:

```bash
tmux new -s code-brain-global-kit-harness 'make harness-forever'
```

하네스는 `validate -> doctor -> install dry-run -> 선택적 install` 순서로 반복한다. `--forever`는 명시적인 장시간 세션에서만 실행한다.

연구/평가/개발 루프 1회 실행:

```bash
./scripts/dev-loop.sh --once
```

연구 스냅샷만 생성:

```bash
./scripts/research-snapshot.sh
```

## Evolution Loop

Code Brain Evolution Loop는 `capture -> score -> promote -> snapshot` 순서로 연구 후보를 작게 검토한다. 설계 기준은 `docs/AI_EVOLUTION.md`에 있으며, 토큰 예산은 SessionStart 600자/top 3 주입으로 제한하고 memory poisoning 방어와 auto-apply 경계를 문서화한다.

```bash
make evolution-capture
make evolution-score
make evolution-promote
make evolution-snapshot
```

실제 스크립트는 `scripts/evolve-*.sh`이며, promotion은 dry-run 기본값과 사용자 승인 경계를 유지한다.

## Code Brain 접목

Code Brain은 전역 도구가 아니라 프로젝트별 `.ai/` 설치 도구다. 대상 프로젝트에 `.ai/bin/ai`가 있을 때만 사용한다.

```bash
.ai/bin/ai session start --agent claude --json
.ai/bin/ai session start --agent codex --json
.ai/bin/ai obs health-summary --json
.ai/bin/ai recommend skills --limit 5 --json
```

새 프로젝트에 Code Brain을 설치할 때는 로컬 Code Brain repo에서 실행한다.

```bash
cd /Users/ezbuilder/workspace/code-brain
make install-into TARGET=/path/to/repo
```

Code Brain이 스킬 후보를 제안하면 사용자 승인 후에만 생성, 수정, 삭제, 전역 승격한다.

## 검증

```bash
make validate
make doctor
make codex-doctor
```

검증 항목:

- 필수 파일 존재
- shell script 문법
- JSON 설정 문법
- 임시 HOME 설치 스모크 테스트
- 자율 하네스 문법
- 연구/평가 개발 루프 문법
- Evolution Loop 문서 존재와 README 색인
- 설치 대상 규칙 파일 존재
- 전역 Claude/Codex 설치물 상태
- Codex `config.toml`/`hooks.json` 구조 진단
- 선택한 repo의 project-local Claude override 충돌
- placeholder 잔존 여부
- `.DS_Store` 같은 로컬 산출물 잔존 여부

특정 repo의 로컬 Claude 설정까지 확인:

```bash
./scripts/doctor.sh --target /path/to/repo
```

## 토큰 최적화

- `rules/CLAUDE.md`와 `rules/AGENTS.md`는 매 세션 로드될 수 있으므로 짧게 유지한다.
- 자주 바뀌는 설명과 긴 정책은 `docs/`에 둔다.
- 프로젝트마다 `.claudeignore`를 두어 빌드 산출물과 대용량 디렉터리를 제외한다.
- 긴 검색/출력은 Code Brain이 있으면 요약 경로를 우선하고, 없으면 `rg` 범위를 좁힌다.
