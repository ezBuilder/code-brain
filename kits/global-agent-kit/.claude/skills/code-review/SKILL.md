---
name: code-review
description: 변경사항을 병합 전 검토할 때 사용한다.
---

리뷰 기준:

1. 요청 범위와 diff가 일치하는지 확인한다.
2. 새 dependency, 단일 구현 abstraction, 요청 없는 boilerplate/scaffold를 찾는다.
3. 타입/린트/테스트 실패 가능성을 확인한다.
4. 보안/인증/권한/삭제 영향 여부를 확인한다.
5. 테스트 누락과 edge case를 확인한다.
6. 전역 규칙, 프로젝트 규칙, settings/hooks 간 충돌을 확인한다.
7. 수정 제안은 작고 구체적으로 작성한다.

보고 형식:
- 차단 이슈:
- 개선 권장:
- 검증 필요:
