---
name: safe-refactor
description: 동작 유지 리팩터링을 작게 수행할 때 사용한다.
---

리팩터링 절차:

1. 리팩터링 목표와 비목표를 명시한다.
2. 변경 전 검증 명령을 확인한다.
3. **behavior-lock: 편집 전에 동작을 고정하는 회귀 테스트가 GREEN인지 먼저 확인한다.** 커버리지가 없으면 최소 회귀 테스트를 먼저 추가하고, GREEN 베이스라인을 만들 수 없으면 중단·보고한다.
4. public behavior가 바뀌지 않도록 범위를 제한한다.
5. 삭제, 표준/플랫폼 기능, 이미 설치된 의존성, 한 줄 축약을 먼저 찾는다.
6. 가장 안전한 변경부터 위험한 순으로 진행한다.
7. 변경 후 동일 검증을 실행해 baseline과 동일한지 확인한다.
8. 포맷팅만 바뀐 diff가 생기면 되돌릴 방법을 먼저 찾는다.
9. 알려진 한계를 남기는 축약은 `cb-simplify: <ceiling>; revisit when <trigger>`로 표시한다.
10. 동작 변경이 발견되면 중단하고 보고한다.

금지:
- 기능 변경
- 스키마/API 변경
- 새 dependency 추가
- 단일 구현 abstraction/factory/wrapper 생성
- 요청 없는 boilerplate/scaffold 추가
- 테스트 약화
- 포맷팅 대량 diff
- 확신이 없을 때 추측(GUESS)으로 변경 — 모르면 그 부분은 SKIP하고 보고한다
- 같은 파일에서 3회 시도 실패 시 강행 — revert 후 중단·보고한다
