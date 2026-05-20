# CLAUDE.md

이 저장소는 Claude Code와 Codex에 적용할 전역 규칙 키트다.

우선순위: 보안/권한 > 전역 규칙 > 프로젝트 규칙 > 작업 방식 > 응답 규칙

## 작업 대상

- 전역 Claude 규칙: `rules/CLAUDE.md`
- 전역 Codex 규칙: `rules/AGENTS.md`
- 설치 스크립트: `install.sh`
- 검증/진단/하네스 스크립트: `scripts/validate.sh`, `scripts/doctor.sh`, `scripts/harness.sh`
- 상세 정책: `docs/AI_*.md`
- Claude Code 프로젝트 키트: `.claude/`

`rules/`와 `.claude/`가 설치 대상이고, `docs/`는 참고 정책과 조사 근거다.

## 필수 확인

- 보안/권한 작업 전: `docs/AI_SECURITY.md`
- 아키텍처 판단 전: `docs/AI_ARCHITECTURE.md`
- 테스트/검증 전: `docs/AI_TESTING.md`
- 토큰/컨텍스트 최적화 전: `docs/AI_TOKEN_OPTIMIZATION.md`
- Claude subagent 조정 전: `docs/AI_SUBAGENTS.md`
- Code Brain 접목 전: `docs/AI_INTEGRATIONS.md`
- Claude/Codex 최신 기능 반영 전: `docs/AI_RESEARCH.md`
- 자율 개발 루프 조정 전: `docs/AI_DEV_LOOP.md`

모르면 추측하지 말고 실제 파일, 명령 도움말, 로컬 설정에서 확인한다.

## 절대 규칙

- 사용자 변경, 로컬 전역 설정, 인증 파일을 임의로 되돌리지 않는다.
- `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`는 백업 없이 덮어쓰지 않는다.
- 커밋, 푸시, GitHub repo 생성, 배포, 패키지 publish는 사용자 명시 요청이 있을 때만 한다.
- 민감 파일, 토큰, 키, 실제 `.env`는 읽기/수정/출력하지 않는다.
- 검증 없이 성공, 완료, 설치됨이라고 말하지 않는다.

## 작업 방식

1. 현재 대상이 전역 규칙, 프로젝트 키트, 문서 중 무엇인지 먼저 구분한다.
2. 기존 파일의 역할을 유지하고 중복만 줄인다.
3. 새 규칙은 Claude와 Codex 양쪽에 적용 가능한지 확인한다.
4. Claude 전용 개념은 `rules/CLAUDE.md` 또는 `.claude/`에만 둔다.
5. Codex 전용 개념은 `rules/AGENTS.md`에 별도로 변환한다.
6. Code Brain은 프로젝트별 선택 통합으로 두고 전역 강제 의존성으로 만들지 않는다.
7. 전역 규칙은 짧게 유지하고 상세 정책은 `docs/`로 분리한다.
8. 변경 후 `make validate` 또는 `./scripts/validate.sh`를 실행한다.

## Diff 원칙

- 모든 변경 라인은 전역 설치 안정성, 규칙 명확성, 검증 가능성과 연결되어야 한다.
- placeholder, 중복 규칙, 문서 간 충돌을 남기지 않는다.
- 설치 스크립트는 dry-run, 백업, 실패 시 명확한 오류를 제공해야 한다.
- 외부 도구 의존성은 optional로 처리하고 없을 때의 fallback을 문서화한다.

## 완료 보고

- 변경:
- 검증:
- 참고/위험:

검증을 못 했으면 이유와 대신 확인한 내용을 적는다.
