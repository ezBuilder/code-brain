---
name: diagnose-with-memory
description: 과거 실패 기억을 회상해 버그를 진단하고 근본원인을 다시 기억에 남길 때 사용한다.
---

기억을 활용한 진단 절차:

1. 증상을 한 문장으로 정리한다.
2. 먼저 과거 경험을 회상한다:
   - `.ai/bin/ai memory recall --query "<증상 핵심어>"` (결정·실패·교훈·절차 통합)
   - 또는 `.ai/bin/ai lessons recall --query "<증상 핵심어>"` (교훈만)
3. 회상된 실패가 현재 버전/환경에서도 유효한지 확인한다(날짜·버전 관측이지 영구 금지가 아님).
4. 관련 파일과 기존 패턴을 찾는다.
5. 원인을 확인한 뒤 가장 작은 수정만 적용한다.
6. 좁은 테스트부터 실행해 검증한다.
7. 새로 배운 것을 기억에 남긴다:
   - 재현 가능한 실패: `.ai/bin/ai memory decision add --kind failure --text "<무엇이 실패>" --observed-version <pkg>=<버전> --retest-after <YYYY-MM-DD>`
   - 일반화된 교훈: `.ai/bin/ai lessons add --failure "<현상>" --cause "<원인>" --fix "<해결>"`
8. 완료 보고에 변경/검증/위험을 적는다.

금지:
- 회상 없이 같은 실패를 반복 진단
- 원인 모른 채 우회 코드 작성
- 영구 금지로 기록(항상 버전·날짜 관측으로)

<!-- memanto(MIT)의 diagnose-with-memory 패턴에서 영감, Code Brain 로컬 메모리로 재작성 -->
