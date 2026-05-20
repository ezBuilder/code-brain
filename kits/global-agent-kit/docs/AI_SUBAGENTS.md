# AI_SUBAGENTS.md

Claude Code 프로젝트 키트의 subagent 정책이다. Codex 전역 규칙에는 그대로 적용하지 않는다.

## 모델 정책

- 기본 탐색과 테스트 검토: Haiku
- 보안, 권한, 결제, 삭제, 복잡한 설계 판단: Sonnet
- 금지: Opus, inherit

## 현재 subagent

- `codebase-researcher`: Haiku, 읽기 전용 코드베이스 조사
- `test-reviewer`: Haiku, 테스트 누락과 최소 검증 명령 점검
- `security-reviewer`: Sonnet, 보안/권한/민감 로직 리뷰
- `codebase-deep-reviewer`: Sonnet, 복잡 설계와 대규모 영향 분석

## 사용 기준

- 단순한 파일 확인, 짧은 검색, 한두 파일 수정은 메인 세션에서 처리한다.
- 대량 조사, 넓은 영향 범위, 테스트 리뷰, 보안 리뷰, 복잡한 회귀 위험 분석은 subagent를 적극 사용한다.
- subagent는 파일을 수정하지 않는 읽기 전용 역할로 유지한다.
- 결과는 근거 파일, 함수, 라인 중심으로 보고하게 한다.

## 병렬 agent

- 서로 독립적인 조사, 영향 범위 분석, 테스트 검토, 보안 검토는 병렬로 나눈다.
- 즉시 다음 작업을 막는 핵심 판단은 메인 세션이 직접 수행한다.
- 코드 수정 agent를 병렬로 쓸 때는 파일/모듈 소유권을 명확히 나눈다.
- 같은 파일을 여러 agent가 동시에 수정하게 하지 않는다.
- 병렬 결과는 메인 세션이 통합 검토하고, 검증 전 완료 처리하지 않는다.
- 단순 작업이나 토큰 비용이 더 큰 작업에는 병렬 agent를 쓰지 않는다.

## Supervisor mode

- 대규모, 장기, 다중 모듈 작업은 메인 세션을 supervisor로 둔다.
- supervisor는 목표, 범위, 비목표, 작업 분해, 파일 소유권, 검증 기준을 정한다.
- 조사, 테스트 리뷰, 보안 리뷰, 독립 모듈 구현은 subagent에 위임한다.
- 핵심 원인 분석, 충돌 해결, 최종 diff 검토, 최종 검증 판단은 supervisor가 직접 수행한다.
- 작은 단일 파일 작업에는 supervisor-only 구조를 강제하지 않는다.

## 주의

`CLAUDE_CODE_SUBAGENT_MODEL` 환경변수는 모든 subagent 모델을 덮어쓴다.
Haiku/Sonnet 분리를 유지하려면 이 값을 설정하지 않는다.
