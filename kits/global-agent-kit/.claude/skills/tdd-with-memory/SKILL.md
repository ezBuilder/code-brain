---
name: tdd-with-memory
description: 과거 결정·컨벤션을 회상해 테스트 우선으로 구현하고 새 컨벤션을 기억에 남길 때 사용한다.
---

기억을 활용한 TDD 절차:

1. 구현 대상을 한 문장으로 정리한다.
2. 먼저 관련 결정·컨벤션·교훈을 회상한다:
   - `.ai/bin/ai memory recall --query "<기능/모듈 핵심어>"`
   - 결정만 좁혀 보려면 `.ai/bin/ai memory decision list --text "<핵심어>"`
3. RED: 기대 동작을 표현하는 실패 테스트를 먼저 작성한다.
4. GREEN: 테스트를 통과시키는 가장 작은 구현만 한다.
5. REFACTOR: 동작을 유지한 채 중복·과설계를 줄인다.
6. 좁은 테스트부터 실행하고, 필요하면 린트/타입체크를 돌린다.
7. 재사용할 가치가 있는 새 컨벤션/설계 결정을 기억에 남긴다:
   - `.ai/bin/ai memory decision add --text "<채택한 컨벤션/결정>" --tag convention`
   - 반복되는 실패 예방책이면 `.ai/bin/ai lessons add --failure ... --cause ... --fix ...`
8. 완료 보고에 변경/검증/위험을 적는다.

금지:
- 테스트 없이 구현부터 작성
- 통과를 위해 실패 테스트 삭제·약화
- 기존 결정과 모순되는 컨벤션을 회상 없이 도입(충돌 시 `memory conflicts`로 확인)

<!-- memanto(MIT)의 tdd-with-memory 패턴에서 영감, Code Brain 로컬 메모리로 재작성 -->
