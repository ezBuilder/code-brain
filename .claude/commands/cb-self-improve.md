---
description: 자가개선 폐루프 — 최근 작업을 싼 judge로 리뷰해 프롬프트 규칙 1개를 제안(자동 ratchet·롤백).
argument-hint: "[run | status | judge]"
---

`$ARGUMENTS` 분기. 이 폐루프는 사용자 입력을 절대 막지 않는다(백그라운드 워커가 수행).

- **`run`(기본)** → `ai selfimprove run --tier cheap` 실행. loopd 풀의 싼 워커에 "최근 출력 vs 사용자 명령 리뷰→규칙 1개 제안" 작업을 enqueue한다. 그 다음 `ai loopd dispatch-once`로 배정. 1줄 보고(요청 id).
- **`status`** → `ai selfimprove status --json`의 rules(active/kept/regressed)와 learned_prompt를 1줄 요약.
- **`judge`** → (싼 judge 워커가 직접 호출하는 모드) 아래 judge 절차 수행.

## judge 절차 (싼 모델 전용. 코드/파일 수정 금지)
1. `ai prompt-growth status` + `ai obs search`로 최근 신호 확인(장황 보고, 반복 패턴).
2. **반복되고 일반화 가능한** 개선이 명확할 때만 규칙 1개 제안:
   `ai selfimprove propose --text "<일반화된 규칙>" --rationale "<근거+증거>"`
3. 규칙은 보안/승인/redaction을 약화하면 자동 거부된다(M_core 게이트). 자동 적용 후 실측 토큰 ratchet으로 좋아지면 유지, 나빠지면 자동 롤백된다.
4. 명확한 게 없으면 아무것도 하지 말고 "no change" 보고.

규칙: 한 번에 규칙 1개. 결과 요약이 아니라 일반화된 규칙 문장. 보고 1줄.
