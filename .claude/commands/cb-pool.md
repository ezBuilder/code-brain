---
description: 멀티에이전트 풀(codex/claude/agy) 운영 — 자연어로 작업 위임. CLI 외울 필요 없음.
argument-hint: "[작업 설명 | status | up | down]"
---

`$ARGUMENTS`를 해석해 Code Brain 워커 풀을 MCP 도구로 운영한다. 사용자는 CLI를 외우지 않는다.

도구(전부 MCP): `loopd_agents`, `loopd_status`, `loopd_up`, `loop_submit`, `loopd_dispatch_once`, `loopd_recover`.

**항상 먼저** `loopd_agents`로 설치된 에이전트(codex/claude/agy)와 tmux 가용 여부를 확인한다. 설치 안 된 에이전트는 자동 스킵된다(에러 아님). 하나도 없으면 "사용 가능한 에이전트 없음 — 풀 비활성"이라고 1줄 보고하고 종료.

분기:
- **인자 없음 또는 `status`** → `loopd_status` 호출 후 1줄 요약(큐 pending/processing/done, 워커 idle/working 수).
- **`up`** → `loopd_up`(autonomous=true) 호출해 풀 기동. 실패/미설치면 사유 1줄.
- **`down`** → 사용자에게 `tmux kill-session` 안내(직접 실행은 안 함).
- **그 외(작업 설명)** → 위임 실행:
  1. `loopd_status`로 idle 워커 있는지 확인. 없으면 `loopd_up`(autonomous=true)로 먼저 기동.
  2. 작업을 1개 이상으로 쪼개 각각 `loop_submit`(instruction=구체적 작업, goal=한 줄). 모델 티어는 지정하지 말 것 — 복잡도로 자동 선택된다. 사용자가 "싸게"라고 하면 model_tier=cheap, "최고로"면 best.
  3. `loopd_dispatch_once` 호출해 배정.
  4. `loopd_status`로 배정 결과 확인, 1줄 보고(어떤 워커에 몇 건).
  5. 진행 확인이 필요하면 `loopd_recover`(완료→idle·멈춤 nudge) 후 다시 status.

규칙:
- 위험 작업(배포/시크릿/삭제/머지)은 자동 배정 안 됨 — `loopd_status`의 blocked로 뜨면 사용자 승인 필요하다고 1줄 보고.
- 보고는 핵심 1줄. 큐/워커 숫자와 배정 결과만. 표·해설 금지.
