---
name: implement-feature
description: 새 기능을 최소 변경으로 구현할 때 사용한다.
---

기능 구현 절차:

1. 요구사항을 검증 가능한 목표로 바꾼다.
2. 기존 구현과 유사 패턴을 조사한다.
3. public API, schema, UX 변경 여부를 확인한다.
4. Code Brain이 설치된 프로젝트인지 확인하고, 있으면 검색/검증 경로에 활용한다.
5. 무코드, 표준/플랫폼 기능, 이미 설치된 의존성, 한 줄 구현 순서로 먼저 줄인다.
6. 필요한 테스트만 추가/수정한다.
7. 좁은 검증부터 실행한다.
8. 알려진 한계를 남기는 단순화는 `cb-simplify: <ceiling>; revisit when <trigger>`로 표시한다.
9. 기존 동작 변경이 있으면 완료 보고에 명시한다.

금지:
- 요청 없는 옵션/설정/확장성 추가
- 새 dependency 추가
- 단일 구현 abstraction/factory/wrapper 생성
- 요청 없는 boilerplate/scaffold 추가
- 무관한 코드 개선
