---
description: 자가개선 폐루프 — 최근 작업을 싼 judge로 리뷰해 프롬프트 규칙 1개를 제안(자동 ratchet·롤백).
argument-hint: "[run | status | judge]"
---

`$ARGUMENTS` 분기. 폐루프는 사용자 입력을 절대 막지 않는다(백그라운드 워커가 수행).

- **`run`(기본)** → `ai selfimprove run --tier cheap` 후 `ai loopd dispatch-once`. 싼 워커에 리뷰 작업 enqueue. 1줄 보고(요청 id).
- **`status`** → `ai selfimprove status --json` rules·learned_prompt 1줄 요약.
- **`judge`** → 싼 judge 워커 모드. 절차 수행.

## judge 절차 (싼 모델 전용. 코드/파일 수정 금지)
1. `ai prompt-growth status` + `ai obs search`로 최근 신호 확인.
2. 반복·일반화 가능한 개선이 명확할 때만 규칙 1개:
   `ai selfimprove propose --text "<일반화된 규칙>" --rationale "<근거>"`
3. 보안/승인/redaction 약화 규칙은 자동 거부(M_core). 자동 적용 후 토큰 ratchet으로 keep/rollback.
4. 명확한 게 없으면 "no change".

규칙: 1회 1규칙. 일반화된 규칙 문장. 보고 1줄.
