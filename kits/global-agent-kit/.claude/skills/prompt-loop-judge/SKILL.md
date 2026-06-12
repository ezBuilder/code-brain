---
name: prompt-loop-judge
description: 세션 종료 시점에 1회, 사용자 명령 대비 출력 품질을 채점하고 반복 위반이 보이면 프롬프트 패치를 제안한다. 자기 자신이 아닌 저가 모델(haiku 서브에이전트)로 평가한다.
---

목적: "사용자 명령 vs 에이전트 출력"을 비교 채점해, 반복되는 프롬프트 위반(특히 ≤50자 보고 규약 위반, 불필요한 장황함)을 발견하면 프롬프트 수정안을 pending으로 적재한다. 패치는 절대 자동 적용하지 않는다 — 사람이 `ai prompt-loop accept`로만 반영한다.

## 실행 규칙

- **세션당 1회만.** 매턴 호출 금지(토큰 역효과). 세션 종료/요약 시점에만 돈다.
- **자기 자신 금지.** 평가는 반드시 별도 저가 모델(haiku 서브에이전트)로 위임한다. `Agent(subagent_type=...)` 또는 complexity_router(`autoresearch_route`)로 local/저가 라우팅.
- **측정 우선.** 추정 금지. 토큰은 `ai prompt-loop signals`(휴리스틱 위반 신호)와 `ai obs usage`(실측 출력 토큰)만 근거로 쓴다.

## 절차

1. 신호 수집: `ai prompt-loop signals --json` → `long_reports`(≤50자 규약 위반 추정 건수)·샘플 확인.
2. 위반이 0이면 종료(패치 제안 없음). 노이즈 패치를 만들지 않는다.
3. 반복 위반이 있으면 저가 judge 서브에이전트에게 위임:
   - 입력: 위반 샘플 + 현재 전역 규약(`rules/CLAUDE.md` 응답 섹션).
   - 산출: 일반화된 **프롬프트 패치 문장**(결과 요약이 아니라 규칙 diff). ReasoningBank식 일반화.
4. 패치 적재(자동적용 아님):
   `ai prompt-loop propose --target global_claude --rationale "<왜>" --patch "<규칙 수정안>" --violation "<유형>" --evidence "<샘플>"`
   - target: `global_claude` | `global_codex` | `project_agents`.
5. 사용자에게 1줄로 보고: pending 패치 id와 핵심. 사용자가 `accept`해야만 반영됨을 명시.

## 가드(drift 방지)

- 중복/모순 패치 금지: 기존 pending과 의미가 겹치면 새로 만들지 말고 보고만 한다.
- 패치는 항상 rollback 가능한 단위(규칙 1개)로 쪼갠다.
- self-reinforcing drift 의심 시 제안하지 말고 사용자 판단을 요청한다.
