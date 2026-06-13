---
description: 멀티에이전트 풀(codex/claude/agy) 운영 — 자연어로 작업 위임. CLI 외울 필요 없음.
argument-hint: "[작업 설명 | status | up | down]"
---

`$ARGUMENTS`를 해석해 Code Brain 워커 풀을 MCP 도구로 운영한다. 사용자는 CLI를 외우지 않는다.

도구(전부 MCP): `loopd_status`, `loopd_up`, `loop_submit`, `loopd_dispatch_once`, `loopd_recover`.

분기:
- **인자 없음 또는 `status`** → `loopd_status` 후 1줄 요약(큐 pending/processing/done, 워커 idle/working).
- **`up`** → `loopd_up`(autonomous=true)로 풀 기동. 실패/미설치면 사유 1줄.
- **`down`** → 사용자에게 `tmux kill-session` 안내(직접 실행 안 함).
- **그 외(작업 설명)** → 위임:
  1. `loopd_status`로 idle 워커 확인. 없으면 `loopd_up`(autonomous=true) 먼저.
  2. 작업을 쪼개 각각 `loop_submit`(instruction, goal). 티어 미지정(복잡도 자동). "싸게"=cheap, "최고로"=best.
  3. `loopd_dispatch_once` 배정.
  4. `loopd_status`로 결과 확인 후 1줄 보고(워커별 건수).
  5. 필요 시 `loopd_recover`(완료→idle·nudge) 후 재확인.

규칙:
- 위험 작업(배포/시크릿/삭제/머지)은 자동 배정 안 됨 — blocked면 승인 필요 1줄 보고.
- 보고 핵심 1줄. 표·해설 금지.
