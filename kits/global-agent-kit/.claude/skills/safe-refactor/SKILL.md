---
name: safe-refactor
description: 동작 유지 리팩터링을 작게 수행할 때 사용한다.
---

리팩터링 절차:

1. 리팩터링 목표와 비목표를 명시한다.
2. 변경 전 검증 명령을 확인한다.
3. public behavior가 바뀌지 않도록 범위를 제한한다.
4. 삭제, 표준/플랫폼 기능, 이미 설치된 의존성, 한 줄 축약을 먼저 찾는다.
5. 변경 후 동일 검증을 실행한다.
6. 포맷팅만 바뀐 diff가 생기면 되돌릴 방법을 먼저 찾는다.
7. 알려진 한계를 남기는 축약은 `cb-simplify: <ceiling>; revisit when <trigger>`로 표시한다.
8. 동작 변경이 발견되면 중단하고 보고한다.

금지:
- 기능 변경
- 스키마/API 변경
- 새 dependency 추가
- 단일 구현 abstraction/factory/wrapper 생성
- 요청 없는 boilerplate/scaffold 추가
- 테스트 약화
- 포맷팅 대량 diff
