# AI_RESEARCH.md

Claude Code와 Codex CLI 기능 조사를 이 키트에 반영할 때의 기준이다. 날짜가 중요한 기능은 공식 문서나 upstream repo를 다시 확인한다.

## 확인한 공식 기능면

Claude Code:

- 전역/프로젝트 settings는 `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json` 계층으로 동작한다.
- settings는 Managed, command line, Local, Project, User 순서로 적용되며 permission rule은 merge 동작을 한다.
- hooks는 `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart` 등 이벤트에 연결할 수 있고 MCP tool matcher도 지원한다.
- 최신 hook 표면에는 `PermissionDenied`, `PostToolUseFailure`, `PostToolBatch`, `TaskCreated`처럼 진단/관찰에 유용한 이벤트가 포함된다.
- slash command에는 `/agents`, `/doctor`, `/mcp`, `/memory`, `/permissions`, `/cost` 등이 포함되므로 이 키트의 `doctor`와 하네스는 Claude 내장 진단 흐름과 충돌하지 않게 shell script로 제공한다.
- hook script 경로는 프로젝트나 플러그인 기준 placeholder를 쓸 수 있으나, user-level 설치 자산은 installer가 절대 경로로 고정해야 한다.
- subagent는 `~/.claude/agents/` 또는 `.claude/agents/`의 Markdown frontmatter로 배포할 수 있다.
- skills는 `~/.claude/skills/` 또는 `.claude/skills/`의 `SKILL.md`로 배포하며, 기존 slash command보다 supporting files와 호출 제어에 유리하다.
- MCP는 OAuth, Claude.ai connector, prompt-as-command, resource reference를 제공하므로 전역 규칙은 MCP 사용을 허용하되 인증 토큰이나 원격 설정을 자동 생성하지 않는다.
- auto memory는 Claude Code 버전에 따라 기본 활성화될 수 있으므로 보안 규칙은 민감 정보 저장 금지를 별도로 유지해야 한다.

Codex CLI:

- Codex CLI는 로컬 터미널 agent로 동작하며 `npm install -g @openai/codex` 또는 Homebrew 설치 경로를 제공한다.
- Codex의 지속 지침은 `AGENTS.md`가 핵심이며, 전역 파일은 `~/.codex/AGENTS.md`로 배포한다.
- Codex는 sandbox와 approval/full-auto 흐름으로 명령 실행 경계를 관리하므로 전역 자연어 규칙은 보조 안전망으로 둔다.
- Codex 공식 설정 문서는 `config.toml`의 approval/sandbox/MCP 설정을 다루며, lifecycle hook의 managed-only 제한은 `requirements.toml` 전용이다.
- sandbox 동작은 `codex sandbox macos|linux|windows` 하위 명령으로 확인할 수 있다.
- MCP와 IDE/desktop/cloud 진입점은 환경마다 활성화 방식이 다르므로 이 키트는 토큰이나 커넥터 등록을 자동으로 만들지 않는다.

## 이 키트에 반영한 결정

- `./install.sh --all --yes`는 Claude 규칙, Claude settings, hooks, agents, skills, Codex 규칙을 한 번에 설치한다.
- `./scripts/harness.sh --once --install`은 설치 품질 루프를 한 번 실행하고, `--forever --install`은 명시적인 tmux/장시간 세션에서 반복 실행한다.
- 기존 파일이나 디렉터리는 `~/.local/state/code-brain-global-kit/backups/` 아래에 백업한다. 전역 `CLAUDE.md`/`AGENTS.md`는 덮어쓰기보다 managed block을 추가/갱신한다.
- 반복 하네스 실행으로 백업이 무한 증가하지 않게 기본 20개만 보존한다.
- 기존 Claude user settings는 덮어쓰기보다 permissions/hooks를 병합한다.
- Claude hooks는 user-level 설치 후에도 동작하도록 `~/.claude/hooks/` 절대 경로로 변환한다.
- 실제 credential, OAuth, MCP token, production secret은 자동 설정하지 않는다.
- Claude/Codex 공통 규칙은 짧게 유지하고, 조사 근거와 운영 정책은 `docs/AI_*.md`에 둔다.

## 재조사할 때 볼 공식 위치

- Claude Code settings: `https://docs.anthropic.com/en/docs/claude-code/settings`
- Claude Code hooks: `https://docs.anthropic.com/en/docs/claude-code/hooks`
- Claude Code skills/slash commands: `https://docs.anthropic.com/en/docs/claude-code/slash-commands`
- Claude Code subagents: `https://docs.anthropic.com/en/docs/claude-code/sub-agents`
- Claude Code MCP: `https://docs.anthropic.com/en/docs/claude-code/mcp`
- Claude Code memory: `https://docs.anthropic.com/en/docs/claude-code/memory`
- Codex CLI repo: `https://github.com/openai/codex`
- Codex configuration reference: `https://developers.openai.com/codex/config-reference`
- Codex security/sandbox: `https://developers.openai.com/codex/security`
- Codex CLI help: `https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started`
