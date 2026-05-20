# AI_ARCHITECTURE.md

전역 규칙 키트와 프로젝트별 AI 지침을 설계할 때 따르는 아키텍처 기준이다.

## 역할 분리

- `rules/`: 전역 설치 대상. 모든 프로젝트에 적용 가능한 짧고 강한 규칙만 둔다.
- `docs/`: 상세 정책. 판단 기준과 확장 규칙을 설명하지만 자동 설치 대상은 아니다.
- `.claude/`: Claude Code 프로젝트 확장 자산. settings, hooks, agents, skills를 포함한다.
- `.ai/`: Code Brain이 설치된 프로젝트의 repo-local source of truth다. 이 키트가 직접 소유하지 않는다.

## 설계 원칙

- 기존 구조와 도구의 source of truth를 존중한다.
- 단일 사용 목적의 추상화는 만들지 않는다.
- 전역 규칙은 특정 언어, 프레임워크, 패키지 매니저에 의존하지 않는다.
- 프로젝트별 세부 명령은 프로젝트 문서나 manifest에서 발견한다.
- 공용 API, 스키마, hook payload, settings 구조를 바꿀 때는 영향 범위를 먼저 확인한다.

## 변경 전 체크

- 이 규칙이 전역에 강제돼도 안전한가?
- Claude 전용인지, Codex에도 적용 가능한지 구분했는가?
- 프로젝트별 예외가 필요한 내용인지 확인했는가?
- 기존 `docs/AI_*`, `.claude/settings.json`, hooks와 충돌하지 않는가?
- Code Brain이 없어도 정상 동작하는가?

## 변경 후 체크

- 설치 대상과 참고 문서가 분리되어 있는가?
- 보안/권한 규칙이 약화되지 않았는가?
- placeholder나 비어 있는 명령이 남아 있지 않은가?
- `scripts/validate.sh`가 통과하는가?
- 전역 설치 시 기존 사용자 파일이 백업되는가?
